from datetime import datetime, timedelta

from sqlalchemy import select

from dealwatch.models import Item, Observation, PricePoint, Retailer, Run
from dealwatch.services.insights import item_insight, overview, retailer_breakdown
from dealwatch.services.llm import FakeSearchClient
from dealwatch.services.pricing import baseline_for_item
from dealwatch.services.runner import execute_run
from dealwatch.services.sources import FakeSource, enabled_sources
from dealwatch.web.charts import Series, line_chart, nice_ticks, sparkline

NOW = datetime(2026, 9, 14, 7, 0)


def test_price_points_recorded_per_item_per_run(session_factory, env):
    run_id = execute_run(session_factory, FakeSearchClient(seed=1), env=env, now=NOW, sources=[])
    with session_factory() as s:
        points = list(s.scalars(select(PricePoint).where(PricePoint.run_id == run_id)))
        found_items = {o.item_id for o in s.scalars(select(Observation).where(Observation.run_id == run_id))}
        assert found_items and {p.item_id for p in points} == found_items and len(points) == len(found_items)
        for p in points:
            assert p.min_price <= p.median_price <= p.max_price and p.min_price <= p.avg_price <= p.max_price
            assert p.n_offers >= 1 and p.n_retailers >= 1 and p.best_domain and p.best_url.startswith("https://")
            assert p.sources == "llm_search" and p.observed_at == NOW and p.best_price >= p.min_price
            if p.unit:
                assert p.min_unit_price is not None and p.best_unit_price == p.min_unit_price


def test_baseline_uses_price_points_and_report_has_tracker(session_factory, env):
    execute_run(session_factory, FakeSearchClient(seed=1), env=env, now=NOW, sources=[])
    execute_run(session_factory, FakeSearchClient(seed=2), env=env, now=NOW + timedelta(days=7), sources=[])
    third = execute_run(session_factory, FakeSearchClient(seed=3), env=env, now=NOW + timedelta(days=14), sources=[])
    with session_factory() as s:
        before = NOW + timedelta(days=14)
        for item in s.scalars(select(Item)):
            prior = [p for p in s.scalars(select(PricePoint).where(PricePoint.item_id == item.id)) if p.observed_at < before]
            b = baseline_for_item(s, item.id, before=before, window_days=90)
            assert b.n == len(prior)
            if prior:
                assert b.basis == "unit" and b.value is not None and "median best price" in b.label or b.n < 3
        report = s.get(Run, third).report.markdown
        assert "## 📈 Price tracker" in report and "compared with their previous check" in report
        assert "Lowest ever" in report  # column in the no-deal table


def test_sources_run_for_all_active_items_without_llm(session_factory, env):
    with session_factory() as s:
        for item in s.scalars(select(Item)):  # nothing is due for the LLM
            item.last_checked_at = NOW - timedelta(hours=1)
        s.commit()
    run_id = execute_run(session_factory, FakeSearchClient(), env=env, now=NOW, sources=[FakeSource()])
    with session_factory() as s:
        run = s.get(Run, run_id)
        assert run.status == "success" and run.groups_planned == 0 and run.api_requests == 0
        obs = list(s.scalars(select(Observation).where(Observation.run_id == run_id)))
        assert obs and all(o.source == "fake_folder" and o.retailer_domain == "ah.nl" and o.retailer_id is not None for o in obs)
        assert {o.item.category for o in obs} <= {"household", "personal_care", "groceries"}
        points = list(s.scalars(select(PricePoint).where(PricePoint.run_id == run_id)))
        assert len(points) == len(obs) and all(p.sources == "fake_folder" for p in points)
        assert '"fake_folder"' in run.log_json and run.deal_count >= 0
        assert all(i.last_checked_at == NOW - timedelta(hours=1) for i in s.scalars(select(Item)))  # sources don't count as LLM checks
        assert "Price tracker" in run.report.markdown


def test_source_respects_retailer_status_and_survives_failures(session_factory, env):
    with session_factory() as s:
        s.scalar(select(Retailer).where(Retailer.domain == "ah.nl")).status = "disabled"
        s.commit()
        assert enabled_sources([FakeSource()], list(s.scalars(select(Retailer)))) == []
    run_id = execute_run(session_factory, FakeSearchClient(), env=env, now=NOW, sources=[FakeSource()])
    with session_factory() as s:
        assert not any(o.source == "fake_folder" for o in s.scalars(select(Observation).where(Observation.run_id == run_id)))
        s.scalar(select(Retailer).where(Retailer.domain == "ah.nl")).status = "enabled"
        s.commit()
    run_id = execute_run(session_factory, FakeSearchClient(), env=env, now=NOW + timedelta(days=8), sources=[FakeSource(fail=True)])
    with session_factory() as s:
        run = s.get(Run, run_id)
        assert run.status == "success" and "simulated source outage" in run.log_json


