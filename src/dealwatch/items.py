"""The one write path for items, shared by the forms and Ask: what an item may hold, adding, changing and the
quick actions (bought, already bought, pause, check in the next run). Deleting stays in the UI."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from sqlalchemy.orm import Session

from .models import CATEGORY_KEYS, PRIORITY_KEYS, UNITS, Item, utcnow
from .settings import Settings

FIELDS = (
    "name",
    "brand",
    "category",
    "priority",
    "search_hints",
    "unit",
    "bulk_ok",
    "target_price",
    "target_unit_price",
    "check_interval_days",
    "reference_url",
    "notes",
)
Action = Literal["bought", "already_bought", "undo_bought", "pause", "resume", "unsnooze", "check_next"]


class ItemIn(BaseModel):
    """An item as entered. Only the name is required; a blank category or priority takes the default."""

    model_config = ConfigDict(validate_default=True)

    name: str | None = None
    brand: str | None = None
    category: str | None = "household"
    priority: str | None = "stock_up"
    search_hints: str | None = None
    unit: str | None = None
    bulk_ok: bool = True
    target_price: float | None = None
    target_unit_price: float | None = None
    check_interval_days: int | None = None
    reference_url: str | None = None
    notes: str | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("target_price", "target_unit_price", mode="before")
    @classmethod
    def _decimal_comma(cls, value: Any) -> Any:
        return value.replace(",", ".") if isinstance(value, str) else value

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str:
        if not value:
            raise ValueError("a name is required")
        return value[:200]

    @field_validator("category")
    @classmethod
    def _category(cls, value: str | None) -> str:
        if value is None:
            return "household"
        if value not in CATEGORY_KEYS:
            raise ValueError(f"use one of {', '.join(CATEGORY_KEYS)}")
        return value

    @field_validator("priority")
    @classmethod
    def _priority(cls, value: str | None) -> str:
        if value is None:
            return "stock_up"
        if value not in PRIORITY_KEYS:
            raise ValueError(f"use one of {', '.join(PRIORITY_KEYS)}")
        return value

    @field_validator("unit")
    @classmethod
    def _unit(cls, value: str | None) -> str | None:
        if value is not None and value not in UNITS:
            raise ValueError(f"use one of {', '.join(UNITS)}")
        return value

    @field_validator("target_price", "target_unit_price")
    @classmethod
    def _positive(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            raise ValueError("must be more than 0")
        return value

    @field_validator("check_interval_days")
    @classmethod
    def _interval(cls, value: int | None) -> int | None:
        if value is not None and not 1 <= value <= 365:
            raise ValueError("between 1 and 365 days")
        return value

    @field_validator("reference_url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        if value and not value.lower().startswith(("http://", "https://")):
            raise ValueError("must start with http:// or https://")
        return value


def parse(data: dict[str, Any]) -> ItemIn:
    """Validate; raises ValueError with one readable line ("Target price: must be more than 0")."""
    try:
        return ItemIn.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{str(e['loc'][0]).replace('_', ' ').capitalize()}: {e['msg'].removeprefix('Value error, ')}"
            for e in exc.errors()
        )
        raise ValueError(problems) from None


def from_form(form: dict[str, Any]) -> ItemIn:
    """The add and edit forms: an unticked checkbox is simply absent."""
    data = {key: form.get(key) for key in FIELDS if key != "bulk_ok"}
    return parse(data | {"bulk_ok": bool(form.get("bulk_ok"))})


def create_item(session: Session, data: ItemIn, image_path: str | None = None) -> Item:
    item = Item(**data.model_dump(), image_path=image_path)
    session.add(item)
    session.commit()
    return item


def change_item(session: Session, item: Item, changes: dict[str, Any]) -> Item:
    """Change some fields; the result is checked as a whole, so nothing is saved when one value is wrong."""
    current = {key: getattr(item, key) for key in FIELDS}
    data = parse(current | changes)
    for key, value in data.model_dump().items():
        setattr(item, key, value)
    session.commit()
    return item


def apply_action(session: Session, item: Item, action: Action, settings: Settings) -> str:
    """One of the quick actions on an item. Returns what happened, in a sentence."""
    now = utcnow()
    if action == "bought":
        item.last_bought_at = now
        item.snooze_until = now + timedelta(days=settings.bought_snooze_days)
        msg = f"Marked as bought, skipped for {settings.bought_snooze_days} days."
    elif action == "already_bought":
        item.status, item.bought_at, item.last_bought_at = "bought", now, now
        msg = "Moved to Purchased: no more checks or deals for it."
    elif action == "undo_bought":
        item.status, item.bought_at = "active", None
        msg = "Back on the list."
    elif action == "pause":
        item.active = False
        msg = "Paused: no more checks until it is resumed."
    elif action == "resume":
        item.active = True
        msg = "Resumed."
    elif action == "unsnooze":
        item.snooze_until = None
        msg = "Snooze removed."
    elif action == "check_next":
        item.active, item.last_checked_at, item.snooze_until = True, None, None
        msg = "Will be checked in the next price check."
    else:
        raise ValueError(f"unknown action {action!r}")
    session.commit()
    return msg
