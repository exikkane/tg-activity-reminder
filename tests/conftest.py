import httpx
import pytest
from cryptography.fernet import Fernet

from bot.config import Settings
from bot.db import DB
from bot.services import WorkService
from bot.youtrack import YouTrack


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        telegram_bot_token="123456789:TEST_ONLY_NOT_A_REAL_TOKEN",
        youtrack_base_url="https://yt.test",
        youtrack_oauth_client_id="client",
        youtrack_oauth_redirect_uri="https://bot.test/oauth/callback",
        app_base_url="https://bot.test",
        token_encryption_key=Fernet.generate_key().decode(),
        database_url="sqlite:///" + str(tmp_path / "bot.db"),
        run_bot=False,
    )


@pytest.fixture
def db(settings):
    db = DB(settings.db_path, settings.token_encryption_key.get_secret_value())
    db.bind(
        1,
        {"id": "u1", "login": "alice", "fullName": "Alice"},
        {"access_token": "secret-access", "refresh_token": "secret-refresh", "expires_in": 3600},
        settings.bot_timezone,
    )
    yield db
    db.close()


class FakeYouTrack:
    def __init__(self):
        self.items = []
        self.issues = []
        self.calls = []
        self.posts = 0
        self.failure = None
        self.enabled = True
        self.total = 480
        self.stage = "In progress"
        self.refreshes = 0
        self.user = {"id": "u1", "login": "alice", "fullName": "Alice"}

    def issue(self, id="2-1"):
        return {
            "id": id,
            "idReadable": "CP-123",
            "summary": "<Task & title>",
            "project": {"name": "One"},
            "customFields": [
                {"name": "Stage", "value": {"name": self.stage}},
                {"name": "Spent time", "value": {"minutes": self.total}},
                {"name": "Responsible", "value": {"id": "u1", "fullName": "Alice"}},
            ],
            "timeTracking": {"enabled": self.enabled},
        }

    async def handle(self, request):
        import json

        self.calls.append(request)
        path = request.url.path
        if path.endswith("/oauth2/token"):
            self.refreshes += 1
            if self.failure == "refresh":
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200,
                json={"access_token": "new-access", "refresh_token": "rotated", "expires_in": 3600},
            )
        if path.endswith("/users/me"):
            return httpx.Response(200, json=self.user)
        if path.endswith("/workItems"):
            if request.method == "POST":
                self.posts += 1
                body = json.loads(request.content)
                if isinstance(self.failure, int):
                    return httpx.Response(self.failure, json={"error": "denied"})
                item = dict(body, id=str(len(self.items)))
                if self.failure != "timeout-before":
                    self.items.append(item)
                    self.total += body["duration"]["minutes"]
                if self.failure in {"timeout", "timeout-before"}:
                    raise httpx.ReadTimeout("simulated lost response", request=request)
                return httpx.Response(200, json=item)
            skip, top = (
                int(request.url.params.get("$skip", 0)),
                int(request.url.params.get("$top", 42)),
            )
            return httpx.Response(200, json=self.items[skip : skip + top])
        if path.endswith("/timeTracking"):
            return httpx.Response(200, json={"enabled": self.enabled})
        if path == "/api/issues":
            skip, top = (
                int(request.url.params.get("$skip", 0)),
                int(request.url.params.get("$top", 42)),
            )
            return httpx.Response(200, json=self.issues[skip : skip + top])
        if path.startswith("/api/issues/"):
            if self.failure == "missing":
                return httpx.Response(404, json={})
            return httpx.Response(200, json=self.issue())
        raise AssertionError(f"Unexpected request: {request.method} {path}")


@pytest.fixture
def backend():
    return FakeYouTrack()


@pytest.fixture
async def yt(settings, db, backend):
    client = httpx.AsyncClient(transport=httpx.MockTransport(backend.handle))
    result = YouTrack(settings, db, client)
    yield result
    await result.close()


@pytest.fixture
def work(settings, db, yt):
    return WorkService(settings, db, yt)
