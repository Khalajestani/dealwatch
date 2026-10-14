"""Markdown report per run, rendered to HTML for the web UI and e-mail."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import markdown as md
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Item, Observation, Run, RunItem
from ..settings import Settings
from .insights import ItemInsight, item_insight
from .planner import RunPlan

_LINK_TEXT_BAD = re.compile(r"[\[\]\n\r]")


def money(value: float | None, currency: str = "EUR") -> str:
    if value is None:
        return "-"
    symbol = {"EUR": "€", "USD": "$", "GBP": "£"}.get(currency.upper(), currency + " ")
    if 0 < abs(value) < 0.1:  # per-ml / per-gram prices need more precision than cents
        return f"{symbol}{value:.4f}"
    return f"{symbol}{value:,.2f}"


def local_time(value: datetime | None, tz_name: str) -> str:
    if value is None:
        return "-"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # unknown tz name in settings
        tz = ZoneInfo("UTC")
    return value.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).strftime("%a %d/%m/%Y %H:%M")


def local_date(value: datetime | None, tz_name: str) -> str | None:
    """dd/mm/yyyy in the app's time zone, or None."""
    return local_time(value, tz_name).split(" ")[1] if value else None


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
    parts.append(f"confidence {round(obs.confidence * 100)}%")
    return " · ".join(parts)


def _baseline_line(obs: Observation, currency: str) -> str:
    if obs.baseline_price is None:
        return "no price history yet (first observation - the baseline starts now)"
    basis = "per unit" if obs.baseline_basis == "unit" else "per pack"
    return f"baseline {money(obs.baseline_price, currency)} {basis}"


def _history_line(insight: ItemInsight, run_id: int, currency: str) -> str | None:
    """'lowest seen ... · 30-day avg ... · N checks since ...' from checks *before* this run."""
    prior = [p for p in insight.points if p.run_id != run_id]
    if not prior:
        return None
    low = min(prior, key=lambda p: p.best)
    suffix = insight.unit_label
    parts = [f"lowest seen {money(low.best, currency)}{suffix} on {low.at:%d/%m/%Y} at {safe(low.domain)}"]
    recent = [p.best for p in prior if p.at >= insight.last.at - timedelta(days=30)] if insight.last else []
    if recent:
        parts.append(f"30-day avg {money(sum(recent) / len(recent), currency)}{suffix}")
    parts.append(f"{len(prior)} earlier check(s) since {prior[0].at:%d/%m/%Y}")
    return " · ".join(parts)


def _tracker_section(
    insights: dict[int, ItemInsight], items: dict[int, Item], run: Run, settings: Settings
) -> list[str]:
    lines: list[str] = []
    current = [i for i in insights.values() if i.last and i.last.run_id == run.id]
    if not current:
        return lines
    with_history = [i for i in current if i.prev is not None]
    lines.append("## 📈 Price tracker")
    lines.append("")
    if not with_history:
        lines.append(f"Price history starts with this run: {len(current)} item(s) now have their first price point.")
        lines.append("")
        return lines
    threshold = max(3.0, settings.min_discount_pct / 2)
    down = sorted(
        [i for i in with_history if i.change_vs_prev_pct is not None and i.change_vs_prev_pct <= -threshold],
        key=lambda i: i.change_vs_prev_pct,
    )[:6]
    up = sorted(
        [i for i in with_history if i.change_vs_prev_pct is not None and i.change_vs_prev_pct >= threshold],
        key=lambda i: -i.change_vs_prev_pct,
    )[:6]
    currency = settings.currency
    for label, movers in (("Cheaper than last check", down), ("More expensive than last check", up)):
        if movers:
            lines.append(f"**{label}:**")
            for i in movers:
                lines.append(
                    f"- {safe(i.item.display_name)}: {money(i.prev.best, currency)} → **{money(i.last.best, currency)}**{i.unit_label} "
                    f"({i.change_vs_prev_pct:+.0f}%) at {safe(i.last.domain)}"
                )
            lines.append("")
    lows = [i for i in with_history if i.is_new_low]
    if lows:
        lines.append(
            "**New all-time low:** "
            + ", ".join(f"{safe(i.item.display_name)} ({money(i.last.best, currency)}{i.unit_label})" for i in lows)
        )
        lines.append("")
    stable = len(with_history) - len(down) - len(up)
    lines.append(
        f"_{len(with_history)} item(s) compared with their previous check; {stable} unchanged within ±{threshold:.0f}%. See the Tracker page for charts and per-shop stats._"
    )
    lines.append("")
    return lines


