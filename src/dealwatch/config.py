"""Process settings from environment variables prefixed with ``DEALWATCH_`` (or a ``.env`` file).

What the owner tunes in the app (model, shops, schedule, deal threshold) lives in `settings.py`, in the database.
Everything here is fixed while the process runs: paths, the login and the token for the Claude plan.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_SECRET_KEY = "dev-secret-change-me"


class Env(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DEALWATCH_", env_file=".env", env_ignore_empty=True, extra="ignore")

    # The SQLite database and the item photos.
    data_dir: Path = Path("data")
    # Nightly copies of the database, kept apart from data_dir.
    backups_dir: Path = Path("backups")
    backups_keep: int = 30

    # One password for the login page; no password means no login (only on a machine nobody else can reach).
    auth_password: SecretStr | None = None
    # Signs the login cookie. Must be a random string of 32 characters or more once a password is set.
    secret_key: SecretStr = SecretStr(DEV_SECRET_KEY)
    # Send the login cookie over HTTPS only. On behind the HTTPS proxy on the Pi.
    cookie_secure: bool = False

    # From `make assistant-token`: signs Claude Code in with the owner's Claude plan, which runs the price checks,
    # the smart entry and Ask. Empty: those are off and the app says so; everything else works.
    claude_code_oauth_token: SecretStr | None = None

    # Daily price check and nightly backup inside the web process. Off in tests.
    scheduler: bool = True
    log_level: str = "INFO"

    @field_validator("auth_password", "secret_key", "claude_code_oauth_token", mode="before")
    @classmethod
    def _drop_inline_comment(cls, value: Any) -> Any:
        """Treat "KEY=   # note" as empty: a comment after an empty value in .env becomes the value."""
        if isinstance(value, str) and value.lstrip().startswith("#"):
            return None
        return value

    @model_validator(mode="after")
    def _login_needs_its_own_secret(self) -> Env:
        """The built-in key is public (it is in the source), so a login signed with it could be forged."""
        key = self.secret_key.get_secret_value()
        if self.auth_password and (key == DEV_SECRET_KEY or len(key) < 32):
            raise ValueError(
                "DEALWATCH_AUTH_PASSWORD is set, so DEALWATCH_SECRET_KEY must be a random string of 32 characters or"
                ' more. Generate one: python3 -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        return self

    @property
    def db_path(self) -> Path:
        return self.data_dir / "dealwatch.db"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def auth_enabled(self) -> bool:
        return self.auth_password is not None

    @property
    def token(self) -> str:
        return self.claude_code_oauth_token.get_secret_value() if self.claude_code_oauth_token else ""
