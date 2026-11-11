import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from bot.app import create_app
from bot.telegram import UI


@pytest.fixture
def ui(settings, db, yt, work):
    bot = SimpleNamespace(
        send_message=AsyncMock(),
        edit_message_text=AsyncMock(),
        set_my_commands=AsyncMock(),
        send_document=AsyncMock(),
    )
    return UI(settings, db, yt, work, bot)


def message(text, user=1):
    return SimpleNamespace(text=text, from_user=SimpleNamespace(id=user), message_id=100)


def callback(token, user=1):
    return SimpleNamespace(
        data=token, from_user=SimpleNamespace(id=user), answer=AsyncMock(), message=message("")
    )


async def test_first_and_repeat_start(ui, db):
    db.disconnect(1)
    await ui.command(message("/start"))
    sent = ui.bot.send_message.call_args
    assert "Добро пожаловать" in sent.args[1]
    assert "/connect?ticket=" in sent.kwargs["reply_markup"].inline_keyboard[0][0].url
    db.bind(1, {"id": "u1", "login": "alice"}, {"access_token": "a"}, "UTC")
    await ui.command(message("/start"))
    assert "Мои задачи" in ui.bot.send_message.call_args.args[1]


async def test_account_and_disconnect_confirmation(ui, db):
    await ui.command(message("/account"))
    assert "Alice" in ui.bot.send_message.call_args.args[1]
    await ui.command(message("/disconnect"))
    assert db.user(1)
    token = ui.bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    await ui.callback(callback(token))
    assert not db.user(1)


async def test_time_dialog_invalid_input_double_tap_and_duplicate_message(ui, db, backend):
    token = db.action(1, "time", {"issue": "2-1"})
    await ui.callback(callback(token))
    await ui.callback(callback(token))
    flow = db.conversation(1)["flow"]
    assert flow == token
    await ui.text(message("45"))
    assert db.conversation(1)["flow"] == flow
    assert backend.posts == 0
    await ui.text(message("1.5ч"))
    await ui.text(message("1.5ч"))
    assert backend.posts == 1
    assert db.conversation(1) is None
    await ui.callback(callback(token))
    assert backend.posts == 1
    buttons = ui.bot.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard
    await ui.callback(callback(buttons[0][0].callback_data))
    await ui.text(message("30м"))
    assert backend.posts == 2


@pytest.mark.parametrize(
    "stage", ["Ready to work", "In progress", "Ready for review", "Done", "Closed"]
)
async def test_search_cancel_and_unassigned_issue(ui, db, backend, stage):
    backend.stage = stage
    await ui.callback(callback(db.action(1, "search")))
    await ui.text(message("CP-145"))
    if stage in {"Done", "Closed"}:
        assert "не отображается" in ui.bot.send_message.call_args.args[1]
        assert "CP-123" not in ui.bot.send_message.call_args.args[1]
    else:
        assert "CP-123" in ui.bot.send_message.call_args.args[1]
    assert db.conversation(1)["mode"] == "search"
    await ui.callback(callback(db.action(1, "list")))
    assert db.conversation(1) is None


async def test_long_titles_pagination_and_callbacks(ui, backend):
    backend.issues = [
        {"id": str(i), "idReadable": f"CP-{i}", "summary": "<&>" * 2000} for i in range(11)
    ]
    await ui.list_tasks(1)
    sent = ui.bot.send_message.call_args
    assert "<&>" not in sent.args[1]
    keys = sent.kwargs["reply_markup"].inline_keyboard
    assert len(keys) == 15
    assert any(row[0].text == "Вперёд ➡️" for row in keys)
    for row in keys:
        for button in row:
            assert len(button.callback_data.encode()) <= 64


async def test_callback_other_user_rejected(ui, db, backend):
    await ui.callback(callback(db.action(1, "time", {"issue": "2-1"}), 2))
    assert backend.posts == 0
    assert "устарела" in ui.bot.edit_message_text.call_args.kwargs["text"]


