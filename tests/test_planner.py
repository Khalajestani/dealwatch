from datetime import datetime, timedelta

from sqlalchemy import select

from dealwatch.models import Item, Retailer
from dealwatch.services.planner import build_plan, is_due
from dealwatch.settings import Settings

NOW = datetime(2026, 9, 14, 7, 0)


def test_is_due_rules():
    s = Settings()
    assert is_due(Item(name="a", active=True, priority="stock_up"), NOW, s)
    assert not is_due(Item(name="a", active=False), NOW, s)
    assert not is_due(Item(name="a", active=True, priority="stock_up", last_checked_at=NOW - timedelta(days=1)), NOW, s)
    assert is_due(Item(name="a", active=True, priority="stock_up", last_checked_at=NOW - timedelta(days=7)), NOW, s)
    assert is_due(
        Item(name="a", active=True, priority="stock_up", last_checked_at=NOW - timedelta(days=6, hours=13)), NOW, s
    )
    assert not is_due(Item(name="a", active=True, priority="wishlist", last_checked_at=NOW - timedelta(days=7)), NOW, s)
    assert is_due(
        Item(name="a", active=True, priority="need", check_interval_days=1, last_checked_at=NOW - timedelta(days=1)),
        NOW,
        s,
    )
    assert not is_due(Item(name="a", active=True, snooze_until=NOW + timedelta(days=3)), NOW, s)


def test_groups_by_category_with_category_aware_domains(session_factory):
    with session_factory() as s:
        plan = build_plan(s, Settings(), now=NOW)
    assert [g.category for g in plan.groups] == [
        "household",
        "personal_care",
        "groceries",
        "tools_diy",
    ]  # wishlist last
    household = plan.groups[0]
    assert len(household.items) == 4 and household.max_uses == 9
    assert "kruidvat.nl" in household.allowed_domains and "mediamarkt.nl" not in household.allowed_domains
    tools = plan.groups[3]
    assert "gamma.nl" in tools.allowed_domains and "ah.nl" not in tools.allowed_domains
    assert tools.max_uses == 3 and tools.blocked_domains == []
    assert plan.items_planned == 8 and not plan.deferred and not plan.not_due


def test_batch_cap_defers_least_urgent(session_factory):
    with session_factory() as s:
        plan = build_plan(s, Settings(max_groups_per_run=2), now=NOW)
    assert len(plan.groups) == 2 and len(plan.deferred) == 2
    assert {g.category for g in plan.groups} == {"household", "personal_care"}
    assert any("deferred" in w for w in plan.warnings)


def test_group_size_chunks(session_factory):
    with session_factory() as s:
        plan = build_plan(s, Settings(group_size=3), now=NOW)
    assert [len(g.items) for g in plan.groups][:2] == [3, 1]


def test_open_mode_uses_blocklist(session_factory):
    with session_factory() as s:
        kruidvat = s.scalar(select(Retailer).where(Retailer.domain == "kruidvat.nl"))
        kruidvat.status = "blocked"
        s.commit()
        plan = build_plan(s, Settings(search_mode="open"), now=NOW)
    g = plan.groups[0]
    assert g.allowed_domains == [] and g.blocked_domains == ["kruidvat.nl"]
    assert all(r["domain"] != "kruidvat.nl" for r in g.retailers)


def test_nothing_due_after_check(session_factory):
    with session_factory() as s:
        for item in s.scalars(select(Item)):
            item.last_checked_at = NOW - timedelta(hours=2)
        s.commit()
        plan = build_plan(s, Settings(), now=NOW)
        assert not plan.groups and len(plan.not_due) == 8
        forced = build_plan(s, Settings(), now=NOW, force=True)
        assert forced.items_planned == 8


def test_no_enabled_retailers_warns(session_factory):
    with session_factory() as s:
        for r in s.scalars(select(Retailer)):
            r.status = "disabled"
        s.commit()
        plan = build_plan(s, Settings(), now=NOW)
    assert not plan.groups and any("No enabled retailers" in w for w in plan.warnings)
