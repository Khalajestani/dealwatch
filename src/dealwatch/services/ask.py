"""One turn on the Ask page: instructions + the list with its latest prices + earlier turns + tools -> reply.

Claude Code keeps nothing between messages, so each one carries the stable instructions (ask.md), today's date,
a compact snapshot of every item as JSON (null where nothing is recorded) and the last turns, stored in
chat_messages. The tools go through items.py, the same write path as the forms; nothing is deleted by a tool.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import items as item_store
from ..items import Action
from ..models import ChatMessage, Item, Observation, utcnow
from ..queries import current_deals, last_complete_run, latest_best_offers
from ..settings import Settings
from .claude import Claude, Unavailable
from .insights import item_insight
from .planner import is_due
from .report import local_date, money
from .similar import find_similar
from .tool import Tool, tool

HISTORY_TURNS = 20
SYSTEM = (Path(__file__).parent / "ask.md").read_text(encoding="utf-8").strip()
# Claude Code's names for the plan's models, as shown in the picker. Keeping the list and answering from it is quick
# work, so Haiku is the default; the picker remembers another choice on the device.
MODELS = {"haiku": "Haiku, quick", "sonnet": "Sonnet, balanced", "opus": "Opus, thorough"}
DEFAULT_MODEL = "haiku"
EFFORT = {"haiku": "low", "sonnet": "medium", "opus": "medium"}


def _status(item: Item, now: datetime, settings: Settings) -> str:
    if not item.active:
        return "paused"
    if item.snooze_until and item.snooze_until > now:
        return f"bought, snoozed until {local_date(item.snooze_until, settings.timezone)}"
    return "due for the next check" if is_due(item, now, settings) else "waiting"


def snapshot(session: Session, settings: Settings, now: datetime) -> list[dict]:
    """One compact record per item: what it is, its targets and the best offer of its latest check."""
    all_items = list(session.scalars(select(Item).order_by(Item.id)))
    best = latest_best_offers(session, [i.id for i in all_items])
    records = []
    for item in all_items:
        offer = best.get(item.id)
        records.append(
            {
                "id": item.id,
                "name": item.display_name,
                "category": item.category,
                "priority": item.priority,
                "unit": item.unit,
                "hints": item.search_hints,
                "target_price": item.target_price,
                "target_unit_price": item.target_unit_price,
                "status": _status(item, now, settings),
                "last_checked": local_date(item.last_checked_at, settings.timezone),
                "latest_best": {
                    "price": offer.price,
                    "per_unit": offer.unit_price,
                    "shop": offer.retailer_domain,
                    "promo": offer.promo_text if offer.promo_type != "none" else None,
                    "deal": offer.is_deal,
                    "usual": offer.baseline_price,
                }
                if offer
                else None,
            }
        )
    return records


def history(session: Session, limit: int = HISTORY_TURNS) -> list[ChatMessage]:
    rows = session.scalars(select(ChatMessage).order_by(ChatMessage.id.desc()).limit(limit)).all()
    return list(reversed(rows))


def clear_history(session: Session) -> None:
    session.execute(delete(ChatMessage))
    session.commit()


def build_system(session: Session, settings: Settings, now: datetime, past: list[ChatMessage]) -> str:
    """Stable instructions first, then what changes per message: the date, the list, earlier turns."""
    local = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(settings.timezone))
    parts = [
        SYSTEM,
        f"TODAY\n{local:%d/%m/%Y} ({local:%A}), {local:%H:%M}",
        "ITEMS (JSON, one record per item; null means not recorded; latest_best is the cheapest offer of the item's "
        "latest check, usual its normal price per unit or pack)\n"
        + json.dumps(snapshot(session, settings, now), ensure_ascii=False, separators=(",", ":")),
    ]
    if past:
        turns = "\n".join(f"{'User' if m.role == 'user' else 'You'}: {m.content}" for m in past)
        parts.append(f"EARLIER IN THIS CONVERSATION\n{turns}")
    return "\n\n".join(parts)


def _line(item: Item) -> str:
    return f"#{item.id} {item.display_name} ({item.priority}{', paused' if not item.active else ''})"


def make_tools(session: Session, settings: Settings) -> list[Tool]:
    def get(item_id: int) -> Item:
        item = session.get(Item, item_id)
        if item is None:
            raise ValueError(f"there is no item #{item_id}")
        return item

    @tool
    def get_item(item_id: int) -> str:
        """Everything recorded about one item: details, price history summary and the offers of its latest checks.

        Args:
            item_id: The item's id from the list.
        """
        item = get(item_id)
        ins = item_insight(session, item, utcnow())
        cur = settings.currency
        lines = [
            _line(item),
            f"category: {item.category}, unit: {item.unit}, bulk packs ok: {item.bulk_ok}",
            f"search hints: {item.search_hints}",
            f"targets: {item.target_price} per pack, {item.target_unit_price} per unit",
            f"notes: {item.notes}",
            f"product link: {item.reference_url}",
        ]
        if ins.n_points:
            lines += [
                f"checks with prices: {ins.n_points} since {local_date(ins.first_seen, settings.timezone)}",
                f"latest best: {money(ins.last.best, cur)}{ins.unit_label} at {ins.last.domain}",
                f"lowest ever: {money(ins.low, cur)}{ins.unit_label} on {local_date(ins.low_at, settings.timezone)} at {ins.low_domain}",
                f"30-day average of best prices: {money(ins.avg_30d, cur)}{ins.unit_label}, trend {ins.trend}",
                f"cheapest most often at: {ins.best_domain}",
            ]
        offers = session.scalars(
            select(Observation).where(Observation.item_id == item_id).order_by(Observation.id.desc()).limit(6)
        )
        for o in offers:
            per_unit = f" ({money(o.unit_price, cur)}/{o.unit})" if o.unit_price is not None else ""
            promo = f", {o.promo_type}: {o.promo_text}" if o.promo_type != "none" else ""
            lines.append(
                f"offer {local_date(o.observed_at, settings.timezone)}: {money(o.price, cur)}{per_unit} at "
                f"{o.retailer_domain}{promo}{', DEAL' if o.is_deal else ''}"
            )
        return "\n".join(lines)

    @tool
    def latest_deals() -> str:
        """The current deals, best first, with the reasons: each item's deal from its most recent check, unless the
        offer has ended."""
        deals = current_deals(session, utcnow().date())
        if not deals:
            return "No current deals." if last_complete_run(session) else "No price check has completed yet."
        return "\n".join(
            f"{item.display_name}: {money(o.price, settings.currency)} at {o.retailer_domain}, found "
            f"{local_date(o.observed_at, settings.timezone)} ({o.reasons})"
            for o, item in deals
        )

    @tool
    def add_item(
        name: str,
        brand: str | None = None,
        category: str = "household",
        priority: str = "stock_up",
        search_hints: str | None = None,
        unit: str | None = None,
        target_price: float | None = None,
        target_unit_price: float | None = None,
        bulk_ok: bool = True,
        notes: str | None = None,
        add_even_if_similar: bool = False,
    ) -> str:
        """Add an item to watch. It is checked in the next price check.

        Args:
            name: Generic product name in English without the brand, e.g. "Kitchen towels".
            brand: e.g. "Page".
            category: household, personal_care, groceries, health, tools_diy, electronics, home_garden, pets_baby or other.
            priority: need (running low), stock_up (buy at a real deal) or wishlist (only great deals).
            search_hints: Dutch keywords with variant and pack size, e.g. "keukenpapier XXL 8 rollen".
            unit: Unit to compare per: piece, roll, wash, tablet, capsule, bag, ml, l, g, kg, m or pair.
            target_price: Buy at or under this price per pack, in EUR.
            target_unit_price: Buy at or under this price per unit, in EUR.
            bulk_ok: Whether household multi-packs are fine.
            notes: Anything else they said.
            add_even_if_similar: Only when they confirmed it is a different product from a similar one on the list.
        """
        data = item_store.parse(
            {
                "name": name,
                "brand": brand,
                "category": category,
                "priority": priority,
                "search_hints": search_hints,
                "unit": unit,
                "target_price": target_price,
                "target_unit_price": target_unit_price,
                "bulk_ok": bulk_ok,
                "notes": notes,
            }
        )
        similar = find_similar(data.name, data.brand, session.scalars(select(Item)))
        if similar and similar[0].likely_duplicate and not add_even_if_similar:
            match = similar[0].item
            return f"Not added: #{match.id} {match.display_name} looks like the same product. Update that one instead?"
        return f"Added {_line(item_store.create_item(session, data))}."

    @tool
    def update_item(
        item_id: int,
        name: str | None = None,
        brand: str | None = None,
        category: str | None = None,
        priority: str | None = None,
        search_hints: str | None = None,
        unit: str | None = None,
        target_price: float | None = None,
        target_unit_price: float | None = None,
        bulk_ok: bool | None = None,
        notes: str | None = None,
    ) -> str:
        """Change some fields of an item. Fields left out keep their value.

        Args:
            item_id: The item's id from the list.
            name: New name.
            brand: New brand.
            category: New category.
            priority: need, stock_up or wishlist.
            search_hints: The complete new search hints.
            unit: New unit to compare per.
            target_price: New target per pack, in EUR.
            target_unit_price: New target per unit, in EUR.
            bulk_ok: Whether household multi-packs are fine.
            notes: The complete new notes. Keep what was there unless they want it gone.
        """
        given = {
            "name": name,
            "brand": brand,
            "category": category,
            "priority": priority,
            "search_hints": search_hints,
            "unit": unit,
            "target_price": target_price,
            "target_unit_price": target_unit_price,
            "bulk_ok": bulk_ok,
            "notes": notes,
        }
        changes = {key: value for key, value in given.items() if value is not None}
        if not changes:
            return "Nothing to change."
        return f"Updated {_line(item_store.change_item(session, get(item_id), changes))}."

    @tool
    def item_action(item_id: int, action: Action) -> str:
        """A quick action on an item: bought (snoozes it), pause, resume, unsnooze, or check_next (check it in the next
        price check).

        Args:
            item_id: The item's id from the list.
            action: bought, pause, resume, unsnooze or check_next.
        """
        item = get(item_id)
        return f"{item.display_name}: {item_store.apply_action(session, item, action, settings)}"

    return [get_item, latest_deals, add_item, update_item, item_action]


def reply(
    claude: Claude, session: Session, settings: Settings, text: str, model: str | None, photo: bytes | None = None
) -> str:
    """Answer one message and store both sides. `model` is the picker's choice; unknown names fall back."""
    past = history(session)
    session.add(ChatMessage(role="user", content=text + (" [photo]" if photo else "")))
    session.commit()
    model = model if model in MODELS else DEFAULT_MODEL
    try:
        system = build_system(session, settings, utcnow(), past)
        answer = claude.ask(
            text or "What do you make of this photo?",
            system,
            model=model,
            effort=EFFORT[model],
            tools=make_tools(session, settings),
            photo=photo,
            on_tool_error=session.rollback,
        )
        answer = answer or "Done."
    except Unavailable as exc:
        answer = str(exc)
    session.add(ChatMessage(role="assistant", content=answer))
    session.commit()
    return answer
