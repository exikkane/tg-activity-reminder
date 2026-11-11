import asyncio
import logging
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from .youtrack import WORK_FIELDS, ServiceError, Uncertain, date_ms

log = logging.getLogger(__name__)


class WorkService:
    def __init__(self, settings, db, yt):
        self.s, self.db, self.yt = settings, db, yt
        self.locks = defaultdict(asyncio.Lock)

    async def record(self, tg, flow, issue_id, minutes, now=None):
        async with self.locks[tg]:
            user = self.db.user(tg)
            if not user or not user["active"]:
                raise ServiceError("Подключи YouTrack командой /start.")
            day = (
                (now or datetime.now(ZoneInfo(user["zone"])))
                .astimezone(ZoneInfo(user["zone"]))
                .date()
                .isoformat()
            )
            op = self.db.prepare_operation(flow, tg, issue_id, minutes, day)
            return await self._run(tg, op)

    async def reconcile(self, tg, op_id):
        async with self.locks[tg]:
            op = self.db.one("SELECT * FROM operations WHERE id=? AND tg=?", (op_id, tg))
            user = self.db.user(tg)
            if not op or not user or op["yt"] != user["yt"]:
                raise ServiceError("Запись недоступна для этого аккаунта.")
            return await self._run(tg, op)

    async def _run(self, tg, op):
        if op["status"] == "done":
            return op
        if op["status"] == "rejected":
            raise ServiceError(
                "Эта попытка отклонена YouTrack. Открой задачу и введи время заново."
            )
        if op["status"] in {"sending", "uncertain"}:
            found = await self.yt.find_work(tg, op)
            if found:
                self.db.mark_operation(op["id"], "done", found["id"])
                return self.db.one("SELECT * FROM operations WHERE id=?", (op["id"],))
            raise Uncertain(op["id"])
        # Read access and project time-tracking configuration before marking a write in flight.
        await self.yt.issue(tg, op["issue"])
        tracking = await self.yt.tracking(tg, op["issue"])
        if not tracking.get("enabled"):
            self.db.mark_operation(op["id"], "rejected")
            raise ServiceError("В этом проекте отключён учёт времени.")
        await self.yt.access_token(tg)
        changed = self.db.execute(
            "UPDATE operations SET status='sending' WHERE id=? AND status='prepared'", (op["id"],)
        )
        if changed.rowcount != 1:
            raise Uncertain(op["id"])
        try:
            result = await self.yt.request(
                tg,
                "POST",
                f"/issues/{op['issue']}/timeTracking/workItems",
                params={"fields": WORK_FIELDS},
                json={
                    "date": date_ms(op["day"]),
                    "duration": {"minutes": op["minutes"]},
                    "author": {"id": op["yt"]},
                    "text": "tg-time:" + op["id"],
                },
            )
            if (
                not result.get("id")
                or result.get("author", {}).get("id") != op["yt"]
                or result.get("duration", {}).get("minutes") != op["minutes"]
            ):
                raise ServiceError("Не удалось проверить запись.", 502)
        except ServiceError as exc:
            # Definitive client rejection vs. unknown network/server outcome.
            if exc.status in {400, 401, 403, 404, 422, 429}:
                self.db.mark_operation(op["id"], "rejected")
                raise
            self.db.mark_operation(op["id"], "uncertain")
            log.warning("work_result_uncertain operation=%s", op["id"])
            raise Uncertain(op["id"]) from None
        self.db.mark_operation(op["id"], "done", result["id"])
        return self.db.one("SELECT * FROM operations WHERE id=?", (op["id"],))


class ReminderService:
    def __init__(self, settings, db, send):
        self.s, self.db, self.send = settings, db, send

    async def tick(self, now):
        from aiogram.exceptions import TelegramForbiddenError

        for user in self.db.all("SELECT * FROM users WHERE active=1 AND blocked=0"):
            local = now.astimezone(ZoneInfo(user["zone"]))
            weekday = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[local.weekday()]
            if self.s.reminder_days != "all" and weekday not in self.s.reminder_days.split(","):
                continue
            day = local.date().isoformat()
            preference = self.db.reminder_preference(user["tg"])
            target = preference["time"] if preference else self.s.reminder_time
            if preference and day < preference["from_day"]:
                continue
            if local.strftime("%H:%M") < target:
                continue
            if not self.db.claim_notification(user["tg"], day):
                continue
            # Claim before sending: at most once even after a crash/ambiguous Telegram result.
            status = "sent"
            try:
                await self.send(user["tg"], local)
            except TelegramForbiddenError:
                self.db.execute("UPDATE users SET blocked=1 WHERE tg=?", (user["tg"],))
                status = "blocked"
            except Exception as exc:
                status = "uncertain"
                log.warning("reminder_failed kind=%s", type(exc).__name__)
            self.db.execute(
                "UPDATE notifications SET status=? WHERE tg=? AND day=?", (status, user["tg"], day)
            )
