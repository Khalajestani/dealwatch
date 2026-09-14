"""Decide what to check this run and how to batch it so each LLM call covers several items.

Grouping strategy: due items are sorted by urgency, grouped by category (so one call searches similar
shops), chunked to `group_size`, and capped at `max_groups_per_run`. Each group gets the retailer domains
that serve its category, which become the web-search allowlist.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import estimate_usd
from ..models import Item, Retailer
from ..settings import Settings
from .pricing import Baseline, baseline_for_item

PRIORITY_RANK = {"need": 0, "stock_up": 1, "wishlist": 2}
DUE_SLACK = timedelta(hours=12)  # so a weekly item checked Mon 07:05 is due again Mon 07:00

# rough token model per batch, used only for the pre-run estimate
_BASE_INPUT_TOKENS = 1_400
_INPUT_TOKENS_PER_SEARCH = 1_500
_OUTPUT_TOKENS_BASE = 900  # JSON scaffolding + adaptive thinking
_OUTPUT_TOKENS_PER_ITEM = 220


@dataclass
class PlannedItem:
    id: int
    name: str
    brand: str | None
    category: str
    search_hints: str | None
    unit: str | None
    bulk_ok: bool
    priority: str
    target_price: float | None
    target_unit_price: float | None
    baseline: float | None = None
    baseline_basis: str | None = None
    baseline_n: int = 0
    lowest_seen: float | None = None

    @property
    def display_name(self) -> str:
        return f"{self.brand} {self.name}".strip() if self.brand else self.name

    def to_prompt_dict(self) -> dict:
        data = {
            "item_id": self.id,
            "name": self.name,
            "brand": self.brand,
            "hints": self.search_hints,
            "unit": self.unit or "piece",
            "bulk_ok": self.bulk_ok,
            "priority": self.priority,
            "target_price": self.target_price,
            "target_unit_price": self.target_unit_price,
            "baseline": self.baseline,
            "baseline_basis": self.baseline_basis,
        }
        return {k: v for k, v in data.items() if v is not None or k in ("item_id", "name")}


@dataclass
class PlannedGroup:
    index: int
    category: str
    items: list[PlannedItem]
    retailers: list[dict]  # [{"name":..., "domain":...}] suggested shops
    allowed_domains: list[str]
    blocked_domains: list[str]
    max_uses: int

    def to_prompt_dict(self) -> dict:
        return {
            "max_uses": self.max_uses,
            "retailers": self.retailers,
            "items": [item.to_prompt_dict() for item in self.items],
        }

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "category": self.category,
            "items": [{"id": i.id, "name": i.display_name, "priority": i.priority, "baseline": i.baseline} for i in self.items],
            "retailers": self.retailers,
            "allowed_domains": self.allowed_domains,
            "blocked_domains": self.blocked_domains,
            "max_uses": self.max_uses,
        }


@dataclass
class CostEstimate:
    api_requests: int = 0
    web_searches: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0


@dataclass
class RunPlan:
    groups: list[PlannedGroup] = field(default_factory=list)
    deferred: list[PlannedItem] = field(default_factory=list)  # due, but over the batch cap
    not_due: list[str] = field(default_factory=list)  # names of active items skipped this run
    enabled_retailers: list[str] = field(default_factory=list)
    blocked_retailers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    estimate: CostEstimate = field(default_factory=CostEstimate)

    @property
    def items_planned(self) -> int:
        return sum(len(g.items) for g in self.groups)

    def to_dict(self) -> dict:
        return {
            "groups": [g.to_dict() for g in self.groups],
            "deferred": [i.display_name for i in self.deferred],
            "not_due": self.not_due,
            "enabled_retailers": self.enabled_retailers,
            "blocked_retailers": self.blocked_retailers,
            "warnings": self.warnings,
            "estimate": asdict(self.estimate),
        }


def is_due(item: Item, now: datetime, settings: Settings) -> bool:
    if not item.active:
        return False
    if item.snooze_until and item.snooze_until > now:
        return False
    if item.last_checked_at is None:
        return True
    interval_days = item.check_interval_days or settings.interval_for(item.priority)
    return item.last_checked_at + timedelta(days=interval_days) - DUE_SLACK <= now


def _urgency_key(item: Item) -> tuple:
    return (PRIORITY_RANK.get(item.priority, 9), item.last_checked_at or datetime.min, item.id)


def _chunks(seq: list, size: int) -> list[list]:
    return [seq[i : i + size] for i in range(0, len(seq), max(1, size))]


def _to_planned_item(item: Item, baseline: Baseline) -> PlannedItem:
    return PlannedItem(
        id=item.id, name=item.name, brand=item.brand, category=item.category, search_hints=item.search_hints,
        unit=item.unit, bulk_ok=item.bulk_ok, priority=item.priority, target_price=item.target_price,
        target_unit_price=item.target_unit_price, baseline=baseline.value, baseline_basis=baseline.basis,
        baseline_n=baseline.n, lowest_seen=baseline.lowest,
    )


def build_plan(session: Session, settings: Settings, *, now: datetime, force: bool = False) -> RunPlan:
    plan = RunPlan()
    items = list(session.scalars(select(Item).where(Item.active.is_(True)).order_by(Item.id)))
    retailers = list(session.scalars(select(Retailer).order_by(Retailer.sort_order, Retailer.name)))
    enabled = [r for r in retailers if r.status == "enabled"]
    blocked = [r for r in retailers if r.status == "blocked"]
    plan.enabled_retailers = [r.domain for r in enabled]
    plan.blocked_retailers = [r.domain for r in blocked]

    due = [i for i in items if force or is_due(i, now, settings)]
    plan.not_due = [i.display_name for i in items if i not in due]
    if not due:
        plan.warnings.append("Nothing is due for a check right now.")
        return plan
    if settings.search_mode == "allowlist" and not enabled:
        plan.warnings.append("No enabled retailers - enable at least one on the Retailers page, or switch search mode to 'open'.")
        return plan

    due.sort(key=_urgency_key)
    by_category: dict[str, list[Item]] = {}
    for item in due:
        by_category.setdefault(item.category, []).append(item)

    raw_groups: list[tuple[str, list[Item]]] = []
    for category, cat_items in by_category.items():
        for chunk in _chunks(cat_items, settings.group_size):
            raw_groups.append((category, chunk))
    raw_groups.sort(key=lambda g: min(_urgency_key(i) for i in g[1]))

    kept = raw_groups[: settings.max_groups_per_run]
    for _, chunk in raw_groups[settings.max_groups_per_run :]:
        plan.deferred.extend(_to_planned_item(i, Baseline()) for i in chunk)
    if plan.deferred:
        plan.warnings.append(f"{len(plan.deferred)} due item(s) deferred to the next run by the batch cap ({settings.max_groups_per_run}).")

    for index, (category, chunk) in enumerate(kept, start=1):
        serving = [r for r in enabled if r.serves(category)]
        if settings.search_mode == "allowlist" and not serving:
            serving = enabled
            plan.warnings.append(f"No enabled retailer is tagged for category '{category}'; using all enabled retailers for that batch.")
        planned_items = [
            _to_planned_item(i, baseline_for_item(session, i.id, before=now, window_days=settings.baseline_window_days))
            for i in chunk
        ]
        max_uses = max(1, min(settings.max_searches_per_group, 2 * len(chunk) + 1))
        plan.groups.append(
            PlannedGroup(
                index=index,
                category=category,
                items=planned_items,
                retailers=[{"name": r.name, "domain": r.domain} for r in serving],
                allowed_domains=[r.domain for r in serving] if settings.search_mode == "allowlist" else [],
                blocked_domains=[r.domain for r in blocked] if settings.search_mode == "open" else [],
                max_uses=max_uses,
            )
        )

    plan.estimate = estimate_plan(plan.groups, settings)
    return plan


def estimate_plan(groups: list[PlannedGroup], settings: Settings) -> CostEstimate:
    est = CostEstimate()
    for group in groups:
        n_items = len(group.items)
        searches = min(group.max_uses, math.ceil(n_items * 1.3))
        est.api_requests += 1
        est.web_searches += searches
        est.input_tokens += _BASE_INPUT_TOKENS + searches * _INPUT_TOKENS_PER_SEARCH
        est.output_tokens += _OUTPUT_TOKENS_BASE + n_items * _OUTPUT_TOKENS_PER_ITEM
    est.usd = round(
        estimate_usd(
            settings.search_model,
            input_tokens=est.input_tokens,
            output_tokens=est.output_tokens,
            web_searches=est.web_searches,
        ),
        3,
    )
    return est
