import asyncio
import base64
import hashlib
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from urllib.parse import quote, urlencode

import httpx

from .db import DB


class ServiceError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Reconnect(ServiceError):
    def __init__(self):
        super().__init__(
            "Подключение YouTrack истекло. Отправь /start, чтобы подключиться заново.", 401
        )


class Uncertain(ServiceError):
    def __init__(self, op_id):
        super().__init__(
            "Результат записи пока не подтверждён. Проверим его без повторного добавления времени."
        )
        self.op_id = op_id


def api_error(status, *, work_item=False):
    if status == 401:
        return Reconnect()
    if status == 403:
        return ServiceError(
            "Не удалось записать время. Возможно, у тебя нет необходимых прав в этом проекте.",
            status,
        )
    if status == 404:
        return ServiceError("Задача больше недоступна.", status)
    if status == 400:
        return ServiceError(
            "YouTrack отклонил запрос. Проверь, включён ли учёт времени и заполнены ли обязательные атрибуты трудозатрат."
            if work_item
            else "YouTrack отклонил запрос данных. Попробуй ещё раз позже.",
            status,
        )
    return ServiceError("Не удалось связаться с YouTrack. Попробуй ещё раз позже.", status)


def date_ms(day: str) -> int:
    # YouTrack date-only fields encode the user's calendar date as midnight UTC.
    return int(
        datetime.combine(date.fromisoformat(day), datetime.min.time(), timezone.utc).timestamp()
        * 1000
    )


ISSUE_FIELDS = (
    "id,idReadable,summary,project(name),customFields(name,value(id,name,fullName,login,minutes))"
)
WORK_FIELDS = "id,text,date,author(id),duration(minutes)"
VISIBLE_STAGES = frozenset({"Ready to work", "In progress", "Ready for review"})
STAGE_QUERY = "Stage: {Ready to work}, {In progress}, {Ready for review}"


def require_visible_stage(issue):
    stage = next(
        (field.get("value") for field in issue.get("customFields", []) if field["name"] == "Stage"),
        None,
    )
    if not isinstance(stage, dict) or stage.get("name") not in VISIBLE_STAGES:
        raise ServiceError(
            "Эта задача не отображается в боте. Доступны только задачи в статусах "
            "Ready to work, In progress и Ready for review."
        )


