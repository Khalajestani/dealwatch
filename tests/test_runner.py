from datetime import date, datetime, timedelta

from sqlalchemy import select

from dealwatch.models import Item, Observation, Report, Retailer, Run, RunItem
from dealwatch.services.llm import FakeSearchClient, GroupSearchResult, UsageTotals
from dealwatch.services.prompts import GroupOutput, ItemResult, Offer
from dealwatch.services.runner import execute_run
from dealwatch.settings import save_settings

NOW = datetime(2026, 9, 14, 7, 0)


def test_full_run_with_fake_client(session_factory, env):
    run_id = execute_run(session_factory, FakeSearchClient(seed=1), env=env, trigger="cli", now=NOW)
    with session_factory() as s:
        run = s.get(Run, run_id)
        assert run.status == "success" and run.items_checked == 8 and run.groups_completed == 4
        assert run.web_searches == 8 and run.cost_usd > 0 and run.api_requests == 4
        assert s.scalar(select(Observation).where(Observation.run_id == run_id)) is not None
        report = s.scalar(select(Report).where(Report.run_id == run_id))
        assert "Deals worth a look" in report.markdown and "<h2>" in report.html
        assert all(i.last_checked_at == NOW for i in s.scalars(select(Item)))
        assert len(list(s.scalars(select(RunItem).where(RunItem.run_id == run_id)))) == 8
        # first run: no baseline yet
        assert all(o.baseline_price is None for o in s.scalars(select(Observation).where(Observation.run_id == run_id)))
    assert (env.reports_dir / f"run-{run_id:04d}.md").exists()

    # nothing is due right after a run
    second = execute_run(session_factory, FakeSearchClient(seed=1), env=env, now=NOW + timedelta(hours=1))
    with session_factory() as s:
        run2 = s.get(Run, second)
        assert run2.status == "success" and run2.items_planned == 0 and run2.groups_planned == 0

    # a week later: due again, and now baselines exist
    third = execute_run(session_factory, FakeSearchClient(seed=2), env=env, now=NOW + timedelta(days=7))
    with session_factory() as s:
        obs = list(s.scalars(select(Observation).where(Observation.run_id == third)))
        assert obs and any(o.baseline_price is not None for o in obs)
        assert s.get(Run, third).status == "success"


def test_dry_run_only_plans(session_factory, env):
    run_id = execute_run(session_factory, FakeSearchClient(), env=env, dry_run=True, now=NOW)
    with session_factory() as s:
        run = s.get(Run, run_id)
        assert run.status == "planned" and run.dry_run and run.estimated_cost_usd > 0
        assert s.scalar(select(Observation).where(Observation.run_id == run_id)) is None
        assert "Dealwatch plan" in s.scalar(select(Report).where(Report.run_id == run_id)).markdown
        assert all(i.last_checked_at is None for i in s.scalars(select(Item)))


def test_failed_batch_marks_partial_and_keeps_items_due(session_factory, env):
    run_id = execute_run(session_factory, FakeSearchClient(fail_group=1), env=env, now=NOW)
    with session_factory() as s:
        run = s.get(Run, run_id)
        assert run.status == "partial" and run.groups_failed == 1 and run.groups_completed == 3
        errored = list(s.scalars(select(RunItem).where(RunItem.run_id == run_id, RunItem.status == "error")))
        assert len(errored) == 4  # the household batch
        assert all(s.get(Item, ri.item_id).last_checked_at is None for ri in errored)
        assert "Errors" in s.scalar(select(Report).where(Report.run_id == run_id)).markdown


class _LeakyClient:
    """Returns offers from domains outside the allowlist / blocked - the runner must drop them."""

    name = "leaky"

    def search_group(self, group, settings, today):
        item = group.items[0]
        offers = [
            Offer(retailer_domain="kruidvat.nl", title="blocked shop", url="https://www.kruidvat.nl/p/1", price=1.0, pack_quantity=1, unit=item.unit, evidence="x", confidence=0.9),
            Offer(retailer_domain="evil.example", title="not allowed", url="https://evil.example/p", price=1.0, pack_quantity=1, unit=item.unit, evidence="x", confidence=0.9),
            Offer(retailer_domain="ah.nl", title="fine", url="https://www.ah.nl/producten/1", price=5.0, list_price=8.0, pack_quantity=8, unit=item.unit, promo_type="sale", evidence="x", confidence=0.9),
        ]
        results = [ItemResult(item_id=item.id, status="found", offers=offers)] + [ItemResult(item_id=i.id, status="not_found", note="skip") for i in group.items[1:]]
        return GroupSearchResult(group_index=group.index, model="leaky", output=GroupOutput(results=results), usage=UsageTotals(api_requests=1, web_searches=1))


def test_offers_outside_allowlist_are_dropped(session_factory, env):
    with session_factory() as s:
        s.scalar(select(Retailer).where(Retailer.domain == "kruidvat.nl")).status = "blocked"
        s.commit()
    run_id = execute_run(session_factory, _LeakyClient(), env=env, now=NOW, force=True)
    with session_factory() as s:
        obs = list(s.scalars(select(Observation).where(Observation.run_id == run_id)))
        assert {o.retailer_domain for o in obs} == {"ah.nl"}
        kept = obs[0]
        assert kept.retailer_id is not None and kept.unit_price == 0.625 and kept.is_deal and kept.discount_pct == 37.5
        run = s.get(Run, run_id)
        assert run.deal_count >= 1 and "domain kruidvat.nl not permitted" in run.log_json and "evil.example not permitted" in run.log_json
        not_found = list(s.scalars(select(RunItem).where(RunItem.run_id == run_id, RunItem.status == "not_found")))
        assert not_found and all(ri.note in ("skip", None) for ri in not_found)  # None = all offers dropped


def test_settings_change_group_size(session_factory, env):
    with session_factory() as s:
        assert save_settings(s, {"group_size": "2", "max_groups_per_run": "2"}) == []
    run_id = execute_run(session_factory, FakeSearchClient(), env=env, now=NOW)
    with session_factory() as s:
        run = s.get(Run, run_id)
        assert run.groups_planned == 2 and run.items_planned == 4 and run.items_checked == 4
