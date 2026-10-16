"""FastAPI web UI: server-rendered Jinja templates and plain HTML forms (no JS build step).

`create_app()` wires the database, the login, Claude and the scheduler. Run with
`uvicorn dealwatch.web.app:create_app --factory`. Price checks run on a background thread so the UI stays
responsive; the Runs page shows progress through the Run row, which is written at once.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import threading
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker
from starlette.datastructures import UploadFile

from .. import __version__, security
from .. import items as item_store
from ..config import Env
from ..db import init_db, make_engine, make_session_factory
from ..domains import is_valid_domain, normalize_domain
from ..models import (
    CATEGORIES,
    CATEGORY_ICON,
    CATEGORY_KEYS,
    CATEGORY_SHORT,
    PRIORITIES,
    RETAILER_STATUSES,
    UNITS,
    Item,
    Observation,
    PricePoint,
    Report,
    Retailer,
    Run,
    RunItem,
    utcnow,
)
from ..queries import current_deals, last_complete_run, latest_best_offers
from ..seed import seed_retailers, seed_sample_items
from ..services import ask, extract
from ..services.claude import Claude, Unavailable
from ..services.insights import all_item_insights, item_insight, overview, price_drops, retailer_breakdown
from ..services.planner import due_at, is_due
from ..services.report import friendly_time, local_time, money
from ..services.runner import RunInProgress, execute_run, is_run_active
from ..services.scheduler import RunScheduler
from ..services.search import ClaudeSearchClient, SearchClient
from ..services.similar import find_similar, normalize
from ..settings import SPECS, get_settings, save_settings
from .charts import Series, line_chart, sparkline

log = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
SHOWN_TURNS = 40  # on the Ask page; the model gets ask.HISTORY_TURNS of them
NOT_CONNECTED = "Claude is not connected yet: see docs/assistant.md."
_EXPORTS = {
    "items": Item,
    "retailers": Retailer,
    "runs": Run,
    "observations": Observation,
    "price_points": PricePoint,
    "run_items": RunItem,
}
_EXPORT_SKIP = {"runs": {"plan_json", "log_json"}}


# The status chips on Items, in order ("" shows everything).
ITEM_STATUSES = [
    ("", "All"),
    ("deal", "Deals"),
    ("due", "Next check"),
    ("waiting", "Waiting"),
    ("untracked", "No price yet"),
    ("snoozed", "Snoozed"),
    ("paused", "Paused"),
]


def _item_tags(item: Item, offer: Observation | None, due: bool, now) -> set[str]:
    """The ITEM_STATUSES an item is in."""
    snoozed = bool(item.snooze_until and item.snooze_until > now)
    return {
        tag
        for tag, applies in (
            ("deal", bool(offer and offer.is_deal)),
            ("due", due),
            ("waiting", item.active and not snoozed and not due),
            ("untracked", offer is None),
            ("snoozed", snoozed),
            ("paused", not item.active),
        )
        if applies
    }


def create_app(
    env: Env | None = None,
    *,
    session_factory: sessionmaker[Session] | None = None,
    search_client: SearchClient | None = None,
    claude: Claude | None = None,
    start_scheduler: bool | None = None,
) -> FastAPI:
    env = env or Env()
    logging.basicConfig(level=env.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if session_factory is None:
        engine = make_engine(env.db_path)
        init_db(engine)
        session_factory = make_session_factory(engine)
    with session_factory() as session:
        seed_retailers(session)
        # A check cut off by a restart would otherwise show as running for ever.
        session.execute(
            update(Run)
            .where(Run.status == "running")
            .values(status="failed", error="interrupted by a restart", finished_at=utcnow())
        )
        session.commit()
        timezone = get_settings(session).timezone
    claude = claude or Claude(env.token, timezone)

    def client_factory() -> SearchClient | None:
        if search_client is not None:
            return search_client
        return ClaudeSearchClient(claude) if claude.enabled else None

    scheduler = RunScheduler(session_factory, client_factory, env)
    use_scheduler = env.scheduler if start_scheduler is None else start_scheduler

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if use_scheduler:
            scheduler.start()
        yield
        scheduler.shutdown()

    app = FastAPI(
        title="Dealwatch", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.env = env
    app.state.session_factory = session_factory
    app.state.scheduler = scheduler
    app.state.claude = claude
    security.install(app, env)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    uploads_dir = env.uploads_dir
    uploads_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/uploads", StaticFiles(directory=uploads_dir), name="uploads")

    def icon(name: str, size: str = "") -> Markup:
        return Markup(f'<svg class="ic {size}" aria-hidden="true"><use href="#i-{name}"/></svg>')

    templates = Jinja2Templates(directory=BASE_DIR / "templates")
    templates.env.filters["money"] = money
    templates.env.filters["localtime"] = local_time
    templates.env.filters["friendly"] = friendly_time
    templates.env.filters["pct"] = lambda v: "-" if v is None else f"{v:+.0f}%"
    templates.env.globals.update(
        categories=CATEGORIES,
        category_short=CATEGORY_SHORT,
        category_icon=CATEGORY_ICON,
        priorities=PRIORITIES,
        priority_short={"need": "need it", "stock_up": "stock up", "wishlist": "wishlist"},
        units=UNITS,
        statuses=RETAILER_STATUSES,
        icon=icon,
        ask_models=ask.MODELS,
    )

    def settings_now():
        with session_factory() as session:
            return get_settings(session)

    def render(request: Request, template: str, status_code: int = 200, **context) -> Response:
        settings = context.pop("settings", None) or settings_now()
        context.update(
            request=request,
            settings=settings,
            tz=settings.timezone,
            run_active=is_run_active(),
            now=context.pop("now", None) or utcnow(),
            msg=request.query_params.get("msg"),
            err=context.pop("err", None) or request.query_params.get("err"),
            claude_ready=claude.enabled or search_client is not None,
        )
        return templates.TemplateResponse(request, template, context, status_code=status_code)

    def redirect(url: str, *, msg: str | None = None, err: str | None = None) -> RedirectResponse:
        for key, value in (("msg", msg), ("err", err)):
            if value:
                url += ("&" if "?" in url else "?") + f"{key}=" + quote(value)
        return RedirectResponse(url, status_code=303)

    def save_upload(jpeg: bytes) -> str:
        name = f"{uuid.uuid4().hex}.jpg"
        (uploads_dir / name).write_bytes(jpeg)
        return name

    def upload_name(name: str | None) -> str | None:
        """A photo name from a form, only when it is one of ours."""
        name = (name or "").strip()
        return name if re.fullmatch(r"[0-9a-f]{32}\.jpg", name) and (uploads_dir / name).exists() else None

    def remove_upload(name: str | None) -> None:
        if name := upload_name(name):
            (uploads_dir / name).unlink(missing_ok=True)

    async def read_photo(form) -> bytes | None:
        """The form's photo as a shrunk JPEG, None when none was chosen. Raises ValueError for a bad file."""
        upload = form.get("photo")
        if not isinstance(upload, UploadFile) or not upload.filename:
            return None
        data = await upload.read(extract.MAX_UPLOAD_BYTES + 1)
        return await run_in_threadpool(extract.prepare_image, data) if data else None

    # ---- health, login, Home Screen app -------------------------------------------------------

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/"):
        if not env.auth_enabled:
            return redirect("/")
        return render(request, "login.html", next=security.safe_next(next), error=None)

    @app.post("/login")
    def login(request: Request, password: Annotated[str, Form()], next: Annotated[str, Form()] = "/"):
        target = security.safe_next(next)
        if not env.auth_enabled:
            return redirect(target)
        match security.login_ok(request, env, password):
            case True:
                request.session["auth"] = True
                return redirect(target)
            case False:
                return render(request, "login.html", 401, next=target, error="Wrong password.")
        return render(
            request, "login.html", 429, next=target, error="Too many wrong passwords. Try again in 10 minutes."
        )

    # The manifest and the service worker must sit at the root to cover every page.
    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")

    # iOS also asks for the Home Screen icon at these fixed addresses, whatever the page says.
    @app.get("/apple-touch-icon.png", include_in_schema=False)
    @app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
    def apple_touch_icon():
        return FileResponse(STATIC_DIR / "apple-touch-icon.png", media_type="image/png")

    # ---- dashboard ---------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        with session_factory() as session:
            settings = get_settings(session)
            now = utcnow()
            items = list(session.scalars(select(Item)))
            active = [i for i in items if i.active]
            best = latest_best_offers(session, [i.id for i in active])
            enabled_retailers = (
                session.scalar(select(func.count()).select_from(Retailer).where(Retailer.status == "enabled")) or 0
            )
            last_run = session.scalars(select(Run).order_by(Run.id.desc())).first()
            last_complete = last_complete_run(session)
            # The last check's own finds first.
            deals = sorted(
                current_deals(session, now.date()), key=lambda d: d[0].run_id != getattr(last_complete, "id", None)
            )
            deal_ids = {item.id for _, item in deals}
            drops = (
                [i for i in price_drops(all_item_insights(session, now), last_complete.id) if i.item.id not in deal_ids]
                if last_complete
                else []
            )
        # What is being checked: the next to be checked first.
        watching = sorted(
            (
                {"item": i, "offer": best.get(i.id), "due": is_due(i, now, settings), "due_at": due_at(i, settings)}
                for i in active
            ),
            key=lambda w: (not w["due"], w["due_at"] or now, w["item"].name.lower()),
        )
        return render(
            request,
            "dashboard.html",
            settings=settings,
            nav="dashboard",
            now=now,
            active_count=len(active),
            due_count=sum(w["due"] for w in watching),
            item_count=len(items),
            enabled_retailers=enabled_retailers,
            last_run=last_run,
            last_complete=last_complete,
            deals=deals,
            drops=drops,
            watching=watching,
            next_run=scheduler.next_run_time(),
        )

    # ---- items ------------------------------------------------------------------------------

    @app.get("/items", response_class=HTMLResponse)
    def items_page(request: Request, q: str = "", category: str = "", status: str = ""):
        with session_factory() as session:
            settings = get_settings(session)
            now = utcnow()
            all_items = list(session.scalars(select(Item).order_by(Item.active.desc(), Item.category, Item.name)))
            best = latest_best_offers(session, [i.id for i in all_items])
        due_ids = {i.id for i in all_items if is_due(i, now, settings)}
        needle = normalize(q)
        searched = [
            item
            for item in all_items
            if (not category or item.category == category)
            and (
                not needle
                or needle
                in normalize(" ".join(filter(None, [item.display_name, item.search_hints, item.notes, item.category])))
            )
        ]
        tags = {item.id: _item_tags(item, best.get(item.id), item.id in due_ids, now) for item in searched}
        counts = Counter(tag for item_tags in tags.values() for tag in item_tags)
        status_links = [
            (
                key,
                label,
                "/items?" + urlencode({k: v for k, v in (("q", q), ("category", category), ("status", key)) if v}),
                counts[key],
            )
            for key, label in ITEM_STATUSES
            if not key or counts[key] or key == status
        ]
        return render(
            request,
            "items.html",
            settings=settings,
            nav="items",
            now=now,
            items=[item for item in searched if not status or status in tags[item.id]],
            total=len(all_items),
            best=best,
            due_ids=due_ids,
            due_ats={item.id: due_at(item, settings) for item in all_items},
            filters={"q": q, "category": category, "status": status},
            status_links=status_links,
        )

    # ---- adding items: the form, or the smart entry and its review card -------------------

    def item_values(form) -> dict:
        """What a submitted item form holds, to show it again with an error."""
        return {key: form.get(key) for key in (*item_store.FIELDS, "id", "image_path")} | {
            "bulk_ok": bool(form.get("bulk_ok"))
        }

    @app.get("/items/new", response_class=HTMLResponse)
    def item_new(request: Request, mode: str = ""):
        if mode not in ("smart", "manual"):
            mode = "smart" if claude.enabled else "manual"
        return render(request, "item_new.html", nav="items", mode=mode, prefill={})

    @app.post("/items/review", response_class=HTMLResponse)
    async def item_review(request: Request):
        form = await request.form()
        if not claude.enabled:
            return redirect("/items/new?mode=manual", err=NOT_CONNECTED)
        text = str(form.get("text") or "").strip()[:2000]
        try:
            photo = await read_photo(form)
        except ValueError as exc:
            return redirect("/items/new?mode=smart", err=str(exc))
        if not text and not photo:
            return redirect("/items/new?mode=smart", err="Say or type what to watch, add a photo, or both.")
        try:
            extracted, link = await run_in_threadpool(extract.draft, claude, text, photo)
        except Unavailable as exc:
            return redirect("/items/new?mode=smart", err=str(exc))
        draft = extracted.to_draft(reference_url=link)
        image_path = save_upload(photo) if photo else None
        with session_factory() as session:
            similar = find_similar(draft.get("name") or "", draft.get("brand"), list(session.scalars(select(Item))))
        return render(
            request,
            "item_review.html",
            nav="items",
            draft=draft,
            extracted=extracted,
            image_path=image_path,
            similar=similar,
        )

    @app.post("/items/review/discard")
    def item_review_discard(image_path: Annotated[str, Form()] = ""):
        remove_upload(image_path)
        return redirect("/items/new?mode=smart", msg="Discarded.")

    @app.get("/api/items/similar")
    def api_items_similar(name: str = "", brand: str = "", exclude: str = ""):
        exclude_id = int(exclude) if exclude.isdigit() else None
        with session_factory() as session:
            matches = find_similar(name, brand or None, list(session.scalars(select(Item))), exclude_id=exclude_id)
            return {
                "matches": [
                    {
                        "id": m.item.id,
                        "name": m.item.display_name,
                        "category": CATEGORY_SHORT.get(m.item.category, m.item.category),
                        "score": m.score,
                        "likely_duplicate": m.likely_duplicate,
                    }
                    for m in matches
                ]
            }

    @app.post("/items")
    async def add_item(request: Request):
        form = dict(await request.form())
        try:
            data = item_store.from_form(form)
        except ValueError as exc:
            values = item_values(form) | {"image_path": upload_name(form.get("image_path"))}
            return render(request, "item_new.html", 400, nav="items", mode="manual", prefill=values, err=str(exc))
        with session_factory() as session:
            item = item_store.create_item(session, data, upload_name(form.get("image_path")))
        return redirect(f"/items/{item.id}", msg=f"Added {item.display_name}. It is checked in the next price check.")

    # ---- one item: its page, editing, the quick actions ------------------------------------

    @app.get("/items/{item_id}", response_class=HTMLResponse)
    def item_detail(request: Request, item_id: int):
        with session_factory() as session:
            item = session.get(Item, item_id)
            if item is None:
                raise HTTPException(404)
            history = list(
                session.scalars(
                    select(Observation)
                    .where(Observation.item_id == item_id)
                    .order_by(Observation.observed_at.desc(), Observation.price)
                    .limit(60)
                )
            )
            checks = list(
                session.scalars(select(RunItem).where(RunItem.item_id == item_id).order_by(RunItem.id.desc()).limit(10))
            )
            settings = get_settings(session)
            now = utcnow()
            insight = item_insight(session, item, now)
            points = list(
                session.scalars(
                    select(PricePoint).where(PricePoint.item_id == item_id).order_by(PricePoint.observed_at.desc())
                )
            )
            breakdown = retailer_breakdown(session, item_id, insight.basis)
            chart_html = (
                line_chart(
                    [
                        Series("Best price", [(p.at, p.best) for p in insight.points]),
                        Series("Average of offers", [(p.at, p.avg) for p in insight.points]),
                    ],
                    chart_id=f"item-{item_id}",
                    title="Price per check" + (f", per {item.unit or 'unit'}" if insight.basis == "unit" else ""),
                    unit_suffix=insight.unit_label,
                )
                if insight.points
                else ""
            )
        return render(
            request,
            "item_detail.html",
            settings=settings,
            nav="items",
            now=now,
            item=item,
            history=history,
            checks=checks,
            due=is_due(item, now, settings),
            due_at=due_at(item, settings),
            insight=insight,
            points=points,
            breakdown=breakdown,
            chart_html=chart_html,
        )

    @app.get("/items/{item_id}/edit", response_class=HTMLResponse)
    def item_edit(request: Request, item_id: int):
        with session_factory() as session:
            item = session.get(Item, item_id)
            if item is None:
                raise HTTPException(404)
        values = {key: getattr(item, key) for key in item_store.FIELDS} | {"id": item.id}
        return render(request, "item_edit.html", nav="items", item=item, values=values)

    @app.post("/items/{item_id}")
    async def update_item(request: Request, item_id: int):
        form = dict(await request.form())
        with session_factory() as session:
            item = session.get(Item, item_id)
            if item is None:
                raise HTTPException(404)
            try:
                item_store.change_item(session, item, item_store.from_form(form).model_dump())
            except ValueError as exc:
                values = item_values(form) | {"id": item_id, "image_path": None}
                return render(request, "item_edit.html", 400, nav="items", item=item, values=values, err=str(exc))
        return redirect(f"/items/{item_id}", msg="Saved.")

    @app.post("/items/{item_id}/action")
    def item_action(item_id: int, action: Annotated[str, Form()]):
        with session_factory() as session:
            item = session.get(Item, item_id)
            if item is None:
                raise HTTPException(404)
            if action == "delete":
                remove_upload(item.image_path)
                session.delete(item)
                session.commit()
                return redirect("/items", msg=f"Deleted {item.display_name}.")
            if action == "remove_photo":
                remove_upload(item.image_path)
                item.image_path = None
                session.commit()
                return redirect(f"/items/{item_id}", msg="Photo removed.")
            try:
                msg = item_store.apply_action(session, item, action, get_settings(session))
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
        return redirect(f"/items/{item_id}", msg=msg)

    # ---- Ask ---------------------------------------------------------------------------------

    @app.get("/ask", response_class=HTMLResponse)
    def ask_page(request: Request):
        with session_factory() as session:
            turns = ask.history(session, SHOWN_TURNS)
        return render(request, "ask.html", nav="ask", turns=turns)

    @app.post("/ask")
    async def ask_post(request: Request):
        form = await request.form()
        text = str(form.get("question") or "").strip()[:2000]
        try:
            photo = await read_photo(form)
        except ValueError as exc:
            return redirect("/ask", err=str(exc))
        if not claude.enabled:
            return redirect("/ask", err=NOT_CONNECTED)
        if not text and not photo:
            return redirect("/ask")
        model = str(form.get("model") or "")

        def answer() -> None:
            with session_factory() as session:
                ask.reply(claude, session, get_settings(session), text, model, photo)

        await run_in_threadpool(answer)
        return RedirectResponse("/ask#latest", status_code=303)

    @app.post("/ask/clear")
    def ask_clear():
        with session_factory() as session:
            ask.clear_history(session)
        return redirect("/ask")

    # ---- retailers --------------------------------------------------------------------------

    def _categories(raw: str | None) -> str | None:
        return ",".join(c for c in (raw or "").replace(" ", "").split(",") if c in CATEGORY_KEYS) or None

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
        status = form.get("status") if form.get("status") in RETAILER_STATUSES else "enabled"
        with session_factory() as session:
            if session.scalar(select(Retailer).where(Retailer.domain == domain)):
                return redirect("/retailers", err=f"{domain} is already in the list.")
            session.add(
                Retailer(
                    name=name or domain,
                    domain=domain,
                    status=status,
                    categories=_categories(form.get("categories")),
                    notes=(form.get("notes") or "").strip() or None,
                )
            )
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
            retailer.categories = _categories(form.get("categories"))
            retailer.notes = (form.get("notes") or "").strip() or None
            try:
                retailer.sort_order = int(form.get("sort_order") or retailer.sort_order)
            except ValueError:
                return redirect("/retailers", err="Order must be a whole number.")
            session.commit()
        return redirect("/retailers", msg=f"Saved {retailer.domain}.")

    # ---- price checks -----------------------------------------------------------------------

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request):
        with session_factory() as session:
            runs = list(session.scalars(select(Run).order_by(Run.id.desc()).limit(100)))
        return render(request, "runs.html", nav="runs", runs=runs)

    @app.post("/runs/start")
    def start_run(force: Annotated[str | None, Form()] = None, next: Annotated[str, Form()] = "/runs"):
        back = security.safe_next(next, "/runs")
        client = client_factory()
        if client is None:
            return redirect(back, err=NOT_CONNECTED)
        if is_run_active():
            return redirect(back, err="A price check is already in progress.")

        def worker() -> None:
            try:
                execute_run(session_factory, client, trigger="manual", force=bool(force))
            except RunInProgress:
                log.warning("price check skipped: one is already running")
            except Exception:
                log.exception("manual price check failed")

        threading.Thread(target=worker, name="dealwatch-check", daemon=True).start()
        return redirect(back, msg="Price check started. It takes a few minutes.")

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: int):
        with session_factory() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise HTTPException(404)
            report = session.scalar(select(Report).where(Report.run_id == run_id))
            observations = list(
                session.execute(
                    select(Observation, Item)
                    .join(Item, Item.id == Observation.item_id)
                    .where(Observation.run_id == run_id)
                    .order_by(Observation.is_deal.desc(), Observation.score.desc())
                ).all()
            )
            log_data = json.loads(run.log_json) if run.log_json else {}
            plan_data = json.loads(run.plan_json) if run.plan_json else {}
        return render(
            request,
            "run_detail.html",
            nav="runs",
            run=run,
            report=report,
            observations=observations,
            log=log_data,
            plan_pretty=json.dumps(plan_data, indent=1, ensure_ascii=False),
        )

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
        values = {
            spec.key: (spec.key in form) if spec.kind == "bool" else form[spec.key]
            for spec in SPECS
            if spec.kind == "bool" or spec.key in form
        }
        with session_factory() as session:
            errors = save_settings(session, values)
            claude.timezone = get_settings(session).timezone
        if errors:
            return redirect("/settings", err=" · ".join(errors))
        scheduler.reload()
        return redirect("/settings", msg="Settings saved.")

    @app.post("/seed-samples")
    def seed_samples():
        with session_factory() as session:
            added = seed_sample_items(session)
        return redirect("/items", msg=f"Added {added} sample item(s).")

    # ---- tracker and exports ----------------------------------------------------------------

    @app.get("/tracker", response_class=HTMLResponse)
    def tracker_page(request: Request):
        with session_factory() as session:
            now = utcnow()
            insights = all_item_insights(session, now)
            ov = overview(session, now, insights)
            sparks = {i.item.id: sparkline([p.best for p in i.points][-12:]) for i in insights if i.n_points >= 2}
        tracked = sorted(
            [i for i in insights if i.n_points],
            key=lambda i: i.change_vs_prev_pct if i.change_vs_prev_pct is not None else 0,
        )
        untracked = [i for i in insights if not i.n_points]
        return render(
            request, "tracker.html", nav="tracker", ov=ov, tracked=tracked, untracked=untracked, sparks=sparks
        )

    def _csv_response(model, filename: str, where=None) -> Response:
        skip = _EXPORT_SKIP.get(filename, set())
        columns = [c.name for c in model.__table__.columns if c.name not in skip]
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(columns)
        with session_factory() as session:
            query = select(model).order_by(model.id)
            if where is not None:
                query = query.where(where)
            for row in session.scalars(query):
                writer.writerow([getattr(row, c) for c in columns])
        return Response(
            buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'},
        )

    @app.get("/export/{table}.csv")
    def export_csv(table: str):
        model = _EXPORTS.get(table)
        if model is None:
            raise HTTPException(404, f"unknown table; choose one of {', '.join(_EXPORTS)}")
        return _csv_response(model, table)

    @app.get("/items/{item_id}/price_points.csv")
    def export_item_points(item_id: int):
        return _csv_response(PricePoint, f"item-{item_id}-price_points", PricePoint.item_id == item_id)

    return app
