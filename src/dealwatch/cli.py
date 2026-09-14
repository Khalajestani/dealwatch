"""Command line entry points: `dealwatch serve|run|plan|seed|report|items|retailers|settings`."""

from __future__ import annotations

import logging
import sys
from datetime import timedelta

import typer
from sqlalchemy import select

from .config import Env, load_env
from .db import init_db, make_engine, make_session_factory
from .domains import is_valid_domain, normalize_domain
from .models import CATEGORY_KEYS, PRIORITY_KEYS, RETAILER_STATUSES, Item, Observation, PricePoint, Report, Retailer, Run, RunItem, utcnow
from .seed import seed_retailers, seed_sample_items
from .settings import SPECS, get_settings, save_settings

app = typer.Typer(help="Dealwatch - watch your shopping list for real deals.", no_args_is_help=True, add_completion=False)
items_app = typer.Typer(help="Manage shopping-list items.", no_args_is_help=True)
retailers_app = typer.Typer(help="Manage retailers (enable / disable / block).", no_args_is_help=True)
settings_app = typer.Typer(help="Show or change settings.", no_args_is_help=True)
app.add_typer(items_app, name="items")
app.add_typer(retailers_app, name="retailers")
app.add_typer(settings_app, name="settings")


def _bootstrap() -> tuple[Env, object]:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    env = load_env()
    engine = make_engine(env.db_path)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        seed_retailers(session)
    return env, session_factory


def _search_client(env: Env, fake: bool):
    from .services.llm import AnthropicSearchClient, FakeSearchClient

    if fake or env.fake_llm:
        typer.echo("Using the FAKE search client - no credits spent, prices are made up.", err=True)
        return FakeSearchClient()
    if not env.has_api_credentials:
        typer.echo("No Anthropic credentials found (ANTHROPIC_API_KEY). Use --fake to try without credits.", err=True)
        raise typer.Exit(2)
    return AnthropicSearchClient()


@app.command()
def serve(host: str | None = None, port: int | None = None, reload: bool = typer.Option(False, help="Auto-reload on code changes (dev)")):
    """Start the web UI (and the scheduler)."""
    import uvicorn

    env = load_env()
    host = host or env.host
    port = port or env.port
    if reload:
        uvicorn.run("dealwatch.web.app:app_factory", factory=True, host=host, port=port, reload=True)
    else:
        from .web.app import create_app

        uvicorn.run(create_app(env), host=host, port=port)


