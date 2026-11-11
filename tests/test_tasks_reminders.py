from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from bot.services import ReminderService


@pytest.mark.parametrize("count", [0, 1, 11, 105])
async def test_pages_all_projects_with_stage_filter(yt, backend, count):
    backend.issues = [
        {
            "id": str(i),
            "idReadable": f"{'CP' if i % 2 else 'ADDON'}-{i}",
            "summary": "Рабочая задача",
        }
        for i in range(count)
    ]
    seen = []
    for page in range(count // 10 + 1):
        values = await yt.issues(1, page)
        seen.extend(v["id"] for v in values[:10])
        if len(values) <= 10:
            break
    assert seen == [str(i) for i in range(count)]
    query = backend.calls[0].url.params["query"]
    assert "Responsible: me or {Проверяющий}: me" in query
    assert "sort by: updated desc" in query
    assert "and (Stage: {Ready to work}, {In progress}, {Ready for review}) and" in query
    assert "Unresolved" not in query and "project:" not in query


async def test_search_without_assignment_filter(yt, backend):
    await yt.issues(1, 0, "Интеграция MAXMA")
    assert (
        backend.calls[-1].url.params["query"]
        == '(summary: "Интеграция MAXMA") and (Stage: {Ready to work}, {In progress}, {Ready for review}) and sort by: updated desc'
    )
    assert (await yt.issue(1, "CP-145"))["idReadable"] == "CP-123"


async def test_reminder_time_restart_and_multiple_users(settings, db):
    db.bind(2, {"id": "u2", "login": "bob"}, {"access_token": "b"}, "UTC")
    send = AsyncMock()
    service = ReminderService(settings, db, send)
    await service.tick(datetime(2026, 10, 29, 15, 59, tzinfo=timezone.utc))
    assert send.await_count == 0
    await service.tick(datetime(2026, 10, 29, 16, 0, tzinfo=timezone.utc))
    assert send.await_count == 1
    service = ReminderService(settings, db, send)
    await service.tick(datetime(2026, 10, 29, 16, 30, tzinfo=timezone.utc))
    assert send.await_count == 1
    await service.tick(datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc))
    assert send.await_count == 2
    await service.tick(datetime(2026, 10, 30, 16, 0, tzinfo=timezone.utc))
    assert send.await_count == 3


async def test_blocked_and_inactive(settings, db):
    send = AsyncMock(
        side_effect=TelegramForbiddenError(
            method=SendMessage(chat_id=1, text="test"), message="blocked"
        )
    )
    service = ReminderService(settings, db, send)
    await service.tick(datetime(2026, 10, 29, 16, tzinfo=timezone.utc))
    await service.tick(datetime(2026, 10, 30, 16, tzinfo=timezone.utc))
    assert send.await_count == 1
    assert db.user(1)["blocked"] == 1
    db.execute("UPDATE users SET blocked=0,active=0")
    await service.tick(datetime(2026, 10, 31, 16, tzinfo=timezone.utc))
    assert send.await_count == 1


async def test_days_and_ambiguous_delivery(settings, db):
    settings.reminder_days = "mon"
    send = AsyncMock(side_effect=TimeoutError)
    service = ReminderService(settings, db, send)
    await service.tick(datetime(2026, 10, 29, 16, tzinfo=timezone.utc))
    assert send.await_count == 0
    now = datetime(2026, 11, 2, 16, tzinfo=timezone.utc)
    await service.tick(now)
    await service.tick(now)
    assert send.await_count == 1
    assert db.one("SELECT status FROM notifications")["status"] == "uncertain"
