from datetime import datetime, timedelta

from conftest import FakeSearchClient
from sqlalchemy import select

from dealwatch.models import Item, Observation, Report, Retailer, Run, RunItem
from dealwatch.services.runner import execute_run
from dealwatch.services.search import GroupSearchResult, ItemResult, Offer
from dealwatch.settings import save_settings

NOW = datetime(2026, 9, 14, 7, 0)


def test_full_check_with_fake_client(session_factory):
    run_id = execute_run(session_factory, FakeSearchClient(seed=1), trigger="schedule", now=NOW)
    with session_factory() as s:
        run = s.get(Run, run_id)
        assert run.status == "success" and run.items_checked == 8 and run.groups_completed == 4 and run.web_lookups == 8
        assert s.scalar(select(Observation).where(Observation.run_id == run_id)) is not None
        report = s.scalar(select(Report).where(Report.run_id == run_id))
        assert "Deals worth a look" in report.markdown and "<h2>" in report.html and "8 web lookups" in report.markdown
        assert all(i.last_checked_at == NOW for i in s.scalars(select(Item)))
        assert len(list(s.scalars(select(RunItem).where(RunItem.run_id == run_id)))) == 8
        # first check: no baseline yet
        assert all(o.baseline_price is None for o in s.scalars(select(Observation).where(Observation.run_id == run_id)))

    # nothing is due right after a check
    second = execute_run(session_factory, FakeSearchClient(seed=1), now=NOW + timedelta(hours=1))
    with session_factory() as s:
        run2 = s.get(Run, second)
        assert run2.status == "success" and run2.items_planned == 0 and run2.groups_planned == 0

    # a week later: due again, and now baselines exist
    third = execute_run(session_factory, FakeSearchClient(seed=2), now=NOW + timedelta(days=7))
    with session_factory() as s:
        obs = list(s.scalars(select(Observation).where(Observation.run_id == third)))
        assert obs and any(o.baseline_price is not None for o in obs)
        assert s.get(Run, third).status == "success"


def test_failed_batch_marks_partial_and_keeps_items_due(session_factory):
    run_id = execute_run(session_factory, FakeSearchClient(fail_group=1), now=NOW)
    with session_factory() as s:
        run = s.get(Run, run_id)
        assert run.status == "partial" and run.groups_failed == 1 and run.groups_completed == 3
        errored = list(s.scalars(select(RunItem).where(RunItem.run_id == run_id, RunItem.status == "error")))
        assert len(errored) == 4  # the household batch
        assert all(s.get(Item, ri.item_id).last_checked_at is None for ri in errored)
        assert "Errors" in s.scalar(select(Report).where(Report.run_id == run_id)).markdown


def test_a_reached_plan_limit_or_failed_sign_in_stops_the_rest_of_the_check(session_factory):
    client = FakeSearchClient(stop_group=1)
    run_id = execute_run(session_factory, client, now=NOW)
    with session_factory() as s:
        run = s.get(Run, run_id)
        assert client.calls == 1, "nothing more is sent once the plan's limit is reached"
        assert run.status == "failed" and run.groups_failed == 4 and run.error == "simulated batch failure"
        assert all(i.last_checked_at is None for i in s.scalars(select(Item))), "everything stays due"


class _PartialClient:
    """Recorded the first item, then the run was cut short."""

    def search_group(self, group, settings, today):
        first = group.items[0]
        offer = Offer(
            retailer_domain="ah.nl",
            title="x",
            url="https://www.ah.nl/p/1",
            price=3.0,
            pack_quantity=1,
            unit=first.unit,
            confidence=0.9,
        )
        return GroupSearchResult(
            group.index, "x", results=[ItemResult(item_id=first.id, offers=[offer])], error="stopped"
        )


def test_a_batch_cut_short_keeps_what_it_recorded(session_factory):
    run_id = execute_run(session_factory, _PartialClient(), now=NOW, force=True)
    with session_factory() as s:
        statuses = {
            ri.status for ri in s.scalars(select(RunItem).where(RunItem.run_id == run_id, RunItem.group_index == 1))
        }
        assert statuses == {"found", "error"} and s.get(Run, run_id).status == "failed"
        assert s.scalar(select(Observation).where(Observation.run_id == run_id)).retailer_domain == "ah.nl"


class _LeakyClient:
    """Returns offers from domains outside the allowlist or blocked: the runner must drop them."""

    def search_group(self, group, settings, today):
        item = group.items[0]
        offers = [
            Offer(
                retailer_domain="kruidvat.nl",
                title="blocked shop",
                url="https://www.kruidvat.nl/p/1",
                price=1.0,
                pack_quantity=1,
                unit=item.unit,
                evidence="x",
                confidence=0.9,
            ),
            Offer(
                retailer_domain="evil.example",
                title="not allowed",
                url="https://evil.example/p",
                price=1.0,
                pack_quantity=1,
                unit=item.unit,
                evidence="x",
                confidence=0.9,
            ),
            Offer(
                retailer_domain="ah.nl",
                title="fine",
                url="https://www.ah.nl/producten/1",
                price=5.0,
                list_price=8.0,
                pack_quantity=8,
                unit=item.unit,
                promo_type="sale",
                evidence="x",
                confidence=0.9,
            ),
        ]
        results = [ItemResult(item_id=item.id, status="found", offers=offers)]
        results += [ItemResult(item_id=i.id, status="not_found", note="skip") for i in group.items[1:]]
        return GroupSearchResult(group_index=group.index, model="leaky", results=results, lookups=["search: x"])


def test_offers_outside_allowlist_are_dropped(session_factory):
    with session_factory() as s:
        s.scalar(select(Retailer).where(Retailer.domain == "kruidvat.nl")).status = "blocked"
        s.commit()
    run_id = execute_run(session_factory, _LeakyClient(), now=NOW, force=True)
    with session_factory() as s:
        obs = list(s.scalars(select(Observation).where(Observation.run_id == run_id)))
        assert {o.retailer_domain for o in obs} == {"ah.nl"}
        kept = obs[0]
        assert kept.retailer_id is not None and kept.unit_price == 0.625 and kept.is_deal and kept.discount_pct == 37.5
        run = s.get(Run, run_id)
        assert (
            run.deal_count >= 1
            and "domain kruidvat.nl not permitted" in run.log_json
            and "evil.example not permitted" in run.log_json
        )
        not_found = list(s.scalars(select(RunItem).where(RunItem.run_id == run_id, RunItem.status == "not_found")))
        assert not_found and all(ri.note in ("skip", None) for ri in not_found)  # None: every offer was dropped


def test_settings_change_group_size(session_factory):
    with session_factory() as s:
        assert save_settings(s, {"group_size": "2", "max_groups_per_run": "2"}) == []
    run_id = execute_run(session_factory, FakeSearchClient(), now=NOW)
    with session_factory() as s:
        run = s.get(Run, run_id)
        assert run.groups_planned == 2 and run.items_planned == 4 and run.items_checked == 4
