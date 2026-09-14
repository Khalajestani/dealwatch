"""Orchestrates one run: plan -> direct sources -> LLM search batches (parallel) -> store observations -> assess deals
-> record price points -> report -> notify.

Only one run can be active per process (`_run_lock`). Every step records what happened on the Run row so
the run page can show usage, cost, queries and errors even when something failed half-way.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..config import Env, estimate_usd
from ..domains import domain_matches, domain_of_url, normalize_domain
from ..models import Item, Observation, PricePoint, Report, Retailer, Run, RunItem, utcnow
from ..settings import Settings, get_settings
from . import notify
from .llm import GroupSearchResult, SearchClient
from .planner import PlannedGroup, RunPlan, active_item_views, build_plan
from .pricing import Baseline, assess_offer, baseline_for_item, summarize_offers, unit_price
from .sources import SOURCES, DealSource, enabled_sources
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
    sources: list[DealSource] | None = None,
) -> int:
    """Run the pipeline and return the Run id. Raises RunInProgress if another run is active.

    `sources` are direct deal sources (see services/sources); default = the registered SOURCES.
    """
    if not _run_lock.acquire(blocking=False):
        raise RunInProgress("A run is already in progress")
    try:
        return _execute(
            session_factory, search_client, env=env, trigger=trigger, dry_run=dry_run, force=force, now=now or utcnow(),
            sources=SOURCES if sources is None else sources,
        )
    finally:
        _run_lock.release()


def _execute(session_factory, search_client, *, env, trigger, dry_run, force, now, sources) -> int:
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

    log_data: dict[str, Any] = {"queries": {}, "errors": [], "dropped_offers": [], "sources": {}, "warnings": list(plan.warnings)}

    if dry_run:
        with session_factory() as session:
            run = session.get(Run, run_id)
            run.status = "planned"
            run.finished_at = utcnow()
            run.log_json = json.dumps(log_data, ensure_ascii=False)
            _save_report(session, run, plan, settings, env)
            session.commit()
        return run_id

    try:
        if sources:
            with session_factory() as session:
                _run_sources(session, run_id, sources, settings, log_data, now)
        if plan.groups:
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
            _record_price_points(session, run)
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


def _run_sources(session: Session, run_id: int, sources: list[DealSource], settings: Settings, log_data: dict, now: datetime) -> None:
    """Direct deal sources (folders/APIs) run for every active item; their offers join the run like LLM offers."""
    retailers = list(session.scalars(select(Retailer)))
    items = active_item_views(session, now)
    item_ids = {i.id for i in items}
    for source in enabled_sources(sources, retailers):
        try:
            offers = source.fetch(items, settings, now.date())
        except Exception as exc:  # a broken adapter must not kill the run
            log.exception("source %s failed", source.name)
            log_data["errors"].append({"source": source.name, "error": repr(exc)})
            continue
        stored = 0
        for source_offer in offers:
            offer = source_offer.offer
            if source_offer.item_id not in item_ids or offer.price <= 0:
                continue
            domain = domain_of_url(offer.url) or normalize_domain(offer.retailer_domain) or source.domain
            if not domain_matches(domain, source.domain):
                log_data["dropped_offers"].append({"item": source_offer.item_id, "reason": f"source {source.name} reported foreign domain {domain}", "url": offer.url})
                continue
            retailer = _match_retailer(domain, retailers)
            unit = offer.unit or next((i.unit for i in items if i.id == source_offer.item_id), None)
            session.add(
                Observation(
                    run_id=run_id, item_id=source_offer.item_id, retailer_id=retailer.id if retailer else None, retailer_domain=domain,
                    title=offer.title[:500], url=offer.url[:2000], price=offer.price, currency=offer.currency, list_price=offer.list_price,
                    pack_quantity=offer.pack_quantity, unit=unit, unit_price=unit_price(offer.price, offer.pack_quantity),
                    promo_type=offer.promo_type, promo_text=offer.promo_text, valid_until=offer.valid_until_date, confidence=offer.confidence,
                    evidence=(offer.evidence or "")[:1000] or None, source=source.name, observed_at=now,
                )
            )
            stored += 1
        log_data["sources"][source.name] = {"offers": stored, "items_offered": len(items)}
    session.commit()


def _record_price_points(session: Session, run: Run) -> None:
    """One PricePoint per item that had offers this run - the tracked time series."""
    by_item: dict[int, list[Observation]] = defaultdict(list)
    for obs in session.scalars(select(Observation).where(Observation.run_id == run.id)):
        by_item[obs.item_id].append(obs)
    for item_id, observations in by_item.items():
        session.add(PricePoint(item_id=item_id, run_id=run.id, observed_at=run.started_at, **summarize_offers(observations)))
    session.commit()


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
                        source="llm_search",
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