def build_report(session: Session, run: Run, plan: RunPlan | None, settings: Settings) -> str:
    currency = settings.currency
    lines: list[str] = []
    lines.append(f"# Dealwatch report - check #{run.id} - {local_time(run.started_at, settings.timezone)}")
    lines.append("")
    lines.append(
        f"Checked **{run.items_checked}** item(s) in {run.groups_completed} batch(es) · "
        f"{run.web_lookups} web lookups · model {run.model}"
    )
    if run.groups_failed:
        lines.append(
            f"\n> ⚠️ {run.groups_failed} batch(es) failed - see Errors below. Their items will be retried next run."
        )
    lines.append("")

    observations = list(
        session.scalars(
            select(Observation)
            .where(Observation.run_id == run.id)
            .order_by(Observation.score.desc(), Observation.price)
        )
    )
    run_items = list(session.scalars(select(RunItem).where(RunItem.run_id == run.id)))
    items = {i.id: i for i in session.scalars(select(Item))}

    deals_by_item: dict[int, list[Observation]] = {}
    others_by_item: dict[int, list[Observation]] = {}
    for obs in observations:
        bucket = deals_by_item if obs.is_deal else others_by_item
        bucket.setdefault(obs.item_id, []).append(obs)
    insights = {
        item_id: item_insight(session, items[item_id], now=run.started_at)
        for item_id in {o.item_id for o in observations}
        if item_id in items
    }

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
        lines.append(
            f"### {rank}. {safe(item.display_name)} - {money(best.price, currency)} at {safe(best.retailer_domain)}"
        )
        lines.append(f"- {_offer_line(best, item, currency)}")
        lines.append(f"- Why: {safe(best.reasons) or 'meets your criteria'} · {_baseline_line(best, currency)}")
        history = _history_line(insights[item_id], run.id, currency) if item_id in insights else None
        if history:
            lines.append(f"- History: {history}")
        lines.append(f"- {link(best.title, best.url)}")
        if best.evidence:
            lines.append(f"- _{safe(best.evidence)}_")
        for other in offers[1:3]:
            lines.append(f"- Also a deal: {_offer_line(other, item, currency)} - {link('link', other.url)}")
        cheaper_regular = [o for o in others_by_item.get(item_id, []) if o.comparable_price < best.comparable_price]
        if cheaper_regular:
            o = cheaper_regular[0]
            lines.append(
                f"- Note: a non-promo listing is cheaper per unit: {_offer_line(o, item, currency)} - {link('link', o.url)}"
            )
        lines.append("")

    lines.extend(_tracker_section(insights, items, run, settings))

    # ---- other findings --------------------------------------------------------------------
    found_no_deal = {k: v for k, v in others_by_item.items() if k not in deals_by_item}
    lines.append(f"## 📋 Checked, no deal ({len(found_no_deal)})")
    lines.append("")
    if found_no_deal:
        lines.append("| Item | Best price seen | Per unit | Shop | vs baseline | Lowest ever |")
        lines.append("|---|---|---|---|---|---|")
        for item_id, offers in sorted(
            found_no_deal.items(), key=lambda kv: items.get(kv[0]).display_name if items.get(kv[0]) else ""
        ):
            item = items.get(item_id)
            if item is None:
                continue
            best = min(offers, key=lambda o: o.comparable_price)
            vs = f"{best.discount_pct:+.0f}%" if best.discount_pct is not None else "n/a"
            per_unit = f"{money(best.unit_price, currency)}/{best.unit}" if best.unit_price is not None else "-"
            ins = insights.get(item_id)
            lowest = (
                f"{money(ins.low, currency)}{ins.unit_label} ({ins.low_at:%d/%m/%Y})"
                if ins and ins.low is not None and ins.n_points > 1
                else "-"
            )
            lines.append(
                f"| {safe(item.display_name)} | {money(best.price, currency)} | {per_unit} | {link(best.retailer_domain, best.url)} | {vs} | {lowest} |"
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
            lines.append(
                f"<details><summary>Not due this run ({len(plan.not_due)})</summary>\n\n{', '.join(safe(n) for n in plan.not_due)}\n\n</details>"
            )
            lines.append("")
        for warning in plan.warnings:
            lines.append(f"> {safe(warning)}")
        if plan.warnings:
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"
