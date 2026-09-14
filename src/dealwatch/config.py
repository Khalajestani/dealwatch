"""Process-level configuration: secrets, paths, price tables.

Behavioural settings that the user edits in the UI live in `settings.py` (stored in the DB).
Everything here comes from environment variables / `.env` and never changes at runtime.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# USD per 1M tokens (input, output). Only used for cost *estimates* shown in the UI/reports.
# Update when Anthropic pricing changes: https://platform.claude.com/docs/en/about-claude/pricing
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-fable-5-1": (10.00, 50.00),
}
WEB_SEARCH_USD_PER_SEARCH = 0.01  # $10 per 1,000 searches
CACHE_READ_MULTIPLIER = 0.10  # cache hits cost ~10% of a normal input token
CACHE_WRITE_MULTIPLIER = 1.25  # cache writes cost ~125% of a normal input token

# Shown in the Settings page. The first entry is the default.
MODEL_CHOICES: list[tuple[str, str]] = [
    ("claude-opus-5", "Claude Opus 5 - best judgement, $5/$25 per 1M tokens"),
    ("claude-sonnet-5", "Claude Sonnet 5 - cheaper, $2/$10 per 1M tokens"),
    ("claude-haiku-4-5", "Claude Haiku 4.5 - cheapest, basic web search only"),
]


def estimate_usd(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    web_searches: int = 0,
) -> float:
    """Rough USD cost for a set of usage numbers. Unknown models fall back to Opus 5 rates."""
    price_in, price_out = MODEL_PRICES.get(model, MODEL_PRICES["claude-opus-5"])
    per_token_in = price_in / 1_000_000
    per_token_out = price_out / 1_000_000
    return (
        input_tokens * per_token_in
        + cache_read_tokens * per_token_in * CACHE_READ_MULTIPLIER
        + cache_write_tokens * per_token_in * CACHE_WRITE_MULTIPLIER
        + output_tokens * per_token_out
        + web_searches * WEB_SEARCH_USD_PER_SEARCH
    )


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Env:
    data_dir: Path
    db_path: Path
    host: str
    port: int
    scheduler_enabled: bool
    fake_llm: bool
    smtp_host: str | None
    smtp_port: int
    smtp_user: str | None
    smtp_password: str | None
    smtp_from: str | None
    smtp_to: str | None
    telegram_bot_token: str | None
    telegram_chat_id: str | None

    @property
    def has_api_credentials(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"


def load_env() -> Env:
    data_dir = Path(os.environ.get("DEALWATCH_DATA_DIR", "data")).expanduser().resolve()
    db_path = Path(os.environ.get("DEALWATCH_DB_PATH", str(data_dir / "dealwatch.db"))).expanduser()
    return Env(
        data_dir=data_dir,
        db_path=db_path,
        host=os.environ.get("DEALWATCH_HOST", "127.0.0.1"),
        port=int(os.environ.get("DEALWATCH_PORT", "8765")),
        scheduler_enabled=_env_bool("DEALWATCH_SCHEDULER", True),
        fake_llm=_env_bool("DEALWATCH_FAKE_LLM", False),
        smtp_host=os.environ.get("SMTP_HOST") or None,
        smtp_port=int(os.environ.get("SMTP_PORT", "587")),
        smtp_user=os.environ.get("SMTP_USER") or None,
        smtp_password=os.environ.get("SMTP_PASSWORD") or None,
        smtp_from=os.environ.get("SMTP_FROM") or None,
        smtp_to=os.environ.get("SMTP_TO") or None,
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID") or None,
    )
