import asyncio
from datetime import datetime, timezone

import pytest

from bot.db import DB
from bot.services import WorkService
from bot.youtrack import ServiceError, Uncertain, date_ms, spent


@pytest.mark.parametrize("minutes", [30, 60, 90])
async def test_additive_correct_author_date(work, backend, yt, minutes):
    now = datetime(2026, 10, 29, 21, 15, tzinfo=timezone.utc)
    op = await work.record(1, "flow", "2-1", minutes, now)
    assert op["status"] == "done"
    item = backend.items[0]
    assert item["author"] == {"id": "u1"}
    assert item["date"] == date_ms("2026-10-30")
    assert spent(await yt.issue(1, "2-1")) == 480 + minutes
    assert len([r for r in backend.calls if r.method == "POST"]) == 1
    assert backend.calls[-2].url.path.endswith("/workItems")


async def test_duplicate_and_intentional_repeat(work, backend):
    first, duplicate = await asyncio.gather(
        work.record(1, "flow", "2-1", 90), work.record(1, "flow", "2-1", 90)
    )
    assert first["id"] == duplicate["id"]
    assert backend.posts == 1
    await work.record(1, "another-flow", "2-1", 30)
    assert backend.posts == 2
    assert backend.total == 600


async def test_lost_response_reconcile(work, backend):
    backend.failure = "timeout"
    with pytest.raises(Uncertain) as exc:
        await work.record(1, "flow", "2-1", 90)
    result = await work.reconcile(1, exc.value.op_id)
    assert result["status"] == "done"
    assert backend.posts == 1


async def test_unknown_absent_never_resends(work, backend):
    backend.failure = "timeout-before"
    with pytest.raises(Uncertain):
        await work.record(1, "flow", "2-1", 90)
    with pytest.raises(Uncertain):
        await work.record(1, "another-flow", "2-1", 90)
    assert backend.posts == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429])
async def test_definitive_failure(work, backend, db, status):
    backend.failure = status
    with pytest.raises(ServiceError):
        await work.record(1, "flow", "2-1", 30)
    assert db.one("SELECT status FROM operations")["status"] == "rejected"
    assert not backend.items


async def test_time_tracking_disabled(work, backend):
    backend.enabled = False
    with pytest.raises(ServiceError, match="отключён"):
        await work.record(1, "flow", "2-1", 30)
    assert backend.posts == 0


async def test_issue_missing(work, backend):
    backend.failure = "missing"
    with pytest.raises(ServiceError, match="недоступна"):
        await work.record(1, "flow", "2-1", 30)
    assert backend.posts == 0


async def test_reconcile_paginates_all_work_items(work, backend):
    backend.items = [{"id": str(i), "text": "other"} for i in range(205)]
    backend.failure = "timeout"
    with pytest.raises(Uncertain) as exc:
        await work.record(1, "flow", "2-1", 30)
    assert (await work.reconcile(1, exc.value.op_id))["status"] == "done"
    assert backend.posts == 1


async def test_restart_retains_operations(settings, db, yt, backend):
    service = WorkService(settings, db, yt)
    backend.failure = "timeout"
    with pytest.raises(Uncertain):
        await service.record(1, "flow", "2-1", 30)
    reopened = DB(settings.db_path, settings.token_encryption_key.get_secret_value())
    try:
        service = WorkService(settings, reopened, yt)
        assert (await service.record(1, "flow", "2-1", 30))["status"] == "done"
        assert reopened.user(1)["login"] == "alice"
        assert backend.posts == 1
    finally:
        reopened.close()


async def test_wrong_author_is_not_success(work, backend):
    backend.failure = "timeout"
    with pytest.raises(Uncertain) as exc:
        await work.record(1, "flow", "2-1", 30)
    backend.items[0]["author"] = {"id": "other"}
    with pytest.raises(ServiceError, match="несовпадающими"):
        await work.reconcile(1, exc.value.op_id)
