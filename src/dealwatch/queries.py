"""Reads shared by the pages and Ask."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Item, Observation, Run


def latest_best_offers(session: Session, item_ids: list[int]) -> dict[int, Observation]:
    """The best (lowest comparable) offer from each item's most recent check that found it."""
    best: dict[int, Observation] = {}
    if not item_ids:
        return best
    latest_run = (
        select(Observation.item_id, func.max(Observation.run_id).label("run_id"))
        .where(Observation.item_id.in_(item_ids))
        .group_by(Observation.item_id)
        .subquery()
    )
    rows = session.scalars(
        select(Observation).join(
            latest_run, (Observation.item_id == latest_run.c.item_id) & (Observation.run_id == latest_run.c.run_id)
        )
    )
    for obs in rows:
        current = best.get(obs.item_id)
        if current is None or obs.comparable_price < current.comparable_price:
            best[obs.item_id] = obs
    return best


def last_complete_run(session: Session) -> Run | None:
    return session.scalars(select(Run).where(Run.status.in_(["success", "partial"])).order_by(Run.id.desc())).first()


def deals_of(session: Session, run: Run | None) -> list[tuple[Observation, Item]]:
    """The best deal per item in a check, highest score first."""
    if run is None:
        return []
    rows = session.execute(
        select(Observation, Item)
        .join(Item, Item.id == Observation.item_id)
        .where(Observation.run_id == run.id, Observation.is_deal.is_(True))
        .order_by(Observation.score.desc())
    ).all()
    seen: set[int] = set()
    deals = []
    for obs, item in rows:
        if item.id not in seen:
            seen.add(item.id)
            deals.append((obs, item))
    return deals
