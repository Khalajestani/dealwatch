"""Reads shared by the pages and Ask."""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .models import Item, Observation, Run


def _latest_check_per_item(item_ids: list[int]):
    """Each item's most recent check that found it, as (item_id, run_id)."""
    return (
        select(Observation.item_id, func.max(Observation.run_id).label("run_id"))
        .where(Observation.item_id.in_(item_ids))
        .group_by(Observation.item_id)
        .subquery()
    )


def latest_best_offers(session: Session, item_ids: list[int]) -> dict[int, Observation]:
    """The best (lowest comparable) offer from each item's most recent check that found it."""
    best: dict[int, Observation] = {}
    if not item_ids:
        return best
    latest = _latest_check_per_item(item_ids)
    rows = session.scalars(
        select(Observation).join(
            latest, (Observation.item_id == latest.c.item_id) & (Observation.run_id == latest.c.run_id)
        )
    )
    for obs in rows:
        current = best.get(obs.item_id)
        if current is None or obs.comparable_price < current.comparable_price:
            best[obs.item_id] = obs
    return best


def last_complete_run(session: Session) -> Run | None:
    return session.scalars(select(Run).where(Run.status.in_(["success", "partial"])).order_by(Run.id.desc())).first()


def current_deals(session: Session, today: date) -> list[tuple[Observation, Item]]:
    """The best deal of each watched item from its most recent check, unless the offer has ended; best first.

    A daily check only looks at the items that are due, so the last check alone would hide the deals the checks
    before it found for the other items."""
    item_ids = list(session.scalars(select(Item.id).where(Item.watched)))
    if not item_ids:
        return []
    latest = _latest_check_per_item(item_ids)
    rows = session.execute(
        select(Observation, Item)
        .join(Item, Item.id == Observation.item_id)
        .join(latest, (Observation.item_id == latest.c.item_id) & (Observation.run_id == latest.c.run_id))
        .where(
            Observation.is_deal.is_(True),
            or_(Observation.valid_until.is_(None), Observation.valid_until >= today),
        )
        .order_by(Observation.score.desc())
    ).all()
    seen: set[int] = set()
    deals = []
    for obs, item in rows:
        if item.id not in seen:
            seen.add(item.id)
            deals.append((obs, item))
    return deals
