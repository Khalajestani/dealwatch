"""Orchestrates one run: plan -> search batches (parallel) -> store observations -> assess deals -> report -> notify.

Only one run can be active per process (`_run_lock`). Every step records what happened on the Run row so
the run page can show usage, cost, queries and errors even when something failed half-way.
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..config import Env, estimate_usd
from ..domains import domain_matches, domain_of_url, normalize_domain
from ..models import Item, Observation, Report, Retailer, Run, RunItem, utcnow
from ..settings import Settings, get_settings
from . import notify
from .llm import GroupSearchResult, SearchClient
from .planner import PlannedGroup, RunPlan, build_plan
from .pricing import Baseline, assess_offer, baseline_for_item, unit_price
from .report import build_report, render_html

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
    env: Env | None = None,
    trigger: str = "manual",
    dry_run: bool = False,
    force: bool = False,
    now: datetime | None = None,
) -> int:
    """Run the pipeline and return the Run id. Raises RunInProgress if another run is active."""
    if not _run_lock.acquire(blocking=False):
        raise RunInProgress("A run is already in progress")
    try:
        return _execute(session_factory, search_client, env=env, trigger=trigger, dry_run=dry_run, force=force, now=now or utcnow())
    finally:
        _run_lock.release()


def _execute(session_factory, search_client, *, env, trigger, dry_run, force, now) -> int:
    with session_factory() as session:
        settings = get_settings(session)
        plan = build_plan(session, settings, now=now, force=force)
        run = Run(
            trigger=trigger,
            status="running",
            dry_run=dry_run,
            started_at=now,
            model=settings.search_model if not dry_run else settings.search_model,
            effort=settings.search_effort,
            search_mode=settings.search_mode,
            groups_planned=len(plan.groups),
            items_planned=plan.items_planned,
            estimated_cost_usd=plan.estimate.usd,
            plan_json=json.dumps(plan.to_dict(), ensure_ascii=False),
        )
        session.add(run)
        session.commit()
        run_id = run.id
    log.info("run %s started (%s): %d batches, %d items", run_id, trigger, len(plan.groups), plan.items_planned)

    log_data: dict[str, Any] = {"queries": {}, "errors": [], "dropped_offers": [], "warnings": list(plan.warnings)}

    if dry_run or not plan.groups:
        with session_factory() as session:
            run = session.get(Run, run_id)
            run.status = "planned" if dry_run else "success"
            run.finished_at = utcnow()
            run.log_json = json.dumps(log_data, ensure_ascii=False)
            _save_report(session, run, plan, settings, env)
            session.commit()
        return run_id

    try:
        with ThreadPoolExecutor(max_workers=max(1, settings.parallel_groups)) as pool:
            futures = {pool.submit(search_client.search_group, group, settings, now.date()): group for group in plan.groups}
            for future in as_completed(futures):
                group = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # a bug in the client must not kill the whole run
                    log.exception("batch %s crashed", group.index)
                    result = GroupSearchResult(group_index=group.index, model=settings.search_model, error=f"unexpected error: {exc!r}")
                with session_factory() as session:
                    _persist_group(session, run_id, group, result, settings, log_data, now)
        with session_factory() as session:
            run = session.get(Run, run_id)
            _assess_run(session, run, settings)
            _finalize(session, run, plan, settings, log_data, env)
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
    for retailer in retailers:
        if domain_matches(domain, retailer.domain):
            return retailer
    return None


def _persist_group(session: Session, run_id: int, group: PlannedGroup, result: GroupSearchResult, settings: Settings, log_data: dict, now: datetime) -> None:
    run = session.get(Run, run_id)
    usage = result.usage
    run.api_requests += usage.api_requests
    run.input_tokens += usage.input_tokens
    run.output_tokens += usage.output_tokens
    run.cache_read_tokens += usage.cache_read_tokens
    run.cache_write_tokens += usage.cache_write_tokens
    run.web_searches += usage.web_searches
    if result.queries:
        log_data["queries"][str(group.index)] = result.queries

    if not result.ok:
        run.groups_failed += 1
        log_data["errors"].append({"batch": group.index, "error": result.error or "unknown error"})
        for planned in group.items:
            session.add(RunItem(run_id=run_id, item_id=planned.id, group_index=group.index, status="error", note=result.error))
        session.commit()
        return

    run.groups_completed += 1
    retailers = list(session.scalars(select(Retailer)))
    by_item = {r.item_id: r for r in result.output.results}
    for planned in group.items:
        item = session.get(Item, planned.id)
        if item is None:
            continue
        item_result = by_item.get(planned.id)
        kept = 0
        if item_result is not None:
            for offer in item_result.offers:
                domain = domain_of_url(offer.url) or normalize_domain(offer.retailer_domain)
                if not domain or offer.price <= 0:
                    log_data["dropped_offers"].append({"item": planned.display_name, "reason": "no domain/price", "url": offer.url})
                    continue
                if not _domain_permitted(domain, group):
                    log_data["dropped_offers"].append({"item": planned.display_name, "reason": f"domain {domain} not permitted", "url": offer.url})
                    continue
                retailer = _match_retailer(domain, retailers)
                if retailer is not None and retailer.status == "blocked":
                    log_data["dropped_offers"].append({"item": planned.display_name, "reason": f"retailer {domain} is blocked", "url": offer.url})
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
                        unit=(offer.unit or item.unit or None),
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
        status = "found" if kept else "not_found"
        note = (item_result.note if item_result else "model returned no entry for this item") or None
        session.add(RunItem(run_id=run_id, item_id=item.id, group_index=group.index, status=status, note=note))
        if kept:
            run.items_found += 1
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
            baselines[obs.item_id] = baseline_for_item(session, obs.item_id, before=run.started_at, window_days=settings.baseline_window_days)
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


def _finalize(session: Session, run: Run, plan: RunPlan, settings: Settings, log_data: dict, env: Env | None) -> None:
    run.finished_at = utcnow()
    run.cost_usd = round(
        estimate_usd(
            run.model or settings.search_model,
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            cache_read_tokens=run.cache_read_tokens,
            cache_write_tokens=run.cache_write_tokens,
            web_searches=run.web_searches,
        ),
        4,
    )
    if run.groups_planned and run.groups_completed == 0:
        run.status = "failed"
        run.error = "every batch failed - see log"
    elif run.groups_failed:
        run.status = "partial"
    else:
        run.status = "success"
    run.log_json = json.dumps(log_data, ensure_ascii=False)
    markdown = _save_report(session, run, plan, settings, env)
    session.commit()

    if env is not None and settings.notify_channel != "none" and not (settings.notify_only_deals and run.deal_count == 0):
        try:
            notify.send(env, settings, run, markdown)
        except Exception as exc:  # notification problems must not fail the run
            log.warning("notification failed: %s", exc)
            log_data["errors"].append({"notify": str(exc)})
            run.log_json = json.dumps(log_data, ensure_ascii=False)
            session.commit()


def _save_report(session: Session, run: Run, plan: RunPlan, settings: Settings, env: Env | None) -> str:
    markdown = build_report(session, run, plan, settings)
    existing = session.get(Report, run.report.id) if run.report else None
    if existing:
        existing.markdown, existing.html = markdown, render_html(markdown)
    else:
        session.add(Report(run_id=run.id, markdown=markdown, html=render_html(markdown)))
    if env is not None:
        try:
            env.reports_dir.mkdir(parents=True, exist_ok=True)
            (env.reports_dir / f"run-{run.id:04d}.md").write_text(markdown, encoding="utf-8")
        except OSError as exc:
            log.warning("could not write report file: %s", exc)
    return markdown
