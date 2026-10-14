"""Price normalisation, baselines from history, and the deal decision."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import mean, median

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Item, Observation, PricePoint
from ..settings import Settings

PRIORITY_BONUS = {"need": 10.0, "stock_up": 5.0, "wishlist": 0.0}
MIN_CONFIDENCE_FOR_DEAL = 0.35


def unit_price(price: float | None, pack_quantity: float | None) -> float | None:
    if price is None or not pack_quantity or pack_quantity <= 0:
        return None
    return round(price / pack_quantity, 4)


@dataclass
class Baseline:
    value: float | None = None  # median comparable price in the window
    basis: str | None = None  # "unit" | "pack"
    n: int = 0
    lowest: float | None = None

    @property
    def label(self) -> str:
        if self.value is None:
            return "no history yet"
        kind = "per unit" if self.basis == "unit" else "per pack"
        if self.n < 3:
            return f"best price last seen {self.value:.2f} {kind}"
        return f"median best price over {self.n} checks {self.value:.2f} {kind}"


def baseline_for_item(session: Session, item_id: int, *, before: datetime, window_days: int) -> Baseline:
    """The item's normal price: median of the best price seen per check (price points) inside the window, strictly
    before `before`, so a check never compares against itself. Per unit when the history has unit prices (fair
    across pack sizes), else per pack."""
    since = before - timedelta(days=window_days)
    points = session.execute(
        select(PricePoint.min_price, PricePoint.min_unit_price).where(
            PricePoint.item_id == item_id, PricePoint.observed_at >= since, PricePoint.observed_at < before
        )
    ).all()
    unit_values = [u for _, u in points if u is not None]
    if unit_values:
        return Baseline(value=round(median(unit_values), 4), basis="unit", n=len(unit_values), lowest=min(unit_values))
    pack_values = [p for p, _ in points]
    if pack_values:
        return Baseline(value=round(median(pack_values), 4), basis="pack", n=len(pack_values), lowest=min(pack_values))
    return Baseline()


@dataclass
class Assessment:
    discount_vs_baseline: float | None = None
    discount_vs_list: float | None = None
    under_target: bool = False
    is_deal: bool = False
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def discount_pct(self) -> float | None:
        candidates = [d for d in (self.discount_vs_baseline, self.discount_vs_list) if d is not None]
        return max(candidates) if candidates else None


def assess_offer(
    *,
    price: float,
    unit_price_value: float | None,
    list_price: float | None,
    promo_type: str,
    confidence: float,
    baseline: Baseline,
    item: Item,
    settings: Settings,
) -> Assessment:
    result = Assessment()

    # 1. versus our own history (the most honest signal once it exists)
    comparable = unit_price_value if baseline.basis == "unit" else price
    if baseline.value and comparable is not None:
        result.discount_vs_baseline = round((baseline.value - comparable) / baseline.value * 100, 1)
        if result.discount_vs_baseline >= settings.min_discount_pct:
            result.reasons.append(f"{result.discount_vs_baseline:.0f}% below {baseline.label}")
        elif result.discount_vs_baseline <= -settings.min_discount_pct:
            result.reasons.append(f"{-result.discount_vs_baseline:.0f}% ABOVE {baseline.label}")

    # 2. versus the regular price shown by the shop
    if list_price and list_price > price:
        result.discount_vs_list = round((list_price - price) / list_price * 100, 1)
        if result.discount_vs_list >= settings.min_discount_pct:
            result.reasons.append(f"{result.discount_vs_list:.0f}% off the shop's regular price {list_price:.2f}")

    # 3. versus the user's targets
    if item.target_price is not None and price <= item.target_price:
        result.under_target = True
        result.reasons.append(f"at/below your target {item.target_price:.2f}")
    if (
        item.target_unit_price is not None
        and unit_price_value is not None
        and unit_price_value <= item.target_unit_price
    ):
        result.under_target = True
        result.reasons.append(f"at/below your unit target {item.target_unit_price:.2f}/{item.unit or 'unit'}")

    promo = promo_type not in ("none", "")
    best = result.discount_pct
    threshold_hit = best is not None and best >= settings.min_discount_pct
    promo_without_context = promo and baseline.value is None and result.discount_vs_list is None and confidence >= 0.6
    if promo_without_context:
        result.reasons.append(f"promotion ({promo_type}) - no price history yet to size it")

    result.is_deal = result.under_target or threshold_hit or promo_without_context
    if result.is_deal and confidence < MIN_CONFIDENCE_FOR_DEAL:
        result.is_deal = False
        result.reasons.append("ignored: confidence too low")

    score = max(best or 0.0, 0.0)
    score += 25.0 if result.under_target else 0.0
    score += PRIORITY_BONUS.get(item.priority, 0.0)
    score += 5.0 if promo else 0.0
    score *= 0.5 + 0.5 * max(0.0, min(1.0, confidence))
    result.score = round(score, 2)
    return result


def summarize_offers(observations: list[Observation]) -> dict:
    """Aggregate one run's offers for an item into the fields of a PricePoint (without ids)."""
    if not observations:
        raise ValueError("no observations to summarise")
    prices = [o.price for o in observations]
    unit_prices = [o.unit_price for o in observations if o.unit_price is not None]
    best = min(observations, key=lambda o: (0, o.unit_price) if o.unit_price is not None else (1, o.price))
    units = [o.unit for o in observations if o.unit]
    return {
        "n_offers": len(observations),
        "n_retailers": len({o.retailer_domain for o in observations}),
        "unit": max(set(units), key=units.count) if units else None,
        "min_price": round(min(prices), 4),
        "avg_price": round(mean(prices), 4),
        "median_price": round(median(prices), 4),
        "max_price": round(max(prices), 4),
        "min_unit_price": round(min(unit_prices), 4) if unit_prices else None,
        "avg_unit_price": round(mean(unit_prices), 4) if unit_prices else None,
        "median_unit_price": round(median(unit_prices), 4) if unit_prices else None,
        "max_unit_price": round(max(unit_prices), 4) if unit_prices else None,
        "best_price": best.price,
        "best_unit_price": best.unit_price,
        "best_pack_quantity": best.pack_quantity,
        "best_domain": best.retailer_domain,
        "best_url": best.url,
        "best_promo_type": best.promo_type or "none",
        "promo_count": sum(1 for o in observations if o.promo_type and o.promo_type != "none"),
        "deal_count": sum(1 for o in observations if o.is_deal),
    }