async def test_commands_menu(ui):
    await ui.setup()
    assert [c.command for c in ui.bot.set_my_commands.call_args.args[0]] == [
        "start",
        "tasks",
        "free",
        "report",
        "teamreport",
        "settings",
        "cancel",
        "account",
        "disconnect",
    ]


async def test_oauth_callback_cookie_replay_and_health(settings, backend):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        await app.state.yt.http.aclose()
        app.state.yt.http = httpx.AsyncClient(transport=httpx.MockTransport(backend.handle))
        app.state.ui.show = AsyncMock()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://bot.test"
        ) as browser:
            assert (await browser.get("/healthz")).json() == {"status": "ok", "bot_enabled": False}
            state = app.state.db.new_oauth(1)
            start = await browser.get("/connect", params={"ticket": state})
            assert start.status_code == 302
            assert "code_challenge_method=S256" in start.headers["location"]
            assert (
                "HttpOnly" in start.headers["set-cookie"]
                and "Secure" in start.headers["set-cookie"]
            )
            callback_response = await browser.get(
                "/oauth/callback", params={"state": state, "code": "valid-code"}
            )
            assert callback_response.status_code == 200
            assert app.state.db.user(1)["yt"] == "u1"
            assert app.state.ui.show.await_count == 1
            assert (
                await browser.get("/oauth/callback", params={"state": state, "code": "valid-code"})
            ).status_code == 400
            assert len([c for c in backend.calls if c.url.path.endswith("/oauth2/token")]) == 1
            assert callback_response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("problem", ["denied", "cookie", "expired", "duplicate-account", "guest"])
async def test_oauth_failures(settings, backend, problem):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        await app.state.yt.http.aclose()
        app.state.yt.http = httpx.AsyncClient(transport=httpx.MockTransport(backend.handle))
        app.state.ui.show = AsyncMock()
        if problem == "duplicate-account":
            app.state.db.bind(2, backend.user, {"access_token": "a"}, "UTC")
        if problem == "guest":
            backend.user["guest"] = True
        state = app.state.db.new_oauth(1)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://bot.test"
        ) as browser:
            await browser.get("/connect", params={"ticket": state})
            if problem == "cookie":
                browser.cookies.clear()
            if problem == "expired":
                app.state.db.execute("UPDATE oauth SET expires=0")
            response = await browser.get(
                "/oauth/callback",
                params={
                    "state": state,
                    "code": "x",
                    "error": "denied" if problem == "denied" else "",
                },
            )
            assert response.status_code == 400
            assert app.state.db.user(1) is None


async def test_poll_offset_persists_before_next_request(ui, db):
    from aiogram.types import Update

    updates = [Update(update_id=10), Update(update_id=11)]
    ui.dp.feed_update = AsyncMock()
    ui.bot.get_updates = AsyncMock(side_effect=[updates, asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await ui.poll()
    assert ui.dp.feed_update.await_count == 2
    assert db.one("SELECT value FROM meta WHERE key='telegram_offset'")["value"] == "12"
    assert ui.bot.get_updates.call_args.kwargs["offset"] == 12
    ui.bot.get_updates = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await ui.poll()
    assert ui.bot.get_updates.call_args.kwargs["offset"] == 12


async def test_failed_poller_requests_graceful_restart(settings, monkeypatch):
    import signal

    from bot.services import ReminderService

    signals = []
    settings.run_bot = True
    monkeypatch.setattr(UI, "poll", AsyncMock(side_effect=RuntimeError("test")))
    monkeypatch.setattr(ReminderService, "tick", AsyncMock())
    monkeypatch.setattr("bot.app.os.kill", lambda pid, sig: signals.append(sig))
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert signals == [signal.SIGTERM]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://bot.test"
        ) as client:
            assert (await client.get("/healthz")).status_code == 503


async def test_oauth_cookie_path_with_reverse_proxy_prefix(settings, backend):
    settings.app_base_url = "https://bot.test/timebot"
    settings.youtrack_oauth_redirect_uri = "https://bot.test/timebot/oauth/callback"
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://bot.test"
        ) as browser:
            state = app.state.db.new_oauth(1)
            response = await browser.get("/connect", params={"ticket": state})
            assert "Path=/timebot/oauth/callback" in response.headers["set-cookie"]


