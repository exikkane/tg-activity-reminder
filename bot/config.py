from datetime import time
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    telegram_bot_token: SecretStr
    telegram_proxy_url: SecretStr = SecretStr("")
    youtrack_base_url: str
    youtrack_oauth_client_id: str
    youtrack_oauth_client_secret: SecretStr = SecretStr("")
    youtrack_oauth_redirect_uri: str
    youtrack_hub_url: str = ""
    youtrack_oauth_scope: str = "YouTrack"
    oauth_offline: bool = True
    app_base_url: str
    token_encryption_key: SecretStr
    bot_timezone: str = "Europe/Ulyanovsk"
    reminder_time: str = "20:00"
    reminder_days: str = "all"
    database_url: str = "sqlite:///./data/bot.db"
    max_duration_minutes: int = 960
    log_level: str = "INFO"
    run_bot: bool = True
    allow_local_http: bool = False
    # Optional allowlist for a small private team. Empty = any YouTrack account with access.
    allowed_telegram_ids: str = ""
    # Empty disables team reports; independent of general bot access.
    report_admin_telegram_ids: str = ""

    @model_validator(mode="after")
    def validate_settings(self):
        self.youtrack_base_url = self.youtrack_base_url.rstrip("/")
        self.app_base_url = self.app_base_url.rstrip("/")
        self.youtrack_hub_url = (self.youtrack_hub_url or self.youtrack_base_url + "/hub").rstrip(
            "/"
        )
        for url in (
            self.youtrack_base_url,
            self.app_base_url,
            self.youtrack_hub_url,
            self.youtrack_oauth_redirect_uri,
        ):
            p = urlparse(url)
            local = self.allow_local_http and p.hostname in {"localhost", "127.0.0.1"}
            if (
                not p.netloc
                or p.username
                or p.password
                or p.query
                or p.fragment
                or (p.scheme != "https" and not (local and p.scheme == "http"))
            ):
                raise ValueError("Use HTTPS URLs without credentials, query or fragment")
        if self.youtrack_oauth_redirect_uri != self.app_base_url + "/oauth/callback":
            raise ValueError("Redirect URI must be APP_BASE_URL/oauth/callback")
        ZoneInfo(self.bot_timezone)
        time.fromisoformat(self.reminder_time)
        if len(self.reminder_time) != 5:
            raise ValueError("REMINDER_TIME must be HH:MM")
        if self.reminder_days != "all" and not set(self.reminder_days.split(",")) <= {
            "mon",
            "tue",
            "wed",
            "thu",
            "fri",
            "sat",
            "sun",
        }:
            raise ValueError("REMINDER_DAYS must be all or mon,tue,...")
        if not 1 <= self.max_duration_minutes <= 1440:
            raise ValueError("MAX_DURATION_MINUTES must be between 1 and 1440")
        Fernet(self.token_encryption_key.get_secret_value().encode())
        if not self.database_url.startswith("sqlite:///"):
            raise ValueError("Only sqlite:/// is supported")
        for value in filter(None, self.allowed_telegram_ids.split(",")):
            int(value)
        for value in filter(None, self.report_admin_telegram_ids.split(",")):
            int(value)
        return self

    @property
    def db_path(self) -> Path:
        return Path(self.database_url.removeprefix("sqlite:///"))

    def allowed(self, tg: int) -> bool:
        return not self.allowed_telegram_ids or tg in {
            int(v) for v in self.allowed_telegram_ids.split(",")
        }

    def report_admin(self, tg: int) -> bool:
        return tg in {int(v) for v in self.report_admin_telegram_ids.split(",") if v.strip()}
