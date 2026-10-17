"""Already bought: kept with its history, left out of the checks and of everything that reports deals."""

from datetime import datetime

from conftest import FakeSearchClient
from sqlalchemy import select, text

from dealwatch.db import init_db, make_engine
from dealwatch.items import apply_action
from dealwatch.models import Item, Observation, Run, RunItem
from dealwatch.queries import current_deals
from dealwatch.services.insights import all_item_insights
from dealwatch.services.planner import build_plan, is_due
from dealwatch.services.report import build_report
from dealwatch.services.runner import execute_run
from dealwatch.settings import Settings

NOW = datetime(2026, 9, 14, 7, 0)


def planned_ids(session, **kwargs) -> set[int]:
    return {i.id for g in build_plan(session, Settings(), now=NOW, **kwargs).groups for i in g.items}


def test_items_from_before_the_change_are_active(tmp_path):
    engine = make_engine(tmp_path / "old.db")
    init_db(engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE items DROP COLUMN status"))
        conn.execute(text("ALTER TABLE items DROP COLUMN bought_at"))
        conn.execute(
            text(
                "INSERT INTO items (name, category, priority, bulk_ok, active, created_at, updated_at) "
                "VALUES ('Toilet paper', 'household', 'stock_up', 1, 1, '2026-09-01', '2026-09-01')"
            )
        )
    init_db(engine)  # what the app does when it starts
    with engine.connect() as conn:
        assert tuple(conn.execute(text("SELECT status, bought_at FROM items")).one()) == ("active", None)


def test_bought_items_are_not_searched_until_undone(session_factory):
    with session_factory() as s:
        drill = s.scalar(select(Item).where(Item.name == "Cordless drill"))
        assert drill.status == "active" and drill.id in planned_ids(s)
        apply_action(s, drill, "already_bought", Settings())
        assert drill.status == "bought" and drill.bought_at is not None
        assert not is_due(drill, NOW, Settings())
        assert drill.id not in planned_ids(s) and drill.id not in planned_ids(s, force=True)
        assert drill.display_name not in build_plan(s, Settings(), now=NOW).not_due
        drill_id = drill.id

    run_id = execute_run(session_factory, FakeSearchClient(seed=1), now=NOW)
    with session_factory() as s:
        assert s.scalar(select(RunItem).where(RunItem.run_id == run_id, RunItem.item_id == drill_id)) is None
        drill = s.get(Item, drill_id)
        apply_action(s, drill, "undo_bought", Settings())
        assert drill.status == "active" and drill.bought_at is None and drill.last_bought_at is not None
        assert drill_id in planned_ids(s)


def test_bought_items_leave_deals_drops_and_reports_but_keep_their_history(session_factory):
    run_id = execute_run(session_factory, FakeSearchClient(seed=1), now=NOW)
    with session_factory() as s:
        run = s.get(Run, run_id)
        offer = s.scalar(select(Observation).where(Observation.run_id == run_id))
        offer.is_deal, offer.valid_until = True, None
        s.commit()
        item = s.get(Item, offer.item_id)
        assert item.display_name in build_report(s, run, None, Settings())
        assert item.id in {i.id for _, i in current_deals(s, NOW.date())}
        assert item.id in {i.item.id for i in all_item_insights(s, NOW)}

        apply_action(s, item, "already_bought", Settings())
        assert item.display_name not in build_report(s, run, None, Settings()), "bought while the check ran"
        assert item.id not in {i.id for _, i in current_deals(s, NOW.date())}
        assert item.id not in {i.item.id for i in all_item_insights(s, NOW)}, "no price drops or tracker"
        assert s.scalar(select(Observation).where(Observation.item_id == item.id)) is not None
