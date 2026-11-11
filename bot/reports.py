"""Administrator's monthly report, using the requester's YouTrack permissions."""

import csv
import io
import re
from datetime import datetime, timezone
from urllib.parse import quote

from aiogram.types import BufferedInputFile

from .duration import human
from .free_time import month_dates
from .youtrack import Reconnect, ServiceError


def safe_cell(value):
    value = str(value)
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def report_csv(rows):
    out = io.StringIO(newline="")
    writer = csv.writer(out, delimiter=";")
    writer.writerow(
        [
            "Дата",
            "Сотрудник",
            "Логин",
            "Источник",
            "Проект",
            "Задача",
            "Название задачи",
            "Описание работы",
            "Минуты",
            "Время",
            "Ссылка",
            "ID записи",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                safe_cell(row[key])
                for key in (
                    "day",
                    "name",
                    "login",
                    "source",
                    "project",
                    "issue",
                    "title",
                    "description",
                    "minutes",
                    "duration",
                    "url",
                    "id",
                )
            ]
        )
    return out.getvalue().encode("utf-8-sig")


class Reports:
    def __init__(self, ui):
        self.ui = ui

    def authorize(self, tg):
        if not self.ui.s.report_admin(tg):
            raise ServiceError("Общий отчёт доступен только администратору бота.", 403)
        if not self.ui.db.user(tg):
            raise Reconnect()

    async def collect(self, tg, month, source="all"):
        self.authorize(tg)
        start, end, _ = month_dates(month)
        if source not in {"all", "youtrack", "free"}:
            raise ValueError("Неизвестный вид отчёта.")
        users = self.ui.db.all("SELECT tg,yt,name,login FROM users ORDER BY name,tg")
        authors = {user["yt"]: user for user in users}
        rows = []
        if source != "free":
            items = await self.ui.yt.month_work(tg, list(authors), start, end)
            for item in items:
                user = authors[item["author"]["id"]]
                issue = item.get("issue") or {}
                if not issue.get("idReadable"):
                    raise ServiceError(
                        "YouTrack не вернул задачу для записи времени. Повтори отчёт позже."
                    )
                minutes = item["duration"]["minutes"]
                description = item.get("text") or ""
                if re.fullmatch(r"tg-time:[0-9a-f]{32}", description):
                    description = ""
                rows.append(
                    dict(
                        day=datetime.fromtimestamp(item["date"] / 1000, timezone.utc)
                        .date()
                        .isoformat(),
                        name=user["name"],
                        login=user["login"],
                        source="YouTrack",
                        project=(issue.get("project") or {}).get("name", ""),
                        issue=issue["idReadable"],
                        title=issue.get("summary") or "",
                        description=description,
                        minutes=minutes,
                        duration=human(minutes),
                        url=self.ui.s.youtrack_base_url
                        + "/issue/"
                        + quote(issue["idReadable"], safe=""),
                        id="youtrack:" + item["id"],
                        owner=user["tg"],
                    )
                )
        if source != "youtrack":
            for user in users:
                for entry in self.ui.db.free_month(user["tg"], start, end):
                    rows.append(
                        dict(
                            day=entry["day"],
                            name=user["name"],
                            login=user["login"],
                            source="Вне YouTrack",
                            project="",
                            issue="",
                            title="",
                            description=entry["description"],
                            minutes=entry["minutes"],
                            duration=human(entry["minutes"]),
                            url="",
                            id="free:" + entry["id"],
                            owner=user["tg"],
                        )
                    )
        return users, sorted(rows, key=lambda r: (r["day"], r["name"], r["source"], r["id"]))

    async def show(self, tg, month=None, message=None, source="all", download=False):
        from .telegram import escape

        self.authorize(tg)
        month = month or self.ui.local.today(tg)[:7]
        _, end, previous = month_dates(month)
        try:
            users, rows = await self.collect(tg, month, source)
        except ServiceError as exc:
            if source == "free":
                raise
            reason = (
                "Обнови подключение через /start."
                if isinstance(exc, Reconnect)
                else "Попробуй позже и проверь доступ к трудозатратам."
            )
            return await self.ui.show(
                tg,
                "Не удалось получить данные YouTrack. Полный отчёт не сформирован. "
                + reason
                + "\n\nЗаписи вне YouTrack можно скачать отдельно.",
                self.ui.keys(
                    [
                        self.ui.button(
                            tg, "🔄 Повторить общий отчёт", "team_report", {"month": month}
                        )
                    ],
                    [
                        self.ui.button(
                            tg,
                            "📥 CSV: вне YouTrack",
                            "team_export",
                            {"month": month, "source": "free"},
                        )
                    ],
                ),
                message,
            )
        if download:
            label = {
                "all": "YouTrack и вне YouTrack",
                "youtrack": "YouTrack",
                "free": "вне YouTrack",
            }[source]
            return await self.ui.bot.send_document(
                tg,
                BufferedInputFile(report_csv(rows), filename=f"team-time-{month}-{source}.csv"),
                caption=f"{month}: {label}. Участников: {len(users)}, записей: {len(rows)}, всего: {human(sum(r['minutes'] for r in rows))}.\nYouTrack — в пределах твоего доступа.",
            )
        text = f"<b>📊 Общий отчёт — {month}</b>\nУчастников бота: {len(users)}\n"
        for user in users:
            own = [r for r in rows if r["owner"] == user["tg"]]
            yt = sum(r["minutes"] for r in own if r["source"] == "YouTrack")
            free = sum(r["minutes"] for r in own if r["source"] == "Вне YouTrack")
            text += f"\n<b>{escape(user['name'], 100)}</b>\nYouTrack: {human(yt)} · Вне YouTrack: {human(free)}\nИтого: <b>{human(yt + free)}</b>\n"
        text += f"\n<b>Всего: {human(sum(r['minutes'] for r in rows))}</b>\nЗаписей: {len(rows)}\n\nYouTrack: все статусы и способы внесения времени, в пределах твоего доступа. Описания и ссылки — в CSV."
        await self.ui.show(
            tg,
            text,
            self.ui.keys(
                [
                    self.ui.button(
                        tg, "📥 CSV: всё вместе", "team_export", {"month": month, "source": "all"}
                    )
                ],
                [
                    self.ui.button(
                        tg,
                        "📥 CSV: YouTrack",
                        "team_export",
                        {"month": month, "source": "youtrack"},
                    ),
                    self.ui.button(
                        tg,
                        "📥 CSV: вне YouTrack",
                        "team_export",
                        {"month": month, "source": "free"},
                    ),
                ],
                [
                    self.ui.button(tg, "⬅️ Предыдущий месяц", "team_report", {"month": previous}),
                    self.ui.button(tg, "Следующий месяц ➡️", "team_report", {"month": end[:7]}),
                ],
                [self.ui.button(tg, "Мои записи вне YouTrack", "free_report", {"month": month})],
            ),
            message,
        )
