"""FastAPI web UI: server-rendered Jinja templates and plain HTML forms (no JS build step).

`create_app()` wires DB, scheduler and the search client. Runs are started on a background thread so
the UI stays responsive; the Runs page shows progress via the Run row that is written immediately.
"""

from __future__ import annotations

import json
import logging
import threading
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ..config import MODEL_CHOICES, Env, load_env
from ..db import init_db, make_engine, make_session_factory
from ..domains import is_valid_domain, normalize_domain
from ..models import CATEGORIES, CATEGORY_KEYS, PRIORITIES, PRIORITY_KEYS, RETAILER_STATUSES, UNITS, Item, Observation, Report, Retailer, Run, RunItem, utcnow
from ..seed import seed_retailers, seed_sample_items
from ..services.llm import AnthropicSearchClient, FakeSearchClient, SearchClient
from ..services.planner import is_due
from ..services.report import local_time, money
from ..services.runner import RunInProgress, execute_run, is_run_active
from ..services.scheduler import RunScheduler
from ..settings import SPECS, get_settings, save_settings

log = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent


def _float_or_none(raw: str | None) -> float | None:
    raw = (raw or "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"'{raw}' is not a number") from exc


def _int_or_none(raw: str | None) -> int | None:
    value = _float_or_none(raw)
    return int(value) if value is not None else None


def create_app(
    env: Env | None = None,
    *,
    session_factory: sessionmaker[Session] | None = None,
    search_client: SearchClient | None = None,
    start_scheduler: bool | None = None,
) -> FastAPI:
    env = env or load_env()
    if session_factory is None:
        engine = make_engine(env.db_path)
        init_db(engine)
        session_factory = make_session_factory(engine)
    with session_factory() as session:
        seed_retailers(session)

    def client_factory() -> SearchClient:
        if search_client is not None:
            return search_client
        return FakeSearchClient() if env.fake_llm else AnthropicSearchClient()

    scheduler = RunScheduler(session_factory, client_factory, env)
    use_scheduler = env.scheduler_enabled if start_scheduler is None else start_scheduler

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if use_scheduler:
            scheduler.start()
        yield
        scheduler.shutdown()

    app = FastAPI(title="Dealwatch", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.env = env
    app.state.session_factory = session_factory
    app.state.scheduler = scheduler
    app.state.client_factory = client_factory
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    templates = Jinja2Templates(directory=BASE_DIR / "templates")
    templates.env.filters["money"] = money
    templates.env.filters["localtime"] = local_time
    templates.env.filters["pct"] = lambda v: "-" if v is None else f"{v:+.0f}%"
    templates.env.globals.update(categories=CATEGORIES, priorities=PRIORITIES, units=UNITS, statuses=RETAILER_STATUSES, model_choices=MODEL_CHOICES)

    api_ready = env.has_api_credentials or env.fake_llm or search_client is not None

    def render(request: Request, template: str, **context):
        with session_factory() as session:
            settings = get_settings(session)
        context.update(
            request=request,
            settings=settings,
            tz=settings.timezone,
            run_active=is_run_active(),
            msg=request.query_params.get("msg"),
            err=request.query_params.get("err"),
            api_ready=api_ready,
            fake=env.fake_llm and search_client is None,
        )
        return templates.TemplateResponse(request, template, context)

    def redirect(url: str, *, msg: str | None = None, err: str | None = None) -> RedirectResponse:
        if msg:
            url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
        if err:
            url += ("&" if "?" in url else "?") + "err=" + quote(err)
        return RedirectResponse(url, status_code=303)

    # ---- dashboard ---------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        with session_factory() as session:
            settings = get_settings(session)
            now = utcnow()
            items = list(session.scalars(select(Item)))
            active = [i for i in items if i.active]
            due = [i for i in active if is_due(i, now, settings)]
            enabled_retailers = session.scalar(select(func.count()).select_from(Retailer).where(Retailer.status == "enabled")) or 0
            last_run = session.scalars(select(Run).where(Run.dry_run.is_(False)).order_by(Run.id.desc())).first()
            last_complete = session.scalars(
                select(Run).where(Run.dry_run.is_(False), Run.status.in_(["success", "partial"])).order_by(Run.id.desc())
            ).first()
            deals: list[tuple[Observation, Item]] = []
            if last_complete:
                rows = session.execute(
                    select(Observation, Item)
                    .join(Item, Item.id == Observation.item_id)
                    .where(Observation.run_id == last_complete.id, Observation.is_deal.is_(True))
                    .order_by(Observation.score.desc())
                ).all()
                seen: set[int] = set()
                for obs, item in rows:
                    if item.id in seen:
                        continue
                    seen.add(item.id)
                    deals.append((obs, item))
            since = now - timedelta(days=30)
            spent_30d = session.scalar(select(func.coalesce(func.sum(Run.cost_usd), 0.0)).where(Run.started_at >= since)) or 0.0
            runs_30d = session.scalar(select(func.count()).select_from(Run).where(Run.started_at >= since, Run.dry_run.is_(False))) or 0
        return render(
            request, "dashboard.html", nav="dashboard", active_count=len(active), due_count=len(due), item_count=len(items),
            enabled_retailers=enabled_retailers, last_run=last_run, last_complete=last_complete, deals=deals,
            spent_30d=spent_30d, runs_30d=runs_30d, next_run=scheduler.next_run_time(),
        )

    # ---- items ------------------------------------------------------------------------------

    def _latest_best_offers(session: Session, item_ids: list[int]) -> dict[int, Observation]:
        """Best (lowest comparable) offer from each item's most recent run that found it."""
        best: dict[int, Observation] = {}
        if not item_ids:
            return best
        latest_run = (
            select(Observation.item_id, func.max(Observation.run_id).label("run_id"))
            .where(Observation.item_id.in_(item_ids))
            .group_by(Observation.item_id)
            .subquery()
        )
        rows = session.scalars(
            select(Observation).join(latest_run, (Observation.item_id == latest_run.c.item_id) & (Observation.run_id == latest_run.c.run_id))
        )
        for obs in rows:
            current = best.get(obs.item_id)
            if current is None or obs.comparable_price < current.comparable_price:
                best[obs.item_id] = obs
        return best

    @app.get("/items", response_class=HTMLResponse)
    def items_page(request: Request):
        with session_factory() as session:
            settings = get_settings(session)
            now = utcnow()
            items = list(session.scalars(select(Item).order_by(Item.active.desc(), Item.category, Item.name)))
            best = _latest_best_offers(session, [i.id for i in items])
            due_ids = {i.id for i in items if is_due(i, now, settings)}
        return render(request, "items.html", nav="items", items=items, best=best, due_ids=due_ids)

    def _apply_item_form(item: Item, form: dict) -> None:
        name = (form.get("name") or "").strip()
        if not name:
            raise ValueError("Name is required")
        category = form.get("category") or "household"
        if category not in CATEGORY_KEYS:
            raise ValueError("Unknown category")
        priority = form.get("priority") or "stock_up"
        if priority not in PRIORITY_KEYS:
            raise ValueError("Unknown priority")
        item.name = name
        item.brand = (form.get("brand") or "").strip() or None
        item.category = category
        item.search_hints = (form.get("search_hints") or "").strip() or None
        unit = (form.get("unit") or "").strip() or None
        item.unit = unit if unit in UNITS else None
        item.bulk_ok = bool(form.get("bulk_ok"))
        item.priority = priority
        item.target_price = _float_or_none(form.get("target_price"))
        item.target_unit_price = _float_or_none(form.get("target_unit_price"))
        item.check_interval_days = _int_or_none(form.get("check_interval_days"))
        item.notes = (form.get("notes") or "").strip() or None

    @app.post("/items")
    async def add_item(request: Request):
        form = dict(await request.form())
        item = Item()
        try:
            _apply_item_form(item, form)
        except ValueError as exc:
            return redirect("/items", err=str(exc))
        with session_factory() as session:
            session.add(item)
            session.commit()
        return redirect("/items", msg=f"Added '{item.display_name}'. It will be checked in the next run.")

    @app.get("/items/{item_id}", response_class=HTMLResponse)
    def item_detail(request: Request, item_id: int):
        with session_factory() as session:
            item = session.get(Item, item_id)
            if item is None:
                raise HTTPException(404)
            history = list(
                session.scalars(select(Observation).where(Observation.item_id == item_id).order_by(Observation.observed_at.desc(), Observation.price).limit(60))
            )
            checks = list(session.scalars(select(RunItem).where(RunItem.item_id == item_id).order_by(RunItem.id.desc()).limit(10)))
            settings = get_settings(session)
            due = is_due(item, utcnow(), settings)
        return render(request, "item_detail.html", nav="items", item=item, history=history, checks=checks, due=due)

    @app.post("/items/{item_id}")
    async def update_item(request: Request, item_id: int):
        form = dict(await request.form())
        with session_factory() as session:
            item = session.get(Item, item_id)
            if item is None:
                raise HTTPException(404)
            try:
                _apply_item_form(item, form)
            except ValueError as exc:
                return redirect(f"/items/{item_id}", err=str(exc))
            session.commit()
        return redirect(f"/items/{item_id}", msg="Saved.")

    @app.post("/items/{item_id}/action")
    def item_action(item_id: int, action: str = Form(...)):
        with session_factory() as session:
            item = session.get(Item, item_id)
            if item is None:
                raise HTTPException(404)
            settings = get_settings(session)
            msg = "Done."
            if action == "delete":
                session.delete(item)
                session.commit()
                return redirect("/items", msg=f"Deleted '{item.display_name}'.")
            if action == "toggle":
                item.active = not item.active
                msg = "Item re-activated." if item.active else "Item paused (no more checks)."
            elif action == "bought":
                item.last_bought_at = utcnow()
                item.snooze_until = utcnow() + timedelta(days=settings.bought_snooze_days)
                msg = f"Marked as bought - skipped for {settings.bought_snooze_days} days."
            elif action == "unsnooze":
                item.snooze_until = None
                msg = "Snooze removed."
            elif action == "check_next":
                item.last_checked_at = None
                item.snooze_until = None
                msg = "Will be checked in the next run."
            else:
                raise HTTPException(400, "unknown action")
            session.commit()
        return redirect(f"/items/{item_id}", msg=msg)

    # ---- retailers --------------------------------------------------------------------------

    @app.get("/retailers", response_class=HTMLResponse)
    def retailers_page(request: Request):
        with session_factory() as session:
            retailers = list(session.scalars(select(Retailer).order_by(Retailer.sort_order, Retailer.name)))
        return render(request, "retailers.html", nav="retailers", retailers=retailers)

    @app.post("/retailers")
    async def add_retailer(request: Request):
        form = dict(await request.form())
        name = (form.get("name") or "").strip()
        domain = normalize_domain(form.get("domain") or "")
        if not is_valid_domain(domain):
            return redirect("/retailers", err="Enter a bare domain such as bol.com (no https://, no wildcards).")
        status = form.get("status") or "enabled"
        if status not in RETAILER_STATUSES:
            status = "enabled"
        categories = ",".join(c for c in (form.get("categories") or "").replace(" ", "").split(",") if c in CATEGORY_KEYS)
        with session_factory() as session:
            if session.scalar(select(Retailer).where(Retailer.domain == domain)):
                return redirect("/retailers", err=f"{domain} is already in the list.")
            session.add(Retailer(name=name or domain, domain=domain, status=status, categories=categories or None, notes=(form.get("notes") or "").strip() or None))
            session.commit()
        return redirect("/retailers", msg=f"Added {domain} ({status}).")

    @app.post("/retailers/{retailer_id}")
    async def update_retailer(request: Request, retailer_id: int):
        form = dict(await request.form())
        action = form.get("action") or "save"
        with session_factory() as session:
            retailer = session.get(Retailer, retailer_id)
            if retailer is None:
                raise HTTPException(404)
            if action == "delete":
                session.delete(retailer)
                session.commit()
                return redirect("/retailers", msg=f"Removed {retailer.domain}.")
            if action in RETAILER_STATUSES:
                retailer.status = action
                session.commit()
                return redirect("/retailers", msg=f"{retailer.domain} is now {action}.")
            retailer.name = (form.get("name") or "").strip() or retailer.domain
            retailer.categories = ",".join(c for c in (form.get("categories") or "").replace(" ", "").split(",") if c in CATEGORY_KEYS) or None
            retailer.notes = (form.get("notes") or "").strip() or None
            retailer.sort_order = _int_or_none(form.get("sort_order")) or retailer.sort_order
            session.commit()
        return redirect("/retailers", msg=f"Saved {retailer.domain}.")

    # ---- runs -------------------------------------------------------------------------------

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request):
        with session_factory() as session:
            runs = list(session.scalars(select(Run).order_by(Run.id.desc()).limit(100)))
        return render(request, "runs.html", nav="runs", runs=runs)

    @app.post("/runs/start")
    def start_run(force: str | None = Form(None)):
        if not api_ready:
            return redirect("/runs", err="No Anthropic credentials found. Put ANTHROPIC_API_KEY in .env (or set DEALWATCH_FAKE_LLM=1 to try without credits).")
        if is_run_active():
            return redirect("/runs", err="A run is already in progress.")

        def worker() -> None:
            try:
                execute_run(session_factory, client_factory(), env=env, trigger="manual", force=bool(force))
            except RunInProgress:
                log.warning("run skipped: already active")
            except Exception:
                log.exception("manual run failed")

        threading.Thread(target=worker, name="dealwatch-run", daemon=True).start()
        return redirect("/runs", msg="Run started in the background - refresh this page in a minute.")

    @app.post("/runs/plan")
    def plan_run(force: str | None = Form(None)):
        try:
            run_id = execute_run(session_factory, client_factory(), env=env, trigger="manual", dry_run=True, force=bool(force))
        except RunInProgress:
            return redirect("/runs", err="A run is already in progress.")
        return redirect(f"/runs/{run_id}", msg="Dry run: this is what the next run would do (no credits spent).")

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: int):
        with session_factory() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise HTTPException(404)
            report = session.scalar(select(Report).where(Report.run_id == run_id))
            observations = list(
                session.execute(
                    select(Observation, Item).join(Item, Item.id == Observation.item_id).where(Observation.run_id == run_id).order_by(Observation.is_deal.desc(), Observation.score.desc())
                ).all()
            )
            log_data = json.loads(run.log_json) if run.log_json else {}
            plan_data = json.loads(run.plan_json) if run.plan_json else {}
        return render(request, "run_detail.html", nav="runs", run=run, report=report, observations=observations, log=log_data, plan=plan_data, plan_pretty=json.dumps(plan_data, indent=1, ensure_ascii=False))

    @app.get("/runs/{run_id}/report.md", response_class=PlainTextResponse)
    def run_report_markdown(run_id: int):
        with session_factory() as session:
            report = session.scalar(select(Report).where(Report.run_id == run_id))
            if report is None:
                raise HTTPException(404)
            return report.markdown

    # ---- settings ---------------------------------------------------------------------------

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        groups: dict[str, list] = {}
        for spec in SPECS:
            groups.setdefault(spec.group, []).append(spec)
        return render(request, "settings.html", nav="settings", groups=groups, env=env)

    @app.post("/settings")
    async def save_settings_route(request: Request):
        form = dict(await request.form())
        values: dict[str, object] = {}
        for spec in SPECS:
            if spec.kind == "bool":
                values[spec.key] = spec.key in form
            elif spec.key in form:
                values[spec.key] = form[spec.key]
        custom_model = (form.get("search_model_custom") or "").strip()
        if custom_model:
            values["search_model"] = custom_model
        with session_factory() as session:
            errors = save_settings(session, values)
        if errors:
            return redirect("/settings", err=" · ".join(errors))
        try:
            scheduler.reload()
        except Exception as exc:  # scheduler not started (tests / disabled)
            log.debug("scheduler reload skipped: %s", exc)
        return redirect("/settings", msg="Settings saved.")

    @app.post("/seed-samples")
    def seed_samples():
        with session_factory() as session:
            added = seed_sample_items(session)
        return redirect("/items", msg=f"Added {added} sample item(s).")

    # ---- small JSON API (handy for scripts/agents) --------------------------------------------

    @app.get("/api/health")
    def api_health():
        with session_factory() as session:
            item_count = session.scalar(select(func.count()).select_from(Item)) or 0
        return {"ok": True, "items": item_count, "run_active": is_run_active(), "api_ready": api_ready, "fake_llm": env.fake_llm}

    @app.get("/api/runs/latest")
    def api_latest_run():
        with session_factory() as session:
            run = session.scalars(select(Run).where(Run.dry_run.is_(False)).order_by(Run.id.desc())).first()
            if run is None:
                return JSONResponse({"run": None})
            deals = session.execute(
                select(Observation, Item).join(Item, Item.id == Observation.item_id).where(Observation.run_id == run.id, Observation.is_deal.is_(True)).order_by(Observation.score.desc())
            ).all()
            return {
                "run": {"id": run.id, "status": run.status, "started_at": run.started_at.isoformat(), "deal_count": run.deal_count, "cost_usd": run.cost_usd},
                "deals": [
                    {"item": item.display_name, "price": obs.price, "unit_price": obs.unit_price, "unit": obs.unit, "retailer": obs.retailer_domain, "url": obs.url, "discount_pct": obs.discount_pct, "reasons": obs.reasons}
                    for obs, item in deals
                ],
            }

    return app


def app_factory() -> FastAPI:  # for `uvicorn dealwatch.web.app:app_factory --factory`
    return create_app()
