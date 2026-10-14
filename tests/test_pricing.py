from datetime import datetime, timedelta

from dealwatch.models import Item, PricePoint, Run
from dealwatch.services.pricing import Baseline, assess_offer, baseline_for_item, unit_price
from dealwatch.settings import Settings


def _item(**kw) -> Item:
    base = dict(name="Towels", unit="roll", priority="stock_up")
    base.update(kw)
    return Item(**base)


def test_unit_price():
    assert unit_price(4.99, 8) == 0.6238
    assert unit_price(4.99, None) is None
    assert unit_price(4.99, 0) is None


def test_discount_vs_baseline_is_a_deal():
    a = assess_offer(
        price=4.99,
        unit_price_value=0.62,
        list_price=None,
        promo_type="none",
        confidence=0.9,
        baseline=Baseline(value=0.86, basis="unit", n=4),
        item=_item(),
        settings=Settings(),
    )
    assert a.is_deal and a.discount_vs_baseline == 27.9 and "below" in a.reasons[0]
    assert a.score > 25


def test_small_discount_is_not_a_deal():
    a = assess_offer(
        price=6.50,
        unit_price_value=0.81,
        list_price=None,
        promo_type="none",
        confidence=0.9,
        baseline=Baseline(value=0.86, basis="unit", n=4),
        item=_item(),
        settings=Settings(),
    )
    assert not a.is_deal and a.discount_pct == 5.8


def test_target_price_beats_threshold():
    a = assess_offer(
        price=6.40,
        unit_price_value=0.80,
        list_price=None,
        promo_type="none",
        confidence=0.9,
        baseline=Baseline(value=0.86, basis="unit", n=4),
        item=_item(target_unit_price=0.80),
        settings=Settings(),
    )
    assert a.is_deal and a.under_target and any("unit target" in r for r in a.reasons)


def test_list_price_discount_without_history():
    a = assess_offer(
        price=4.99,
        unit_price_value=None,
        list_price=6.99,
        promo_type="sale",
        confidence=0.8,
        baseline=Baseline(),
        item=_item(unit=None),
        settings=Settings(),
    )
    assert a.is_deal and a.discount_vs_list == 28.6


def test_promo_without_any_context_needs_confidence():
    kw = dict(
        price=4.99,
        unit_price_value=0.62,
        list_price=None,
        promo_type="multibuy",
        baseline=Baseline(),
        item=_item(),
        settings=Settings(),
    )
    assert assess_offer(confidence=0.7, **kw).is_deal
    assert not assess_offer(confidence=0.5, **kw).is_deal


def test_low_confidence_blocks_deal():
    a = assess_offer(
        price=3.00,
        unit_price_value=0.38,
        list_price=None,
        promo_type="none",
        confidence=0.2,
        baseline=Baseline(value=0.86, basis="unit", n=4),
        item=_item(),
        settings=Settings(),
    )
    assert not a.is_deal and "confidence too low" in a.reasons[-1]


def test_baseline_median_and_window(session_factory):
    now = datetime(2026, 9, 14, 7, 0)
    with session_factory() as s:
        item = s.query(Item).first()
        for days_ago, price, qty in [(1, 8.0, 8), (10, 6.0, 8), (20, 7.0, 8), (200, 1.0, 8)]:
            run = Run(started_at=now - timedelta(days=days_ago), status="success")
            s.add(run)
            s.flush()
            per_unit = unit_price(price, qty)
            s.add(
                PricePoint(
                    item_id=item.id,
                    run_id=run.id,
                    observed_at=run.started_at,
                    min_price=price,
                    avg_price=price,
                    median_price=price,
                    max_price=price,
                    min_unit_price=per_unit,
                    best_price=price,
                    best_unit_price=per_unit,
                    best_domain="x.nl",
                    best_url="https://x.nl/p",
                )
            )
        s.commit()
        b = baseline_for_item(s, item.id, before=now, window_days=90)
        assert b.basis == "unit" and b.n == 3 and b.value == 0.875 and b.lowest == 0.75
        # points at or after `before` are excluded (the current check's own prices)
        b2 = baseline_for_item(s, item.id, before=now - timedelta(days=5), window_days=90)
        assert b2.n == 2