async def test_free_work_description_duration_duplicates_and_no_youtrack(
    ui, db, backend, monkeypatch
):
    monkeypatch.setattr(ui.local, "today", lambda tg: "2026-11-06")
    token = db.action(1, "free")
    await ui.callback(callback(token))
    await ui.text(message("Обсуждение <рекламы>\nс клиентом"))
    await ui.callback(callback(token))  # Same button keeps the pending description.
    assert db.conversation(1)["mode"] == "free_time"
    await ui.text(message("Обсуждение <рекламы>\nс клиентом"))  # Duplicate update.
    invalid = message("45")
    invalid.message_id = 101
    await ui.text(invalid)
    assert db.conversation(1)["mode"] == "free_time"
    duration = message("1:30")
    duration.message_id = 102
    await ui.text(duration)
    await ui.text(duration)
    await ui.callback(callback(token))
    rows = db.free_month(1, "2026-11-01", "2026-12-01")
    assert len(rows) == 1 and rows[0]["minutes"] == 90
    assert rows[0]["description"] == "Обсуждение <рекламы>\nс клиентом"
    assert rows[0]["day"] == "2026-11-06"
    assert db.conversation(1) is None
    assert "&lt;рекламы&gt;" in ui.bot.edit_message_text.call_args.kwargs["text"]
    assert backend.calls == [] and backend.posts == 0


async def test_free_history_restart_reconnect_month_scope_and_csv(ui, db, settings):
    from bot.db import DB

    db.save_free_entry(1, "a", '=HYPERLINK("example")\nразбор', 30, "2026-11-01", "UTC")
    db.save_free_entry(1, "b", "Клиент", 90, "2026-11-30", "UTC")
    db.save_free_entry(1, "c", "Прошлый месяц", 20, "2026-10-31", "UTC")
    db.save_free_entry(2, "other", "Чужая запись", 400, "2026-11-01", "UTC")
    db.disconnect(1)
    db.bind(1, {"id": "u1", "login": "alice"}, {"access_token": "a"}, "UTC")
    reopened = DB(settings.db_path, settings.token_encryption_key.get_secret_value())
    try:
        assert sum(r["minutes"] for r in reopened.free_month(1, "2026-11-01", "2026-12-01")) == 120
    finally:
        reopened.close()
    await ui.command(message("/report 2026-11"))
    body = ui.bot.send_message.call_args.args[1]
    assert "2ч" in body or "2 ч" in body
    assert "Записей: 2" in body and "400" not in body
    await ui.local.report(1, "2026-11", download=True)
    payload = ui.bot.send_document.call_args.args[1].data.decode("utf-8-sig")
    assert "'=" in payload and "Чужая" not in payload and "Прошлый месяц" not in payload
    assert "Клиент" in payload and "2026-11-30" in payload


async def test_free_entry_survives_failed_telegram_confirmation(ui, db):
    await ui.command(message("/free"))
    await ui.text(message("Анализ метрики"))
    duration = message("30м")
    duration.message_id = 101
    ui.bot.send_message.side_effect = RuntimeError("Telegram unavailable")
    with pytest.raises(RuntimeError):
        await ui.text(duration)
    assert db.one("SELECT count(*) AS n FROM free_entries")["n"] == 1
    assert db.conversation(1) is None
    ui.bot.send_message.side_effect = None
    await ui.text(duration)
    assert db.one("SELECT count(*) AS n FROM free_entries")["n"] == 1