@app.command()
def run(
    force: bool = typer.Option(False, help="Check every active item, not only the ones that are due."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Only build the plan and cost estimate (no API calls)."),
    fake: bool = typer.Option(False, help="Use the fake search client (no credits)."),
):
    """Execute one run now (for cron/launchd use)."""
    from .services.runner import RunInProgress, execute_run

    from .services.llm import FakeSearchClient

    env, session_factory = _bootstrap()
    client = FakeSearchClient() if dry_run else _search_client(env, fake)
    try:
        run_id = execute_run(session_factory, client, env=env, trigger="cli", dry_run=dry_run, force=force)
    except RunInProgress:
        typer.echo("A run is already in progress.", err=True)
        raise typer.Exit(1)
    with session_factory() as session:
        run_row = session.get(Run, run_id)
        report = session.scalar(select(Report).where(Report.run_id == run_id))
        typer.echo(report.markdown if report else "(no report)")
        typer.echo(f"\nrun #{run_id}: {run_row.status} · {run_row.deal_count} deals · {run_row.web_searches} searches · ${run_row.cost_usd:.3f}", err=True)
        if run_row.status == "failed":
            raise typer.Exit(1)


@app.command()
def plan(force: bool = typer.Option(False, help="Plan as if every active item were due.")):
    """Show what the next run would do and what it would roughly cost (no API calls)."""
    _run_dry(force)


def _run_dry(force: bool) -> None:
    from .services.llm import FakeSearchClient
    from .services.runner import execute_run

    env, session_factory = _bootstrap()
    run_id = execute_run(session_factory, FakeSearchClient(), env=env, trigger="cli", dry_run=True, force=force)
    with session_factory() as session:
        report = session.scalar(select(Report).where(Report.run_id == run_id))
        typer.echo(report.markdown if report else "(no plan)")


@app.command()
def seed(samples: bool = typer.Option(False, help="Also add a few sample items.")):
    """Create the database, default retailers and (optionally) sample items."""
    _, session_factory = _bootstrap()
    with session_factory() as session:
        added = seed_sample_items(session) if samples else 0
    typer.echo(f"Database ready. Sample items added: {added}")


@app.command()
def report(run_id: int | None = typer.Argument(None, help="Run id (default: latest)")):
    """Print a run's report as markdown."""
    _, session_factory = _bootstrap()
    with session_factory() as session:
        if run_id is None:
            run_row = session.scalars(select(Run).where(Run.dry_run.is_(False)).order_by(Run.id.desc())).first()
            run_id = run_row.id if run_row else None
        rep = session.scalar(select(Report).where(Report.run_id == run_id)) if run_id else None
        if rep is None:
            typer.echo("No report found.", err=True)
            raise typer.Exit(1)
        typer.echo(rep.markdown)


@app.command()
def tracker():
    """Per-item price tracker summary (best price per check, change, trend, lowest ever, best shop)."""
    from .services.insights import all_item_insights
    from .services.report import money

    _, session_factory = _bootstrap()
    with session_factory() as session:
        insights = all_item_insights(session, utcnow())
    typer.echo(f"{'item':<38} {'checks':>6} {'last best':>14} {'prev%':>7} {'30d avg':>9} {'lowest':>9} {'trend':<8} cheapest most often")
    for i in insights:
        if not i.n_points:
            typer.echo(f"{i.item.display_name[:38]:<38} {0:>6}  (no price points yet)")
            continue
        prev = f"{i.change_vs_prev_pct:+.0f}%" if i.change_vs_prev_pct is not None else "-"
        avg30 = money(i.avg_30d) if i.avg_30d is not None else "-"
        last = f"{money(i.last.best)}{i.unit_label}"
        typer.echo(f"{i.item.display_name[:38]:<38} {i.n_points:>6} {last:>14} {prev:>7} {avg30:>9} {money(i.low):>9} {i.trend:<8} {i.best_domain or '-'}")


@app.command()
def export(table: str = typer.Argument(..., help="items | retailers | runs | observations | price_points | run_items"), out: str | None = typer.Option(None, help="Output CSV path (default: stdout)")):
    """Export a table as CSV for your own analysis (or query data/dealwatch.db directly with DuckDB/pandas)."""
    import csv

    models = {"items": Item, "retailers": Retailer, "runs": Run, "observations": Observation, "price_points": PricePoint, "run_items": RunItem}
    model = models.get(table)
    if model is None:
        typer.echo(f"Unknown table. Choose one of: {', '.join(models)}", err=True)
        raise typer.Exit(2)
    _, session_factory = _bootstrap()
    skip = {"plan_json", "log_json"} if table == "runs" else set()
    columns = [c.name for c in model.__table__.columns if c.name not in skip]
    handle = open(out, "w", newline="", encoding="utf-8") if out else sys.stdout
    writer = csv.writer(handle)
    writer.writerow(columns)
    with session_factory() as session:
        count = 0
        for row in session.scalars(select(model).order_by(model.id)):
            writer.writerow([getattr(row, c) for c in columns])
            count += 1
    if out:
        handle.close()
        typer.echo(f"Wrote {count} rows to {out}", err=True)


# ---- items ------------------------------------------------------------------------------------

@items_app.command("list")
def items_list():
    _, session_factory = _bootstrap()
    with session_factory() as session:
        for item in session.scalars(select(Item).order_by(Item.category, Item.name)):
            flag = "" if item.active else " (paused)"
            checked = item.last_checked_at.strftime("%Y-%m-%d") if item.last_checked_at else "never"
            typer.echo(f"{item.id:>4}  {item.display_name:<40} {item.category:<14} {item.priority:<9} checked {checked}{flag}")


@items_app.command("add")
def items_add(
    name: str,
    brand: str | None = None,
    category: str = typer.Option("household", help=", ".join(CATEGORY_KEYS)),
    hints: str | None = typer.Option(None, help="Search hints: pack size, variant, model"),
    unit: str | None = typer.Option(None, help="Unit for per-unit comparison (roll, tablet, ml, ...)"),
    priority: str = typer.Option("stock_up", help=", ".join(PRIORITY_KEYS)),
    target_price: float | None = None,
    target_unit_price: float | None = None,
    no_bulk: bool = typer.Option(False, help="Do not consider multi-packs"),
):
    if category not in CATEGORY_KEYS or priority not in PRIORITY_KEYS:
        typer.echo("Unknown category or priority.", err=True)
        raise typer.Exit(2)
    _, session_factory = _bootstrap()
    with session_factory() as session:
        item = Item(name=name, brand=brand, category=category, search_hints=hints, unit=unit, priority=priority, target_price=target_price, target_unit_price=target_unit_price, bulk_ok=not no_bulk)
        session.add(item)
        session.commit()
        typer.echo(f"Added item #{item.id}: {item.display_name}")


@items_app.command("remove")
def items_remove(item_id: int):
    _, session_factory = _bootstrap()
    with session_factory() as session:
        item = session.get(Item, item_id)
        if item is None:
            raise typer.Exit(1)
        session.delete(item)
        session.commit()
        typer.echo(f"Removed {item.display_name}")


@items_app.command("bought")
def items_bought(item_id: int):
    """Mark an item as bought: it is skipped for the configured snooze period."""
    _, session_factory = _bootstrap()
    with session_factory() as session:
        item = session.get(Item, item_id)
        if item is None:
            raise typer.Exit(1)
        settings = get_settings(session)
        item.last_bought_at = utcnow()
        item.snooze_until = utcnow() + timedelta(days=settings.bought_snooze_days)
        session.commit()
        typer.echo(f"{item.display_name}: snoozed for {settings.bought_snooze_days} days")


# ---- retailers --------------------------------------------------------------------------------

@retailers_app.command("list")
def retailers_list():
    _, session_factory = _bootstrap()
    with session_factory() as session:
        for r in session.scalars(select(Retailer).order_by(Retailer.sort_order, Retailer.name)):
            typer.echo(f"{r.id:>4}  {r.status:<9} {r.domain:<22} {r.name:<22} {r.categories or 'all'}")


@retailers_app.command("add")
def retailers_add(domain: str, name: str | None = None, status: str = "enabled", categories: str | None = None):
    domain = normalize_domain(domain)
    if not is_valid_domain(domain) or status not in RETAILER_STATUSES:
        typer.echo("Invalid domain or status.", err=True)
        raise typer.Exit(2)
    _, session_factory = _bootstrap()
    with session_factory() as session:
        session.add(Retailer(name=name or domain, domain=domain, status=status, categories=categories))
        session.commit()
    typer.echo(f"Added {domain} ({status})")


@retailers_app.command("status")
def retailers_status(domain: str, status: str = typer.Argument(..., help="enabled | disabled | blocked")):
    if status not in RETAILER_STATUSES:
        raise typer.Exit(2)
    _, session_factory = _bootstrap()
    with session_factory() as session:
        r = session.scalar(select(Retailer).where(Retailer.domain == normalize_domain(domain)))
        if r is None:
            typer.echo("Unknown retailer.", err=True)
            raise typer.Exit(1)
        r.status = status
        session.commit()
        typer.echo(f"{r.domain} is now {status}")


# ---- settings ---------------------------------------------------------------------------------

@settings_app.command("show")
def settings_show():
    _, session_factory = _bootstrap()
    with session_factory() as session:
        settings = get_settings(session)
    for spec in SPECS:
        typer.echo(f"{spec.key:<24} = {getattr(settings, spec.key)}")


@settings_app.command("set")
def settings_set(key: str, value: str):
    _, session_factory = _bootstrap()
    with session_factory() as session:
        errors = save_settings(session, {key: value})
    if errors:
        typer.echo("; ".join(errors), err=True)
        raise typer.Exit(2)
    typer.echo(f"{key} = {value}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
