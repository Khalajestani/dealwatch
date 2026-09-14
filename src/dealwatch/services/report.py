"""Markdown report per run, rendered to HTML for the web UI and e-mail."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import markdown as md
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Item, Observation, Run, RunItem
from ..settings import Settings
from .planner import RunPlan

_LINK_TEXT_BAD = re.compile(r"[\[\]\n\r]")


def money(value: float | None, currency: str = "EUR") -> str:
    if value is None:
        return "-"
    symbol = {"EUR": "€", "USD": "$", "GBP": "£"}.get(currency.upper(), currency + " ")
    return f"{symbol}{value:,.2f}"


def local_time(value: datetime | None, tz_name: str) -> str:
    if value is None:
        return "-"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # unknown tz name in settings
        tz = ZoneInfo("UTC")
    return value.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).strftime("%a %d %b %Y %H:%M")


def safe(text: str | None) -> str:
    """Neutralise model/web supplied text so it can never inject HTML tags or markdown links.

    Only `<`/`>` are escaped (python-markdown escapes a bare `&` itself), so the .md file stays readable.
    """
    cleaned = _LINK_TEXT_BAD.sub(" ", text or "").strip()
    return cleaned.replace("<", "&lt;").replace(">", "&gt;")


def safe_url(url: str | None) -> str | None:
    url = (url or "").strip()
    if url.lower().startswith(("http://", "https://")) and " " not in url and ")" not in url:
        return url
    return None


def link(text: str, url: str | None) -> str:
    clean_url = safe_url(url)
    return f"[{safe(text)}]({clean_url})" if clean_url else safe(text)


def render_html(markdown_text: str) -> str:
    return md.markdown(markdown_text, extensions=["tables", "sane_lists"])


def _offer_line(obs: Observation, item: Item, currency: str) -> str:
    parts: list[str] = [f"**{money(obs.price, currency)}** at {safe(obs.retailer_domain)}"]
    if obs.unit_price is not None:
        qty = f"{obs.pack_quantity:g} {obs.unit or ''}".strip()
        parts.append(f"{money(obs.unit_price, currency)}/{obs.unit or 'unit'} ({qty})")
    if obs.list_price:
        parts.append(f"regular {money(obs.list_price, currency)}")
    if obs.promo_type and obs.promo_type != "none":
        promo = obs.promo_type + (f": {safe(obs.promo_text)}" if obs.promo_text else "")
        parts.append(promo)
    if obs.valid_until:
        parts.append(f"until {obs.valid_until.isoformat()}")
    parts.append(f"confidence {int(round(obs.confidence * 100))}%")
    return " · ".join(parts)


def _baseline_line(obs: Observation, currency: str) -> str:
    if obs.baseline_price is None:
        return "no price history yet (first observation - the baseline starts now)"
    basis = "per unit" if obs.baseline_basis == "unit" else "per pack"
    return f"baseline {money(obs.baseline_price, currency)} {basis}"


def build_report(session: Session, run: Run, plan: RunPlan | None, settings: Settings) -> str:
    currency = settings.currency
    lines: list[str] = []
    when = local_time(run.started_at, settings.timezone)
    title = "Dealwatch plan (dry run)" if run.dry_run else "Dealwatch report"
    lines.append(f"# {title} - run #{run.id} - {when}")
    lines.append("")

    if run.dry_run:
        lines.extend(_plan_section(plan, run, settings))
        return "\n".join(lines).rstrip() + "\n"

    lines.append(
        f"Checked **{run.items_checked}** item(s) in {run.groups_completed} batch(es) · "
        f"{run.web_searches} web searches · {run.api_requests} API calls · ~${run.cost_usd:.2f} · model {run.model}"
    )
    if run.groups_failed:
        lines.append(f"\n> ⚠️ {run.groups_failed} batch(es) failed - see Errors below. Their items will be retried next run.")
    lines.append("")

    observations = list(
        session.scalars(select(Observation).where(Observation.run_id == run.id).order_by(Observation.score.desc(), Observation.price))
    )
    run_items = list(session.scalars(select(RunItem).where(RunItem.run_id == run.id)))
    items = {i.id: i for i in session.scalars(select(Item))}

    deals_by_item: dict[int, list[Observation]] = {}
    others_by_item: dict[int, list[Observation]] = {}
    for obs in observations:
        bucket = deals_by_item if obs.is_deal else others_by_item
        bucket.setdefault(obs.item_id, []).append(obs)

    # ---- deals -------------------------------------------------------------------------------
    lines.append(f"## 🔥 Deals worth a look ({len(deals_by_item)})")
    lines.append("")
    if not deals_by_item:
        lines.append("_No offer cleared your deal threshold this run._")
        lines.append("")
    ranked = sorted(deals_by_item.items(), key=lambda kv: -max(o.score for o in kv[1]))
    for rank, (item_id, offers) in enumerate(ranked, start=1):
        item = items.get(item_id)
        if item is None:
            continue
        best = offers[0]
        lines.append(f"### {rank}. {safe(item.display_name)} - {money(best.price, currency)} at {safe(best.retailer_domain)}")
        lines.append(f"- {_offer_line(best, item, currency)}")
        lines.append(f"- Why: {safe(best.reasons) or 'meets your criteria'} · {_baseline_line(best, currency)}")
        lines.append(f"- {link(best.title, best.url)}")
        if best.evidence:
            lines.append(f"- _{safe(best.evidence)}_")
        for other in offers[1:3]:
            lines.append(f"- Also a deal: {_offer_line(other, item, currency)} - {link('link', other.url)}")
        cheaper_regular = [o for o in others_by_item.get(item_id, []) if o.comparable_price < best.comparable_price]
        if cheaper_regular:
            o = cheaper_regular[0]
            lines.append(f"- Note: a non-promo listing is cheaper per unit: {_offer_line(o, item, currency)} - {link('link', o.url)}")
        lines.append("")

    # ---- other findings --------------------------------------------------------------------
    found_no_deal = {k: v for k, v in others_by_item.items() if k not in deals_by_item}
    lines.append(f"## 📋 Checked, no deal ({len(found_no_deal)})")
    lines.append("")
    if found_no_deal:
        lines.append("| Item | Best price seen | Per unit | Shop | vs baseline |")
        lines.append("|---|---|---|---|---|")
        for item_id, offers in sorted(found_no_deal.items(), key=lambda kv: items.get(kv[0]).display_name if items.get(kv[0]) else ""):
            item = items.get(item_id)
            if item is None:
                continue
            best = min(offers, key=lambda o: o.comparable_price)
            vs = f"{best.discount_pct:+.0f}%" if best.discount_pct is not None else "n/a"
            per_unit = f"{money(best.unit_price, currency)}/{best.unit}" if best.unit_price is not None else "-"
            lines.append(
                f"| {safe(item.display_name)} | {money(best.price, currency)} | {per_unit} | {link(best.retailer_domain, best.url)} | {vs} |"
            )
    else:
        lines.append("_-_")
    lines.append("")

    # ---- not found / errors ------------------------------------------------------------------
    not_found = [ri for ri in run_items if ri.status == "not_found"]
    if not_found:
        lines.append(f"## ❓ Not found ({len(not_found)})")
        lines.append("")
        for ri in not_found:
            item = items.get(ri.item_id)
            note = f" - {safe(ri.note)}" if ri.note else ""
            lines.append(f"- {safe(item.display_name if item else str(ri.item_id))}{note}")
        lines.append("")
    errored = [ri for ri in run_items if ri.status == "error"]
    if errored:
        lines.append(f"## ⚠️ Errors ({len(errored)} item(s))")
        lines.append("")
        by_note: dict[str, list[str]] = {}
        for ri in errored:
            item = items.get(ri.item_id)
            by_note.setdefault(ri.note or "unknown error", []).append(item.display_name if item else str(ri.item_id))
        for note, names in by_note.items():
            lines.append(f"- {safe(note)}: {', '.join(safe(n) for n in names)}")
        lines.append("")

    # ---- plan leftovers ----------------------------------------------------------------------
    if plan is not None:
        if plan.deferred:
            lines.append(f"## ⏭ Deferred to next run ({len(plan.deferred)})")
            lines.append("")
            lines.append(", ".join(safe(i.display_name) for i in plan.deferred))
            lines.append("")
        if plan.not_due:
            lines.append(f"<details><summary>Not due this run ({len(plan.not_due)})</summary>\n\n{', '.join(safe(n) for n in plan.not_due)}\n\n</details>")
            lines.append("")
        for warning in plan.warnings:
            lines.append(f"> {safe(warning)}")
        if plan.warnings:
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _plan_section(plan: RunPlan | None, run: Run, settings: Settings) -> list[str]:
    lines: list[str] = []
    if plan is None or not plan.groups:
        lines.append("_Nothing to do._")
        if plan is not None:
            for warning in plan.warnings:
                lines.append(f"> {safe(warning)}")
        return lines
    est = plan.estimate
    lines.append(
        f"Would check **{plan.items_planned}** item(s) in **{len(plan.groups)}** batch(es) with model `{settings.search_model}` "
        f"(effort {settings.search_effort}, mode {settings.search_mode})."
    )
    lines.append(
        f"Estimate: ~{est.web_searches} web searches, ~{est.input_tokens:,} input / ~{est.output_tokens:,} output tokens, "
        f"**~${est.usd:.2f}** (rough - real cost is recorded after the run)."
    )
    lines.append("")
    for group in plan.groups:
        shops = ", ".join(r["domain"] for r in group.retailers) or "(open web)"
        lines.append(f"### Batch {group.index}: {group.category} - {len(group.items)} item(s), max {group.max_uses} searches")
        lines.append(f"- Shops: {safe(shops)}")
        for item in group.items:
            baseline = f" (baseline {item.baseline:.2f}/{item.baseline_basis})" if item.baseline else ""
            lines.append(f"- {safe(item.display_name)} [{item.priority}]{baseline}")
        lines.append("")
    if plan.deferred:
        lines.append(f"Deferred by batch cap: {', '.join(safe(i.display_name) for i in plan.deferred)}")
        lines.append("")
    if plan.not_due:
        lines.append(f"Not due: {', '.join(safe(n) for n in plan.not_due)}")
        lines.append("")
    for warning in plan.warnings:
        lines.append(f"> {safe(warning)}")
    return lines