def _add_points(session, item_id, unit_values, domains=("ah.nl",), start=NOW - timedelta(days=60)):
    for i, v in enumerate(unit_values):
        run = Run(started_at=start + timedelta(days=7 * i), status="success")
        session.add(run)
        session.flush()
        session.add(
            PricePoint(
                item_id=item_id, run_id=run.id, observed_at=run.started_at, n_offers=2, n_retailers=2, unit="roll",
                min_price=v * 8, avg_price=v * 8 * 1.1, median_price=v * 8 * 1.05, max_price=v * 8 * 1.2,
                min_unit_price=v, avg_unit_price=v * 1.1, median_unit_price=v * 1.05, max_unit_price=v * 1.2,
                best_price=v * 8, best_unit_price=v, best_pack_quantity=8, best_domain=domains[i % len(domains)], best_url="https://x.nl/1",
                promo_count=1 if i % 2 else 0,
            )
        )
    session.commit()


def test_item_insight_statistics(session_factory):
    with session_factory() as s:
        item = s.scalar(select(Item).where(Item.name == "Kitchen towels"))
        _add_points(s, item.id, [0.90, 0.88, 0.86, 0.84, 0.82, 0.70], domains=("ah.nl", "jumbo.com"))
        ins = item_insight(s, item, NOW)
        empty = item_insight(s, s.scalar(select(Item).where(Item.name == "Shampoo")), NOW)
    assert ins.basis == "unit" and ins.n_points == 6 and ins.unit_label == "/roll"
    assert ins.last.best == 0.70 and ins.prev.best == 0.82 and ins.change_vs_prev_pct == -14.6
    assert ins.low == 0.70 and ins.at_all_time_low and ins.is_new_low and ins.high == 0.90 and ins.low_domain == "jumbo.com"
    assert ins.trend == "falling" and ins.trend_pct_per_check < 0
    assert ins.best_domain == "ah.nl" and ins.best_domain_share_pct == 50 and ins.promo_share_pct == 50
    assert ins.avg_30d == 0.70 and ins.change_vs_30d_pct == 0 and ins.volatility_pct > 0 and ins.days_tracked == 60
    assert empty.n_points == 0 and empty.trend == "n/a" and not empty.at_all_time_low and not empty.is_new_low
    with session_factory() as s:
        flat = s.scalar(select(Item).where(Item.name == "Toothpaste"))
        _add_points(s, flat.id, [0.5, 0.5])
        flat_ins = item_insight(s, flat, NOW)
    assert flat_ins.at_all_time_low and not flat_ins.is_new_low


def test_overview_movers_leaderboard_and_breakdown(session_factory, env):
    with session_factory() as s:
        towels = s.scalar(select(Item).where(Item.name == "Kitchen towels"))
        paper = s.scalar(select(Item).where(Item.name == "Toilet paper"))
        _add_points(s, towels.id, [0.9, 0.7], domains=("ah.nl",))
        _add_points(s, paper.id, [0.5, 0.6], domains=("jumbo.com",))
        ov = overview(s, NOW)
        assert ov.items_tracked == 2 and ov.points == 4 and ov.runs == 4
        assert [i.item.id for i in ov.movers_down] == [towels.id] and [i.item.id for i in ov.movers_up] == [paper.id]
        assert [i.item.id for i in ov.at_low] == [towels.id] and set(ov.leaderboard) == {("ah.nl", 2), ("jumbo.com", 2)}
    run_id = execute_run(session_factory, FakeSearchClient(seed=4), env=env, now=NOW, sources=[FakeSource()])
    with session_factory() as s:
        item = s.scalar(select(Item).where(Item.name == "Kitchen towels"))
        stats = retailer_breakdown(s, item.id, "unit")
        assert stats and stats == sorted(stats, key=lambda r: (r.min_price, -r.wins))
        assert any(r.domain == "ah.nl" for r in stats) and all(r.n_offers >= 1 and r.last_seen for r in stats)


def test_charts():
    pts = [(NOW - timedelta(days=7 * i), 1.0 + 0.1 * i) for i in range(4)]
    html = line_chart([Series("Best", pts), Series("Avg", [(d, v * 1.2) for d, v in pts])], chart_id="c1", title="T", unit_suffix="/roll")
    assert html.count("<path") == 2 and html.count("<circle") == 8 and "viz-legend" in html and 'class="viz-data"' in html and "viz-crosshair" in html
    assert "€1.00/roll" in html or "€1.20/roll" in html  # sparse end labels carry the unit
    single = line_chart([Series("Best", [pts[0]])], chart_id="c2", title="T")
    assert "<path" in single and "viz-legend" in single
    assert line_chart([], chart_id="c3", title="T").startswith("<p")
    assert "<svg" in sparkline([1, 2, 1.5]) and "-" in sparkline([1])
    assert nice_ticks(0.61, 0.93) == [0.6, 0.7, 0.8, 0.9, 1.0]
    assert nice_ticks(10, 10) == [10.0, 11.0] or nice_ticks(10, 10)[0] == 10.0