async def test_free_work_expired_oauth_cancel_and_other_user(ui, db, backend):
    db.execute("UPDATE users SET active=0 WHERE tg=1")
    token = db.action(1, "free")
    await ui.callback(callback(token, user=2))
    assert db.conversation(2) is None
    await ui.callback(callback(token))
    await ui.text(message("Развитие бизнеса"))
    await ui.command(message("/cancel"))
    assert db.conversation(1) is None
    assert db.one("SELECT count(*) AS n FROM free_entries")["n"] == 0
    await ui.command(message("/report 2026-11"))
    assert "Записей: 0" in ui.bot.send_message.call_args.args[1]
    assert backend.calls == []


async def test_reminder_has_free_work_when_youtrack_unavailable(ui):
    from datetime import datetime

    from bot.youtrack import ServiceError

    ui.yt.issues = AsyncMock(side_effect=ServiceError("offline"))
    await ui.reminder(1, datetime(2026, 11, 6, 20, 0))
    buttons = ui.bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard
    assert any("без YouTrack" in b.text for row in buttons for b in row)


async def test_free_description_limit_and_month_validation(ui, db):
    await ui.command(message("/free"))
    await ui.text(message("x" * 1001))
    assert db.conversation(1)["mode"] == "free_description"
    await ui.command(message("/report 2026-13"))
    assert "формате" in ui.bot.send_message.call_args.args[1]
    await ui.command(message("/report 2026-11 extra"))
    assert "формате" in ui.bot.send_message.call_args.args[1]


async def test_team_report_real_work_items_plus_free_without_ledger_duplicates(
    ui, db, backend, settings
):
    import csv
    import io

    from bot.youtrack import date_ms

    settings.report_admin_telegram_ids = "1"
    db.bind(
        2, {"id": "u2", "login": "bob", "fullName": "Bob"}, {"access_token": "other-secret"}, "UTC"
    )
    db.save_free_entry(1, "fa", "Клиент\nобсуждение", 30, "2026-11-01", "UTC")
    db.save_free_entry(2, "fb", "=SUM(1;2)", 60, "2026-11-30", "UTC")
    db.save_free_entry(2, "old", "Октябрь", 900, "2026-10-31", "UTC")
    backend.stage = "Done"  # Completed tasks must still be reported.
    backend.items = [
        dict(
            id="work1",
            date=date_ms("2026-11-01"),
            author={"id": "u1"},
            duration={"minutes": 120},
            text="tg-time:" + "a" * 32,
            issue=backend.issue(),
        ),
        dict(
            id="work2",
            date=date_ms("2026-11-30"),
            author={"id": "u2"},
            duration={"minutes": 45},
            text="Реклама",
            issue=backend.issue(),
        ),
    ]
    db.prepare_operation("already-in-youtrack", 1, "2-1", 120, "2026-11-01")
    await ui.command(message("/report 2026-11"))
    body = ui.bot.send_message.call_args.args[1]
    assert "Alice" in body and "Bob" in body and "4 ч 15 мин" in body and "Записей: 4" in body
    assert "Общий отчёт" in body
    for req in backend.calls:
        assert req.headers["Authorization"] == "Bearer secret-access"
        assert req.url.path == "/api/workItems"
        assert set(req.url.params.get_list("author")) == {"u1", "u2"}
        assert "query" not in req.url.params
    keys = ui.bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard
    await ui.callback(callback(keys[0][0].callback_data))
    upload = ui.bot.send_document.call_args.args[1]
    rows = list(csv.DictReader(io.StringIO(upload.data.decode("utf-8-sig")), delimiter=";"))
    assert len(rows) == 4 and sum(int(r["Минуты"]) for r in rows) == 255
    assert {r["Сотрудник"] for r in rows} == {"Alice", "Bob"}
    assert any(r["Описание работы"] == "Клиент\nобсуждение" for r in rows)
    assert any(r["Описание работы"].startswith("'=") for r in rows)
    assert any(r["Ссылка"] == "https://yt.test/issue/CP-123" for r in rows)
    assert "tg-time:" not in upload.data.decode("utf-8-sig")
    assert backend.posts == 0


