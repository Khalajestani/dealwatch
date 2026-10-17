"""Insights over the tracked price points: per-item statistics, retailer breakdowns, movers and an overview.

Everything here is derived from `price_points` (one row per item per run) and `observations`; nothing is stored.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import mean, pstdev

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Item, Observation, PricePoint, Run

TREND_THRESHOLD_PCT = 1.5  # per check; below this the trend is "flat"
TREND_WINDOW = 8  # last N checks used for the trend slope


@dataclass
class SeriesPoint:
    at: datetime
    best: float  # best comparable price seen that check (per unit when the item has units)
    avg: float  # average comparable price across the offers of that check
    run_id: int
    domain: str
    promo: bool = False


@dataclass
class ItemInsight:
    item: Item
    basis: str = "pack"  # "unit" | "pack"
    points: list[SeriesPoint] = field(default_factory=list)
    last: SeriesPoint | None = None
    prev: SeriesPoint | None = None
    low: float | None = None
    low_at: datetime | None = None
    low_domain: str | None = None
    high: float | None = None
    avg_all: float | None = None
    avg_30d: float | None = None
    change_vs_prev_pct: float | None = None
    change_vs_30d_pct: float | None = None
    trend: str = "n/a"  # falling | flat | rising | n/a
    trend_pct_per_check: float | None = None
    volatility_pct: float | None = None
    best_domain: str | None = None
    best_domain_share_pct: float | None = None
    promo_share_pct: float | None = None
    first_seen: datetime | None = None
    days_tracked: int = 0

    @property
    def n_points(self) -> int:
        return len(self.points)

    @property
    def unit_label(self) -> str:
        return f"/{self.item.unit or 'unit'}" if self.basis == "unit" else ""

    @property
    def at_all_time_low(self) -> bool:
        """The latest check is at (or ties) the lowest price ever recorded for this item."""
        return (
            self.last is not None and self.low is not None and self.n_points >= 2 and self.last.best <= self.low + 1e-9
        )

    @property
    def is_new_low(self) -> bool:
        """The latest check is strictly below every earlier check (a fresh all-time low)."""
        if self.last is None or self.n_points < 2:
            return False
        return self.last.best < min(p.best for p in self.points[:-1]) - 1e-9


def _comparable(point: PricePoint, basis: str) -> tuple[float | None, float | None]:
    if basis == "unit":
        return point.min_unit_price, point.avg_unit_price
    return point.min_price, point.avg_price


def _slope_pct_per_step(values: list[float]) -> float | None:
    """Least-squares slope of values over their index, as % of the mean per step."""
    n = len(values)
    if n < 3:
        return None
    xs = list(range(n))
    x_mean, y_mean = mean(xs), mean(values)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator == 0 or y_mean == 0:
        return None
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, values, strict=True)) / denominator
    return slope / y_mean * 100


def item_insight(session: Session, item: Item, now: datetime, points: list[PricePoint] | None = None) -> ItemInsight:
    if points is None:
        points = list(
            session.scalars(
                select(PricePoint).where(PricePoint.item_id == item.id).order_by(PricePoint.observed_at, PricePoint.id)
            )
        )
    insight = ItemInsight(item=item)
    if not points:
        return insight
    unit_count = sum(1 for p in points if p.min_unit_price is not None)
    insight.basis = "unit" if unit_count * 2 >= len(points) else "pack"
    for p in points:
        best, avg = _comparable(p, insight.basis)
        if best is None:
            continue
        insight.points.append(
            SeriesPoint(
                at=p.observed_at,
                best=best,
                avg=avg if avg is not None else best,
                run_id=p.run_id,
                domain=p.best_domain,
                promo=bool(p.promo_count),
            )
        )
    series = insight.points
    if not series:
        return insight
    bests = [s.best for s in series]
    insight.first_seen = series[0].at
    insight.days_tracked = max(0, (now - series[0].at).days)
    insight.last = series[-1]
    insight.prev = series[-2] if len(series) > 1 else None
    low_point = min(series, key=lambda s: s.best)
    insight.low, insight.low_at, insight.low_domain = low_point.best, low_point.at, low_point.domain
    insight.high = max(bests)
    insight.avg_all = mean(bests)
    recent = [s.best for s in series if s.at >= now - timedelta(days=30)]
    insight.avg_30d = mean(recent) if recent else None
    if insight.prev and insight.prev.best:
        insight.change_vs_prev_pct = round((insight.last.best - insight.prev.best) / insight.prev.best * 100, 1)
    if insight.avg_30d:
        insight.change_vs_30d_pct = round((insight.last.best - insight.avg_30d) / insight.avg_30d * 100, 1)
    slope = _slope_pct_per_step(bests[-TREND_WINDOW:])
    if slope is not None:
        insight.trend_pct_per_check = round(slope, 2)
        insight.trend = (
            "falling" if slope < -TREND_THRESHOLD_PCT else ("rising" if slope > TREND_THRESHOLD_PCT else "flat")
        )
    if len(bests) >= 2 and insight.avg_all:
        insight.volatility_pct = round(pstdev(bests) / insight.avg_all * 100, 1)
    domains = Counter(s.domain for s in series if s.domain)
    if domains:
        domain, wins = domains.most_common(1)[0]
        insight.best_domain = domain
        insight.best_domain_share_pct = round(wins / len(series) * 100)
    insight.promo_share_pct = round(sum(1 for s in series if s.promo) / len(series) * 100)
    return insight


def all_item_insights(session: Session, now: datetime) -> list[ItemInsight]:
    """Every item on the list; bought items keep their history on their own page but are no longer reported."""
    items = list(session.scalars(select(Item).where(Item.status != "bought").order_by(Item.category, Item.name)))
    by_item: dict[int, list[PricePoint]] = defaultdict(list)
    for point in session.scalars(select(PricePoint).order_by(PricePoint.observed_at, PricePoint.id)):
        by_item[point.item_id].append(point)
    return [item_insight(session, item, now, by_item.get(item.id, [])) for item in items]


def price_drops(insights: list[ItemInsight], run_id: int, min_drop_pct: float = 5.0) -> list[ItemInsight]:
    """Items whose price in the given check is a new all-time low or clearly below the check before, biggest first."""
    drops = [
        i
        for i in insights
        if i.last is not None
        and i.last.run_id == run_id
        and (i.is_new_low or (i.change_vs_prev_pct is not None and i.change_vs_prev_pct <= -min_drop_pct))
    ]
    return sorted(drops, key=lambda i: i.change_vs_prev_pct or 0.0)


@dataclass
class RetailerStat:
    domain: str
    n_offers: int
    min_price: float
    avg_price: float
    last_price: float
    last_seen: datetime
    wins: int = 0  # checks where this shop had the best price


def retailer_breakdown(session: Session, item_id: int, basis: str) -> list[RetailerStat]:
    observations = list(
        session.scalars(select(Observation).where(Observation.item_id == item_id).order_by(Observation.observed_at))
    )
    wins = Counter(p.best_domain for p in session.scalars(select(PricePoint).where(PricePoint.item_id == item_id)))
    grouped: dict[str, list[Observation]] = defaultdict(list)
    for obs in observations:
        value = obs.unit_price if basis == "unit" else obs.price
        if value is None:
            continue
        grouped[obs.retailer_domain].append(obs)
    stats: list[RetailerStat] = []
    for domain, rows in grouped.items():
        values = [(o.unit_price if basis == "unit" else o.price) for o in rows]
        stats.append(
            RetailerStat(
                domain=domain,
                n_offers=len(rows),
                min_price=min(values),
                avg_price=mean(values),
                last_price=values[-1],
                last_seen=rows[-1].observed_at,
                wins=wins.get(domain, 0),
            )
        )
    stats.sort(key=lambda s: (s.min_price, -s.wins))
    return stats


@dataclass
class Overview:
    items_total: int = 0
    items_tracked: int = 0
    points: int = 0
    observations: int = 0
    runs: int = 0
    first_point_at: datetime | None = None
    at_low: list[ItemInsight] = field(default_factory=list)
    movers_down: list[ItemInsight] = field(default_factory=list)
    movers_up: list[ItemInsight] = field(default_factory=list)
    leaderboard: list[tuple[str, int]] = field(default_factory=list)  # (domain, checks won)


def overview(
    session: Session, now: datetime, insights: list[ItemInsight] | None = None, mover_threshold_pct: float = 5.0
) -> Overview:
    insights = insights if insights is not None else all_item_insights(session, now)
    ov = Overview()
    ov.items_total = len(insights)
    tracked = [i for i in insights if i.n_points]
    ov.items_tracked = len(tracked)
    ov.points = session.scalar(select(func.count()).select_from(PricePoint)) or 0
    ov.observations = session.scalar(select(func.count()).select_from(Observation)) or 0
    ov.runs = session.scalar(select(func.count()).select_from(Run).where(Run.status.in_(["success", "partial"]))) or 0
    ov.first_point_at = session.scalar(select(func.min(PricePoint.observed_at)))
    ov.at_low = [i for i in tracked if i.at_all_time_low]
    movers = [i for i in tracked if i.change_vs_prev_pct is not None]
    ov.movers_down = sorted(
        [i for i in movers if i.change_vs_prev_pct <= -mover_threshold_pct], key=lambda i: i.change_vs_prev_pct
    )[:8]
    ov.movers_up = sorted(
        [i for i in movers if i.change_vs_prev_pct >= mover_threshold_pct], key=lambda i: -i.change_vs_prev_pct
    )[:8]
    wins = Counter(p.best_domain for p in session.scalars(select(PricePoint)) if p.best_domain)
    ov.leaderboard = wins.most_common(10)
    return ov
