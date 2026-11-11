import asyncio
from urllib.parse import parse_qs, urlparse

import pytest

from bot.db import digest
from bot.youtrack import Reconnect


def test_encryption(db):
    row = db.one("SELECT * FROM credentials WHERE tg=1")
    assert b"secret-access" not in row["access"]
    assert db.open(row["access"]) == "secret-access"


def test_duplicate_binding_and_disconnect(db):
    with pytest.raises(ValueError, match="другому"):
        db.bind(2, {"id": "u1", "login": "alice"}, {"access_token": "a"}, "UTC")
    token = db.action(1, "list")
    db.disconnect(1)
    assert db.user(1) is None
    assert db.one("SELECT * FROM credentials WHERE tg=1") is None
    with pytest.raises(ValueError):
        db.get_action(token, 1)
    db.bind(2, {"id": "u1", "login": "alice"}, {"access_token": "a"}, "UTC")
    assert db.user(2)["yt"] == "u1"


def test_state_cookie_expiry_and_replay(db, yt):
    state = db.new_oauth(1)
    row = db.start_oauth(state, "cookie")
    params = parse_qs(urlparse(yt.authorize_url(state, db.open(row["verifier"]))).query)
    assert params["code_challenge_method"] == ["S256"]
    assert params["access_type"] == ["offline"]
    assert params["code_challenge"][0] != db.open(row["verifier"])
    with pytest.raises(ValueError):
        db.consume_oauth(state, "wrong-cookie")
    db.consume_oauth(state, "cookie")
    with pytest.raises(ValueError):
        db.consume_oauth(state, "cookie")
    state = db.new_oauth(1)
    db.execute("UPDATE oauth SET expires=0 WHERE state=?", (digest(state),))
    with pytest.raises(ValueError):
        db.start_oauth(state, "cookie")


async def test_refresh_rotation_serialized(db, yt, backend):
    db.execute("UPDATE credentials SET expires=0")
    tokens = await asyncio.gather(yt.access_token(1), yt.access_token(1))
    assert tokens == ["new-access", "new-access"]
    assert backend.refreshes == 1
    assert db.open(db.one("SELECT refresh FROM credentials WHERE tg=1")["refresh"]) == "rotated"


async def test_refresh_failure(db, yt, backend):
    backend.failure = "refresh"
    db.execute("UPDATE credentials SET expires=0")
    with pytest.raises(Reconnect):
        await yt.access_token(1)
    assert db.user(1)["active"] == 0


async def test_missing_refresh(db, yt):
    db.execute("UPDATE credentials SET expires=0,refresh=NULL")
    with pytest.raises(Reconnect):
        await yt.access_token(1)


def test_actions_owner_and_generation(db):
    token = db.action(1, "time", {"issue": "2-1"})
    with pytest.raises(ValueError):
        db.get_action(token, 2)
    db.bind(1, {"id": "u1", "login": "alice"}, {"access_token": "x"}, "UTC")
    with pytest.raises(ValueError):
        db.get_action(token, 1)
