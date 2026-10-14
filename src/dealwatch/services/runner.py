"""One price check: plan -> one Claude run per batch -> store offers -> assess deals -> record price points -> report.

Only one check can be active per process (`_run_lock`); the daily schedule and Run now both go through
`execute_run`. Batches run one after another, so the Pi only ever runs one Claude Code for a check. Every step
records what happened on the Run row, so the run page shows lookups and errors even when something failed half-way.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..domains import domain_matches, domain_of_url, normalize_domain
from ..models import Item, Observation, PricePoint, Report, Retailer, Run, RunItem, utcnow
from ..settings import Settings, get_settings
from .planner import PlannedGroup, RunPlan, build_plan
from .pricing import Baseline, assess_offer, baseline_for_item, summarize_offers, unit_price
from .report import build_report, render_html
from .search import GroupSearchResult, SearchClient

log = logging.getLogger(__name__)
_run_lock = threading.Lock()


class RunInProgress(RuntimeError):
    pass


def is_run_active() -> bool:
    return _run_lock.locked()


def execute_run(
    session_factory: sessionmaker[Session],
    search_client: SearchClient,
    *,
    trigger: str = "manual",
    force: bool = False,
    now: datetime | None = None,
) -> int:
    """Run the pipeline and return the Run id. Raises RunInProgress if another check is active."""
    if not _run_lock.acquire(blocking=False):
        raise RunInProgress("A price check is already in progress")
    try:
        return _execute(session_factory, search_client, trigger=trigger, force=force, now=now or utcnow())
    finally:
        _run_lock.release()


def _execute(session_factory, search_client, *, trigger, force, now) -> int:
    with session_factory() as session:
        settings = get_settings(session)
        plan = build_plan(session, settings, now=now, force=force)
        run = Run(
            trigger=trigger,
            status="running",
            started_at=now,
            model=settings.search_model,
            effort=settings.search_effort,
            search_mode=settings.search_mode,
            groups_planned=len(plan.groups),
            items_planned=plan.items_planned,
            plan_json=json.dumps(plan.to_dict(), ensure_ascii=False),
        )
        session.add(run)
        session.commit()
        run_id = run.id
    log.info("run %s started (%s): %d batches, %d items", run_id, trigger, len(plan.groups), plan.items_planned)

    log_data: dict[str, Any] = {"lookups": {}, "errors": [], "dropped_offers": [], "warnings": list(plan.warnings)}
    try:
        stopped: str | None = None
        for group in plan.groups:
            if stopped:  # plan limit or failed sign-in: the rest waits for the next check, nothing is retried now
                result = GroupSearchResult(group_index=group.index, model=settings.search_model, error=stopped)
            else:
                try:
                    result = search_client.search_group(group, settings, now.date())
                except Exception as exc:  # a bug in one batch must not stop the others
                    log.exception("batch %s crashed", group.index)
                    result = GroupSearchResult(group.index, settings.search_model, error=f"unexpected error: {exc!r}")
                if result.stop:
                    stopped = result.error
            with session_factory() as session:
                _persist_group(session, run_id, group, result, log_data, now)
        with session_factory() as session:
            run = session.get(Run, run_id)
            _assess_run(session, run, settings)
            _record_price_points(session, run)
            _finalize(session, run, plan, settings, log_data)
    except Exception as exc:
        log.exception("run %s failed", run_id)
        with session_factory() as session:
            run = session.get(Run, run_id)
            run.status = "failed"
            run.error = repr(exc)
            run.finished_at = utcnow()
            run.log_json = json.dumps(log_data, ensure_ascii=False)
            session.commit()
        raise
    return run_id


def _domain_permitted(domain: str, group: PlannedGroup) -> bool:
    if group.allowed_domains:
        return any(domain_matches(domain, allowed) for allowed in group.allowed_domains)
    if group.blocked_domains:
        return not any(domain_matches(domain, blocked) for blocked in group.blocked_domains)
    return True


def _match_retailer(domain: str, retailers: list[Retailer]) -> Retailer | None:
    return next((r for r in retailers if domain_matches(domain, r.domain)), None)


def _persist_group(
    session: Session, run_id: int, group: PlannedGroup, result: GroupSearchResult, log_data: dict, now: datetime
) -> None:
    """Store what the batch recorded. Items it did not get to are errors when the batch failed (they stay due),
    and not found when it finished without them."""
    run = session.get(Run, run_id)
    run.web_lookups += len(result.lookups)
    if result.lookups:
        log_data["lookups"][str(group.index)] = result.lookups
    if result.error:
        run.groups_failed += 1
        log_data["errors"].append({"batch": group.index, "error": result.error})
    else:
        run.groups_completed += 1

    retailers = list(session.scalars(select(Retailer)))
    by_item = {r.item_id: r for r in result.results}
    for planned in group.items:
        item = session.get(Item, planned.id)
        if item is None:
            continue
        item_result = by_item.get(planned.id)
        if item_result is None and result.error:
            session.add(
                RunItem(run_id=run_id, item_id=item.id, group_index=group.index, status="error", note=result.error)
            )
            continue
        kept = 0
        for offer in item_result.offers if item_result else []:
            domain = domain_of_url(offer.url) or normalize_domain(offer.retailer_domain)
            reason = None
            if not domain or offer.price <= 0:
                reason = "no domain or price"
            elif not _domain_permitted(domain, group):
                reason = f"domain {domain} not permitted"
            retailer = _match_retailer(domain, retailers) if domain else None
            if reason is None and retailer is not None and retailer.status == "blocked":
                reason = f"retailer {domain} is blocked"
            if reason:
                log_data["dropped_offers"].append({"item": planned.display_name, "reason": reason, "url": offer.url})
                continue
            session.add(
                Observation(
                    run_id=run_id,
                    item_id=item.id,
                    retailer_id=retailer.id if retailer else None,
                    retailer_domain=domain,
                    title=offer.title[:500],
                    url=offer.url[:2000],
                    price=offer.price,
                    currency=offer.currency,
                    list_price=offer.list_price,
                    pack_quantity=offer.pack_quantity,
                    unit=offer.unit or item.unit or None,
                    unit_price=unit_price(offer.price, offer.pack_quantity),
                    promo_type=offer.promo_type,
                    promo_text=offer.promo_text,
                    valid_until=offer.valid_until_date,
                    confidence=offer.confidence,
                    evidence=(offer.evidence or "")[:1000] or None,
                    observed_at=now,
                )
            )
            kept += 1
        note = (item_result.note if item_result else "Claude recorded nothing for this item") or None
        session.add(
            RunItem(
                run_id=run_id,
                item_id=item.id,
                group_index=group.index,
                status="found" if kept else "not_found",
                note=note,
            )
        )
        run.items_found += 1 if kept else 0
        run.items_checked += 1
        item.last_checked_at = now
    session.commit()


def _assess_run(session: Session, run: Run, settings: Settings) -> None:
    observations = list(session.scalars(select(Observation).where(Observation.run_id == run.id)))
    baselines: dict[int, Baseline] = {}
    items = {i.id: i for i in session.scalars(select(Item))}
    for obs in observations:
        item = items.get(obs.item_id)
        if item is None:
            continue
        if obs.item_id not in baselines:
            baselines[obs.item_id] = baseline_for_item(
                session, obs.item_id, before=run.started_at, window_days=settings.baseline_window_days
            )
        baseline = baselines[obs.item_id]
        assessment = assess_offer(
            price=obs.price,
            unit_price_value=obs.unit_price,
            list_price=obs.list_price,
            promo_type=obs.promo_type,
            confidence=obs.confidence,
            baseline=baseline,
            item=item,
            settings=settings,
        )
        obs.baseline_price = baseline.value
        obs.baseline_basis = baseline.basis
        obs.discount_pct = assessment.discount_pct
        obs.under_target = assessment.under_target
        obs.is_deal = assessment.is_deal
        obs.score = assessment.score
        obs.reasons = "; ".join(assessment.reasons) or None
    run.deal_count = len({obs.item_id for obs in observations if obs.is_deal})
    session.commit()


def _record_price_points(session: Session, run: Run) -> None:
    """One PricePoint per item that had offers this run: the tracked time series."""
    by_item: dict[int, list[Observation]] = defaultdict(list)
    for obs in session.scalars(select(Observation).where(Observation.run_id == run.id)):
        by_item[obs.item_id].append(obs)
    for item_id, observations in by_item.items():
        session.add(
            PricePoint(item_id=item_id, run_id=run.id, observed_at=run.started_at, **summarize_offers(observations))
        )
    session.commit()


def _finalize(session: Session, run: Run, plan: RunPlan, settings: Settings, log_data: dict) -> None:
    run.finished_at = utcnow()
    if run.groups_planned and run.groups_completed == 0:
        run.status = "failed"
        run.error = log_data["errors"][0]["error"] if log_data["errors"] else "every batch failed"
    elif run.groups_failed:
        run.status = "partial"
    else:
        run.status = "success"
    run.log_json = json.dumps(log_data, ensure_ascii=False)
    markdown = build_report(session, run, plan, settings)
    session.add(Report(run_id=run.id, markdown=markdown, html=render_html(markdown)))
    session.commit()