async def test_team_export_filters_and_empty_csv(ui, db, backend, settings):
    import csv
    import io

    from bot.youtrack import date_ms

    settings.report_admin_telegram_ids = "1"
    db.save_free_entry(1, "f", "Свободная", 30, "2026-11-01", "UTC")
    backend.items = [
        dict(
            id="w",
            date=date_ms("2026-11-02"),
            author={"id": "u1"},
            duration={"minutes": 40},
            issue=backend.issue(),
        )
    ]
    for source, count, minutes in [("free", 1, 30), ("youtrack", 1, 40), ("all", 2, 70)]:
        await ui.reports.show(1, "2026-11", source=source, download=True)
        rows = list(
            csv.DictReader(
                io.StringIO(ui.bot.send_document.call_args.args[1].data.decode("utf-8-sig")),
                delimiter=";",
            )
        )
        assert len(rows) == count and sum(int(r["Минуты"]) for r in rows) == minutes
    await ui.reports.show(1, "2026-12", download=True)
    assert len(ui.bot.send_document.call_args.args[1].data.decode("utf-8-sig").splitlines()) == 1


async def test_team_report_denies_other_users_and_revoked_admin(ui, db, backend, settings):
    db.bind(2, {"id": "u2", "login": "bob"}, {"access_token": "b"}, "UTC")
    settings.report_admin_telegram_ids = "1"
    token = db.action(1, "team_export", {"month": "2026-11"})
    await ui.callback(callback(token, user=2))
    await ui.command(message("/teamreport 2026-11", user=2))
    assert "только администратору" in ui.bot.send_message.call_args.args[1]
    await ui.callback(callback(db.action(2, "team_export", {"month": "2026-11"}), user=2))
    assert "только администратору" in ui.bot.edit_message_text.call_args.kwargs["text"]
    settings.report_admin_telegram_ids = ""
    await ui.callback(callback(token))
    ui.bot.send_document.assert_not_awaited()
    assert backend.calls == []


async def test_team_report_fails_closed_but_free_export_available(ui, db, settings):
    from bot.youtrack import Reconnect

    settings.report_admin_telegram_ids = "1"
    db.save_free_entry(1, "f", "Локальная работа", 30, "2026-11-01", "UTC")
    ui.yt.month_work = AsyncMock(side_effect=Reconnect())
    await ui.reports.show(1, "2026-11", download=True)
    ui.bot.send_document.assert_not_awaited()
    assert "Полный отчёт не сформирован" in ui.bot.send_message.call_args.args[1]
    key = ui.bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard[1][0]
    await ui.callback(callback(key.callback_data))
    assert "Локальная работа" in ui.bot.send_document.call_args.args[1].data.decode("utf-8-sig")
    assert ui.yt.month_work.await_count == 1


async def test_team_work_pagination_dedup_dates_authors_and_short_pages(yt, backend):
    from bot.youtrack import date_ms

    base = dict(
        date=date_ms("2026-11-01"),
        author={"id": "u1"},
        duration={"minutes": 1},
        issue=backend.issue(),
    )
    pages = [
        [dict(base, id=str(i)) for i in range(42)],
        [
            dict(base, id="41"),
            dict(base, id="42"),
            dict(base, id="next", date=date_ms("2026-12-01")),
            dict(base, id="old", date=date_ms("2026-10-31")),
            dict(base, id="outside", author={"id": "u3"}),
        ],
        [],
    ]
    yt.request = AsyncMock(side_effect=pages)
    rows = await yt.month_work(1, ["u1", "u2"], "2026-11-01", "2026-12-01")
    assert len(rows) == 43 and sum(r["duration"]["minutes"] for r in rows) == 43
    assert [dict(c.kwargs["params"])["$skip"] for c in yt.request.call_args_list] == [
        "0",
        "42",
        "47",
    ]
    assert all("query" not in dict(c.kwargs["params"]) for c in yt.request.call_args_list)