class YouTrack:
    def __init__(self, settings, db: DB, client=None):
        self.s, self.db = settings, db
        self.http = client or httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=5), follow_redirects=False
        )
        self.locks = defaultdict(asyncio.Lock)

    def authorize_url(self, state, verifier):
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        params = dict(
            response_type="code",
            client_id=self.s.youtrack_oauth_client_id,
            redirect_uri=self.s.youtrack_oauth_redirect_uri,
            scope=self.s.youtrack_oauth_scope,
            state=state,
            code_challenge=challenge,
            code_challenge_method="S256",
            request_credentials="default",
        )
        if self.s.oauth_offline:
            params["access_type"] = "offline"
        return self.s.youtrack_hub_url + "/api/rest/oauth2/auth?" + urlencode(params)

    async def exchange(self, data):
        data = dict(data, scope=self.s.youtrack_oauth_scope)
        secret = self.s.youtrack_oauth_client_secret.get_secret_value()
        auth = httpx.BasicAuth(self.s.youtrack_oauth_client_id, secret) if secret else None
        if not secret:
            data["client_id"] = self.s.youtrack_oauth_client_id
        try:
            r = await self.http.post(
                self.s.youtrack_hub_url + "/api/rest/oauth2/token", data=data, auth=auth
            )
        except httpx.HTTPError:
            raise ServiceError("Не удалось связаться с YouTrack. Попробуй ещё раз позже.") from None
        if r.status_code in (400, 401, 403):
            raise Reconnect()
        if r.status_code != 200:
            raise api_error(r.status_code)
        try:
            token = r.json()
            if (
                not token.get("access_token")
                or str(token.get("token_type", "Bearer")).lower() != "bearer"
            ):
                raise ValueError
            return token
        except (ValueError, AttributeError):
            raise ServiceError("YouTrack вернул некорректный ответ авторизации.") from None

    async def access_token(self, tg, force=False, stale=None):
        async with self.locks[tg]:
            user = self.db.user(tg)
            row = self.db.one("SELECT * FROM credentials WHERE tg=?", (tg,))
            if not user or not user["active"] or not row:
                raise Reconnect()
            token = self.db.open(row["access"])
            if (not force and row["expires"] > time.time() + 60) or (
                force and stale and token != stale
            ):
                return token
            refresh = self.db.open(row["refresh"])
            if not refresh:
                self.db.execute("UPDATE users SET active=0 WHERE tg=?", (tg,))
                raise Reconnect()
            try:
                tokens = await self.exchange(
                    dict(grant_type="refresh_token", refresh_token=refresh)
                )
            except Reconnect:
                self.db.execute("UPDATE users SET active=0 WHERE tg=?", (tg,))
                raise
            current = self.db.user(tg)
            if not current or current["binding"] != user["binding"]:
                raise Reconnect()
            self.db.save_tokens(tg, tokens)
            return tokens["access_token"]

    async def me(self, token):
        try:
            r = await self.http.get(
                self.s.youtrack_base_url + "/api/users/me",
                params={"fields": "id,login,fullName,guest,banned"},
                headers={"Authorization": "Bearer " + token},
            )
        except httpx.HTTPError:
            raise api_error(503) from None
        if r.status_code != 200:
            raise api_error(r.status_code)
        data = r.json()
        if not data.get("id") or not data.get("login") or data.get("guest") or data.get("banned"):
            raise ServiceError("Нужен действующий личный аккаунт YouTrack.")
        return data

    async def request(self, tg, method, path, **kwargs):
        token = await self.access_token(tg)
        try:
            r = await self.http.request(
                method,
                self.s.youtrack_base_url + "/api" + path,
                headers={"Authorization": "Bearer " + token},
                **kwargs,
            )
            # Only GET is safe to replay. Never retry POST implicitly.
            if r.status_code == 401 and method == "GET":
                token = await self.access_token(tg, force=True, stale=token)
                r = await self.http.request(
                    method,
                    self.s.youtrack_base_url + "/api" + path,
                    headers={"Authorization": "Bearer " + token},
                    **kwargs,
                )
        except httpx.HTTPError:
            raise api_error(503) from None
        if not 200 <= r.status_code < 300:
            if r.status_code == 400 and method == "GET" and path == "/issues":
                raise ServiceError(
                    "Не удалось загрузить задачи: YouTrack отклонил поисковый запрос.", 400
                )
            raise api_error(
                r.status_code,
                work_item=method == "POST" and path.endswith("/timeTracking/workItems"),
            )
        try:
            return r.json()
        except ValueError:
            raise api_error(502) from None

    async def issues(self, tg, page=0, search=None):
        if page < 0:
            raise ValueError("Неверная страница.")
        # Parenthesized expressions require an explicit operator before sorting.
        query = f"(Responsible: me or {{Проверяющий}}: me) and ({STAGE_QUERY}) and sort by: updated desc"
        if search:
            # Quoted phrase prevents user input from injecting status/project filters.
            safe = search.replace("\\", "\\\\").replace('"', '\\"')
            query = f'(summary: "{safe}") and ({STAGE_QUERY}) and sort by: updated desc'
        # Ask for one extra item to decide whether the next page exists.
        return await self.request(
            tg,
            "GET",
            "/issues",
            params={
                "query": query,
                "$top": 11,
                "$skip": page * 10,
                "fields": "id,idReadable,summary",
            },
        )

    async def issue(self, tg, issue):
        return await self.request(
            tg, "GET", "/issues/" + quote(issue, safe=""), params={"fields": ISSUE_FIELDS}
        )

    async def month_work(self, tg, authors, start, end):
        if not authors:
            return []
        first, last = date_ms(start), date_ms(end)
        seen, result, skip = set(), [], 0
        while True:
            items = await self.request(
                tg,
                "GET",
                "/workItems",
                params=[
                    (
                        "fields",
                        "id,text,date,duration(minutes),author(id),issue(id,idReadable,summary,project(name))",
                    ),
                    ("$top", "100"),
                    ("$skip", str(skip)),
                    ("start", str(first)),
                    ("end", str(last)),
                    *[("author", author) for author in authors],
                ],
            )
            if not isinstance(items, list):
                raise ServiceError("YouTrack вернул некорректный отчёт.")
            if not items:
                return result
            new = 0
            for item in items:
                if not isinstance(item, dict) or not item.get("id"):
                    raise ServiceError("YouTrack вернул некорректную запись времени.")
                if item["id"] in seen:
                    continue
                seen.add(item["id"])
                new += 1
                author = (item.get("author") or {}).get("id")
                if author not in authors:
                    continue
                stamp = item.get("date")
                minutes = (item.get("duration") or {}).get("minutes")
                if (
                    not isinstance(stamp, (int, float))
                    or not isinstance(minutes, int)
                    or minutes < 0
                ):
                    raise ServiceError("YouTrack вернул некорректную дату или длительность.")
                # Explicit half-open month; API boundary inclusivity must not leak next month.
                if first <= stamp < last:
                    result.append(item)
            if not new:
                raise ServiceError("YouTrack повторил страницу отчёта. Повтори запрос позже.")
            skip += len(items)

    async def tracking(self, tg, issue):
        return await self.request(
            tg,
            "GET",
            "/issues/" + quote(issue, safe="") + "/timeTracking",
            params={"fields": "enabled"},
        )

    async def find_work(self, tg, op):
        skip = 0
        while True:
            items = await self.request(
                tg,
                "GET",
                f"/issues/{quote(op['issue'], safe='')}/timeTracking/workItems",
                params={"fields": WORK_FIELDS, "$top": 100, "$skip": skip},
            )
            for item in items:
                if item.get("text") == "tg-time:" + op["id"]:
                    if (
                        item.get("author", {}).get("id") != op["yt"]
                        or item.get("duration", {}).get("minutes") != op["minutes"]
                        or item.get("date") != date_ms(op["day"])
                    ):
                        raise ServiceError(
                            "Найдена запись с несовпадающими данными. Нужна проверка администратором."
                        )
                    return item
            if not items:
                return None
            skip += len(items)

    async def close(self):
        await self.http.aclose()


def spent(issue):
    for field in issue.get("customFields", []):
        if field["name"] in {"Spent time", "Затраченное время"}:
            value = field.get("value")
            return int(value.get("minutes", 0)) if value else 0
    return None
