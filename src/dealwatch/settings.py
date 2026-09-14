"""User-editable behaviour settings stored in the DB (`settings` table) and edited on /settings.

To add a setting: add a field with a default on `Settings`, then a `SettingSpec` in SPECS
(the spec drives the settings page and validation). Nothing else is needed.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import MODEL_CHOICES
from .models import Setting

EFFORT_CHOICES = [
    ("low", "low - fastest/cheapest"),
    ("medium", "medium - good balance (default)"),
    ("high", "high"),
    ("xhigh", "xhigh - most thorough"),
]
SEARCH_MODE_CHOICES = [
    ("allowlist", "Only search enabled retailers (recommended)"),
    ("open", "Search the whole web except blocked retailers"),
]
NOTIFY_CHOICES = [("none", "None (web UI only)"), ("email", "E-mail (SMTP_* env vars)"), ("telegram", "Telegram (TELEGRAM_* env vars)")]


@dataclass
class SettingSpec:
    key: str
    label: str
    help: str
    group: str
    kind: str = "str"  # str | int | float | bool | choice | cron
    choices: list[tuple[str, str]] | None = None
    min: float | None = None
    max: float | None = None


@dataclass
class Settings:
    # LLM / search
    search_model: str = "claude-opus-5"
    search_effort: str = "medium"
    search_mode: str = "allowlist"
    search_trim_response: bool = True
    llm_fallbacks: bool = True
    # budget / batching
    group_size: int = 6
    max_groups_per_run: int = 10
    max_searches_per_group: int = 10
    parallel_groups: int = 2
    # deal logic
    min_discount_pct: float = 10.0
    baseline_window_days: int = 90
    interval_need_days: int = 3
    interval_stock_up_days: int = 7
    interval_wishlist_days: int = 14
    bought_snooze_days: int = 30
    # locale
    country: str = "NL"
    city: str = "Amsterdam"
    timezone: str = "Europe/Amsterdam"
    currency: str = "EUR"
    # schedule / notifications
    schedule_enabled: bool = True
    schedule_cron: str = "0 7 * * 1,4"
    notify_channel: str = "none"
    notify_only_deals: bool = True

    def interval_for(self, priority: str) -> int:
        return {
            "need": self.interval_need_days,
            "stock_up": self.interval_stock_up_days,
            "wishlist": self.interval_wishlist_days,
        }.get(priority, self.interval_stock_up_days)


SPECS: list[SettingSpec] = [
    SettingSpec("search_model", "Search model", "Model that runs the web searches and extracts prices. Opus 5 has the best judgement; Sonnet 5 is about 2.5x cheaper per token.", "LLM & search", "choice", MODEL_CHOICES),
    SettingSpec("search_effort", "Effort", "How hard the model thinks per batch. Medium is plenty for price extraction; raise it if results look sloppy.", "LLM & search", "choice", EFFORT_CHOICES),
    SettingSpec("search_mode", "Search mode", "Allowlist restricts the search engine to enabled retailers (fewer wasted searches). Open searches everywhere except blocked retailers.", "LLM & search", "choice", SEARCH_MODE_CHOICES),
    SettingSpec("search_trim_response", "Trim search payloads", "Drop raw search-result blocks from API responses (cheaper). Turn off only when debugging what the model saw.", "LLM & search", "bool"),
    SettingSpec("llm_fallbacks", "Server-side fallbacks", "If the primary model declines a request, let Anthropic retry it on a fallback model inside the same call.", "LLM & search", "bool"),
    SettingSpec("group_size", "Items per batch", "How many items share one LLM call. Bigger batches = fewer calls, but too big and the model gets sloppy. 4-8 works well.", "Budget & batching", "int", min=1, max=12),
    SettingSpec("max_groups_per_run", "Max batches per run", "Hard cap on LLM calls per run. Items beyond the cap are deferred to the next run (most urgent first).", "Budget & batching", "int", min=1, max=100),
    SettingSpec("max_searches_per_group", "Max searches per batch", "Hard cap on web searches in one batch ($0.01 each). The planner also caps it at 2 per item + 1.", "Budget & batching", "int", min=1, max=30),
    SettingSpec("parallel_groups", "Parallel batches", "How many batches run at the same time. 2-3 keeps runs short without hitting rate limits.", "Budget & batching", "int", min=1, max=6),
    SettingSpec("min_discount_pct", "Deal threshold (%)", "Minimum discount versus your price history (or the shown regular price) before something is called a deal.", "Deal logic", "float", min=0, max=90),
    SettingSpec("baseline_window_days", "Baseline window (days)", "Price history window used to compute the 'normal' (median) price per item.", "Deal logic", "int", min=7, max=365),
    SettingSpec("interval_need_days", "Check interval: need (days)", "How often items with priority 'need' are checked.", "Deal logic", "int", min=1, max=90),
    SettingSpec("interval_stock_up_days", "Check interval: stock up (days)", "How often 'stock up' items are checked.", "Deal logic", "int", min=1, max=90),
    SettingSpec("interval_wishlist_days", "Check interval: wishlist (days)", "How often 'wishlist' items are checked.", "Deal logic", "int", min=1, max=180),
    SettingSpec("bought_snooze_days", "Snooze after buying (days)", "When you mark an item as bought it is skipped for this many days.", "Deal logic", "int", min=0, max=365),
    SettingSpec("country", "Country code", "Two-letter ISO code used to localise search results.", "Locale", "str"),
    SettingSpec("city", "City", "Used to localise search results (optional).", "Locale", "str"),
    SettingSpec("timezone", "Timezone", "IANA timezone for schedules and report timestamps.", "Locale", "str"),
    SettingSpec("currency", "Currency", "Currency the report expects prices in.", "Locale", "str"),
    SettingSpec("schedule_enabled", "Scheduled runs", "Run automatically while `dealwatch serve` is running.", "Schedule & notifications", "bool"),
    SettingSpec("schedule_cron", "Schedule (cron)", "Standard 5-field cron in your timezone. Default `0 7 * * 1,4` = Monday and Thursday at 07:00.", "Schedule & notifications", "cron"),
    SettingSpec("notify_channel", "Notification channel", "Where to send the report after each run.", "Schedule & notifications", "choice", NOTIFY_CHOICES),
    SettingSpec("notify_only_deals", "Only notify when there are deals", "Skip the notification when a run found nothing worth buying.", "Schedule & notifications", "bool"),
]
SPEC_BY_KEY = {spec.key: spec for spec in SPECS}
_FIELD_TYPES = {f.name: f.type for f in fields(Settings)}


def _coerce(key: str, raw: Any) -> Any:
    ftype = _FIELD_TYPES[key]
    if ftype in ("bool", bool):
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    if ftype in ("int", int):
        return int(float(str(raw).strip()))
    if ftype in ("float", float):
        return float(str(raw).strip())
    return str(raw).strip()


def validate_setting(key: str, value: Any) -> Any:
    """Coerce and validate one setting. Raises ValueError with a user-facing message."""
    if key not in SPEC_BY_KEY:
        raise ValueError(f"Unknown setting '{key}'")
    spec = SPEC_BY_KEY[key]
    try:
        value = _coerce(key, value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{spec.label}: invalid value") from exc
    if spec.kind == "choice" and spec.choices and value not in {c[0] for c in spec.choices}:
        if key != "search_model":  # allow typing any model id
            raise ValueError(f"{spec.label}: '{value}' is not one of the allowed choices")
    if spec.kind in ("int", "float"):
        if spec.min is not None and value < spec.min:
            raise ValueError(f"{spec.label}: must be at least {spec.min:g}")
        if spec.max is not None and value > spec.max:
            raise ValueError(f"{spec.label}: must be at most {spec.max:g}")
    if spec.kind == "cron":
        try:
            CronTrigger.from_crontab(value)
        except ValueError as exc:
            raise ValueError(f"{spec.label}: invalid cron expression ({exc})") from exc
    if key == "country" and len(value) != 2:
        raise ValueError("Country code must be two letters, e.g. NL")
    if key == "country":
        value = value.upper()
    if key == "search_model" and not value:
        raise ValueError("Search model cannot be empty")
    return value


def get_settings(session: Session) -> Settings:
    settings = Settings()
    for row in session.scalars(select(Setting)):
        if row.key in _FIELD_TYPES:
            try:
                setattr(settings, row.key, _coerce(row.key, row.value))
            except (TypeError, ValueError):
                pass  # keep default for corrupt values
    return settings


def save_settings(session: Session, values: dict[str, Any]) -> list[str]:
    """Validate and persist. Returns a list of error messages (empty on success). Nothing is saved on error."""
    errors: list[str] = []
    clean: dict[str, Any] = {}
    for key, raw in values.items():
        if key not in _FIELD_TYPES:
            continue
        try:
            clean[key] = validate_setting(key, raw)
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        return errors
    for key, value in clean.items():
        row = session.get(Setting, key)
        text_value = str(value).lower() if isinstance(value, bool) else str(value)
        if row is None:
            session.add(Setting(key=key, value=text_value))
        else:
            row.value = text_value
    session.commit()
    return []