async def test_team_work_repeated_page_or_failed_page_never_partial(yt, backend):
    from bot.youtrack import ServiceError, date_ms

    item = dict(
        id="w",
        date=date_ms("2026-11-01"),
        author={"id": "u1"},
        duration={"minutes": 1},
        issue=backend.issue(),
    )
    for second in ([item], ServiceError("offline")):
        yt.request = AsyncMock(side_effect=[[item], second])
        with pytest.raises(ServiceError):
            await yt.month_work(1, ["u1"], "2026-11-01", "2026-12-01")


async def test_personal_reminder_settings_dialog_validation_and_persistence(
    ui, db, settings, monkeypatch
):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from bot.db import DB

    monkeypatch.setattr(
        ui.preferences,
        "now",
        lambda tg: datetime(2026, 11, 14, 12, 0, tzinfo=ZoneInfo("Europe/Ulyanovsk")),
    )
    await ui.command(message("/settings"))
    assert "20:00" in ui.bot.send_message.call_args.args[1]
    key = ui.bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
    await ui.callback(callback(key.callback_data))
    for invalid in ("24:00", "19:60", "9:30", "19:30:00", "18ч", "-1:00"):
        await ui.text(message(invalid))
        assert db.reminder_preference(1) is None
        assert db.conversation(1)["mode"] == "settings_time"
    await ui.text(message("19:30"))
    assert db.reminder_preference(1)["time"] == "19:30"
    assert db.reminder_preference(1)["from_day"] == "2026-11-14"
    assert db.conversation(1) is None
    assert db.reminder_preference(2) is None
    db.disconnect(1)
    db.bind(1, {"id": "u1", "login": "alice"}, {"access_token": "a"}, "Europe/Ulyanovsk")
    reopened = DB(settings.db_path, settings.token_encryption_key.get_secret_value())
    try:
        assert reopened.reminder_preference(1)["time"] == "19:30"
    finally:
        reopened.close()


async def test_settings_past_time_and_already_sent_take_effect_tomorrow(ui, db, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(
        ui.preferences,
        "now",
        lambda tg: datetime(2026, 11, 14, 20, 0, tzinfo=ZoneInfo("Europe/Ulyanovsk")),
    )
    for value in ("00:00", "19:30", "20:00"):
        await ui.command(message("/settings " + value))
        assert db.reminder_preference(1)["from_day"] == "2026-11-15"
        assert "15.11.2026" in ui.bot.send_message.call_args.args[1]
    await ui.command(message("/settings 23:59"))
    assert db.reminder_preference(1)["from_day"] == "2026-11-14"
    db.claim_notification(1, "2026-11-14")
    await ui.command(message("/settings 23:00"))
    assert db.reminder_preference(1)["from_day"] == "2026-11-15"


async def test_settings_cancel_foreign_button_and_expired_oauth(ui, db, backend):
    token = db.action(1, "settings_time")
    await ui.callback(callback(token, user=2))
    assert db.conversation(1) is None and db.conversation(2) is None
    db.execute("UPDATE users SET active=0 WHERE tg=1")
    await ui.callback(callback(token))
    assert db.conversation(1)["mode"] == "settings_time"
    cancel = ui.bot.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
    await ui.callback(callback(cancel.callback_data))
    assert db.conversation(1) is None and db.reminder_preference(1) is None
    await ui.command(message("/settings 21:15"))
    assert db.reminder_preference(1)["time"] == "21:15"
    assert "обнови подключение" in ui.bot.send_message.call_args.args[1]
    assert not backend.calls
