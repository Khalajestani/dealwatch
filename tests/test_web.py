import json
import time
from html.parser import HTMLParser
from pathlib import Path

import pytest
from conftest import FakeClaude, FakeSearchClient, make_image
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from dealwatch.models import ChatMessage, Item, Retailer, Run
from dealwatch.settings import get_settings
from dealwatch.web.app import create_app


@pytest.fixture
def fake_claude():
    return FakeClaude()


@pytest.fixture
def client(session_factory, env, fake_claude):
    app = create_app(env, session_factory=session_factory, search_client=FakeSearchClient(), claude=fake_claude)
    with TestClient(app) as c:
        yield c


def wait_for_check(client, session_factory) -> Run:
    for _ in range(100):
        with session_factory() as s:
            run = s.scalars(select(Run).order_by(Run.id.desc())).first()
        if run and run.status != "running":
            return run
        time.sleep(0.1)
    raise AssertionError("the check did not finish")


def test_pages_render(client):
    for path in [
        "/",
        "/items",
        "/items/new",
        "/items/new?mode=manual",
        "/ask",
        "/retailers",
        "/runs",
        "/tracker",
        "/settings",
        "/items/1",
        "/items/1/edit",
    ]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert "Sign out" not in r.text and "logout" not in r.text.lower(), "there is no sign-out (owner's request)"
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/items/999").status_code == 404
    assert client.get("/manifest.webmanifest").json()["short_name"] == "Dealwatch"
    assert client.get("/sw.js").status_code == 200 and client.get("/apple-touch-icon.png").status_code == 200
    assert "Content-Security-Policy" in client.get("/").headers


def test_login_guards_everything_but_health_and_lasts_without_sign_out(session_factory, env):
    env = env.model_copy(update={"auth_password": SecretStr("correct horse"), "secret_key": SecretStr("k" * 40)})
    with TestClient(create_app(env, session_factory=session_factory, claude=FakeClaude(enabled=False))) as c:
        assert c.get("/healthz").status_code == 200
        r = c.get("/items?q=x", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login?next=/items%3Fq%3Dx"
        assert c.post("/login", data={"password": "wrong", "next": "/items"}).status_code == 401
        r = c.post("/login", data={"password": "correct horse", "next": "//evil.example"}, follow_redirects=False)
        assert r.headers["location"] == "/", "redirects stay on the site"
        assert c.get("/items").status_code == 200 and "Sign out" not in c.get("/items").text
        assert c.post("/logout").status_code in (303, 404, 405)
        assert c.get("/items").status_code == 200
        assert c.post("/seed-samples", headers={"Origin": "https://evil.example"}).status_code == 403


def test_a_short_signing_key_is_refused_with_a_password(env):
    with pytest.raises(ValueError, match="DEALWATCH_SECRET_KEY"):
        env.model_validate(env.model_dump() | {"auth_password": "pw", "secret_key": "short"})


def test_add_and_edit_item(client, session_factory):
    r = client.post(
        "/items",
        data={
            "name": "Bin bags",
            "brand": "Komo",
            "category": "household",
            "priority": "need",
            "unit": "piece",
            "target_price": "3,50",
            "bulk_ok": "1",
            "search_hints": "60 liter",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    with session_factory() as s:
        item = s.scalar(select(Item).where(Item.name == "Bin bags"))
        assert item.target_price == 3.5 and item.priority == "need" and item.bulk_ok
        item.reference_url, item.notes = "https://www.bol.com/p/1", "the blue ones"
        s.commit()

    edit = client.get(f"/items/{item.id}/edit").text
    add = client.get("/items/new?mode=manual").text
    for page in (edit, add):  # one form for both, asking only what the checks use
        assert 'name="search_hints"' in page and 'name="target_unit_price"' in page
        assert 'name="notes" value' not in add and '<input type="url"' not in page
    assert 'value="Bin bags"' in edit and 'name="reference_url" value="https://www.bol.com/p/1"' in edit
    client.post(f"/items/{item.id}", data=_form_of(edit) | {"search_hints": "60 liter, 20 stuks"})
    with session_factory() as s:
        item = s.get(Item, item.id)
        assert item.search_hints == "60 liter, 20 stuks" and item.target_price == 3.5
        assert (item.reference_url, item.notes) == ("https://www.bol.com/p/1", "the blue ones"), "kept as they were"

    r = client.post(
        f"/items/{item.id}",
        data={"name": "Bin bags", "category": "household", "priority": "stock_up", "target_price": "abc"},
        follow_redirects=False,
    )
    assert r.status_code == 400 and "Target price" in r.text and 'value="abc"' in r.text, "typed values stay"
    r = client.post("/items", data={"name": "", "brand": "Komo"}, follow_redirects=False)
    assert r.status_code == 400 and "a name is required" in r.text and 'value="Komo"' in r.text
    for action in ("bought", "pause", "resume", "check_next"):
        assert (
            client.post(f"/items/{item.id}/action", data={"action": action}, follow_redirects=False).status_code == 303
        )
    with session_factory() as s:
        item = s.get(Item, item.id)
        assert item.active and item.last_bought_at is not None and item.snooze_until is None
    client.post(f"/items/{item.id}/action", data={"action": "delete"}, follow_redirects=False)
    with session_factory() as s:
        assert s.get(Item, item.id) is None


def test_already_bought_in_one_click_kept_in_purchased_and_undone(client, session_factory):
    with session_factory() as s:
        drill = s.scalar(select(Item).where(Item.name == "Cordless drill"))
    link = f'href="/items/{drill.id}"'
    page = client.get("/items").text
    assert 'value="already_bought"' in page and 'value="bought"' in page, "one-offs and refills each get their button"

    r = client.post(
        f"/items/{drill.id}/action",
        data={"action": "already_bought", "next": "/items?category=tools_diy"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/items?category=tools_diy&msg="), (
        "back where it was"
    )
    with session_factory() as s:
        assert s.get(Item, drill.id).status == "bought"
    listed, purchased = client.get("/items").text.split('<details class="more-block">\n  <summary>Purchased', 1)
    assert link not in listed and link in purchased, "collapsed Purchased section"
    assert "undo_bought" in purchased and "bought " in purchased
    assert link not in client.get("/").text
    assert "Undo" in client.get(f"/items/{drill.id}").text

    r = client.post(f"/items/{drill.id}/action", data={"action": "undo_bought", "next": "https://example.com"})
    assert r.url.path == f"/items/{drill.id}", "only local return paths"
    with session_factory() as s:
        assert s.get(Item, drill.id).status == "active"
    assert "<summary>Purchased" not in client.get("/items").text


class _FormReader(HTMLParser):
    """Collects what a form sends as it stands: text and hidden fields, ticked boxes, chosen options."""

    def __init__(self) -> None:
        super().__init__()
        self.data: dict[str, str] = {}
        self.select: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input" and a.get("name") and a.get("type") != "file":
            if a.get("type") not in ("checkbox", "radio"):
                self.data[a["name"]] = a.get("value") or ""
            elif "checked" in a:
                self.data[a["name"]] = a.get("value") or "on"
        elif tag == "select":
            self.select = a["name"]
            self.data.setdefault(self.select, "")
        elif tag == "option" and self.select and "selected" in a:
            self.data[self.select] = a.get("value") or ""

    def handle_endtag(self, tag):
        if tag == "select":
            self.select = None


def _form_of(page: str) -> dict:
    """The values the item form on a page would send unchanged."""
    reader = _FormReader()
    reader.feed(page.split('class="item-form"', 1)[1].split("</form>", 1)[0])
    return reader.data


def test_smart_entry_fills_the_review_card_and_keeps_the_photo_until_add(client, fake_claude, env):
    payload = {
        "name": "Dishwasher tablets",
        "brand": "Dreft",
        "category": "household",
        "unit": "tablet",
        "pack_quantity": 60,
        "priority": "need",
        "confidence": 0.9,
        "summary": "Label readable.",
    }
    fake_claude.script(json.dumps(payload))
    r = client.post(
        "/items/review",
        data={"mode": "smart", "text": "these, running low"},
        files={"photo": ("p.jpg", make_image(), "image/jpeg")},
    )
    assert r.status_code == 200 and 'value="Dishwasher tablets"' in r.text and "Filled in by Claude" in r.text
    assert fake_claude.calls[0]["photo"] and "running low" in fake_claude.calls[0]["text"]
    photo = r.text.split('name="image_path" value="')[1].split('"')[0]
    assert (env.uploads_dir / photo).exists()
    client.post(
        "/items", data={"name": "Dishwasher tablets", "category": "household", "priority": "need", "image_path": photo}
    )
    fake_claude.script("not json")
    r = client.post("/items/review", data={"mode": "smart", "text": "hmm"}, follow_redirects=False)
    assert "err=Could%20not%20tell" in r.headers["location"]
    r = client.post("/items/review", data={"mode": "smart", "text": ""}, follow_redirects=False)
    assert "err=" in r.headers["location"]


def test_ask_answers_and_keeps_the_conversation(client, fake_claude, session_factory):
    def model(tools):
        assert set(tools) == {"get_item", "latest_deals", "add_item", "update_item", "item_action"}
        assert "Not added: #1" in tools["add_item"].call({"name": "Kitchen towels", "brand": "Page"})
        assert "Added #9 Komo Bin bags" in tools["add_item"].call(
            {"name": "Bin bags", "brand": "Komo", "unit": "piece", "target_unit_price": 0.12}
        )
        assert "Will be checked" in tools["item_action"].call({"item_id": 9, "action": "check_next"})
        with pytest.raises(ValueError):
            tools["update_item"].call({"item_id": 9, "unit": "barrel"})
        return "Added the bin bags."

    fake_claude.script(model)
    page = client.post("/ask", data={"question": "Watch Komo bin bags under 12 cents a bag", "model": "made-up"}).text
    assert "Watch Komo bin bags under 12 cents a bag" in page and "Added the bin bags." in page
    call = fake_claude.calls[0]
    assert (call["model"], call["effort"]) == ("haiku", "low"), "an unknown model falls back"
    items = json.loads(call["system"].split("usual its normal price per unit or pack)\n")[1].split("\n\n")[0])
    assert items[0]["name"] == "Page Kitchen towels" and items[0]["latest_best"] is None

    fake_claude.script("Nothing yet.")
    client.post("/ask", data={"question": "Deals?", "model": "sonnet"})
    assert "EARLIER IN THIS CONVERSATION\nUser: Watch Komo bin bags" in fake_claude.calls[1]["system"]
    assert fake_claude.calls[1]["model"] == "sonnet"
    client.post("/ask/clear")
    with session_factory() as s:
        assert s.scalars(select(ChatMessage)).first() is None


def test_without_claude_the_app_says_so_and_nothing_runs(session_factory, env):
    claude = FakeClaude(AssertionError("must not run"), enabled=False)
    with TestClient(create_app(env, session_factory=session_factory, claude=claude)) as c:
        assert "Claude is not connected yet" in c.get("/ask").text
        assert 'name="question"' not in c.get("/ask").text
        assert "Claude is not connected yet" in c.get("/items/new").text
        assert "err=" in c.post("/runs/start", follow_redirects=False).headers["location"]
        assert "err=" in c.post("/ask", data={"question": "hi"}, follow_redirects=False).headers["location"]
        assert c.get("/items/new").text.count('class="panel active"') == 1


def test_retailer_config(client, session_factory):
    r = client.post(
        "/retailers",
        data={
            "name": "Blokker",
            "domain": "https://www.Blokker.nl/winkel",
            "status": "enabled",
            "categories": "household, bogus",
        },
        follow_redirects=False,
    )
    assert "msg=" in r.headers["location"]
    with session_factory() as s:
        blokker = s.scalar(select(Retailer).where(Retailer.domain == "blokker.nl"))
        assert blokker.categories == "household"
    assert (
        "err=" in client.post("/retailers", data={"domain": "blokker.nl"}, follow_redirects=False).headers["location"]
    )
    assert "err=" in client.post("/retailers", data={"domain": "*.bad"}, follow_redirects=False).headers["location"]
    client.post(f"/retailers/{blokker.id}", data={"action": "blocked"}, follow_redirects=False)
    with session_factory() as s:
        assert s.get(Retailer, blokker.id).status == "blocked"
    client.post(
        f"/retailers/{blokker.id}",
        data={"action": "save", "name": "Blokker NL", "categories": "tools_diy", "notes": "n"},
        follow_redirects=False,
    )
    with session_factory() as s:
        r2 = s.get(Retailer, blokker.id)
        assert r2.name == "Blokker NL" and r2.categories == "tools_diy" and r2.status == "blocked"
    client.post(f"/retailers/{blokker.id}", data={"action": "delete"}, follow_redirects=False)
    with session_factory() as s:
        assert s.get(Retailer, blokker.id) is None


def test_settings_validation(client, session_factory):
    r = client.post(
        "/settings",
        data={"group_size": "4", "schedule_time": "0 7 * * *", "search_model": "sonnet"},
        follow_redirects=False,
    )
    assert "err=" in r.headers["location"]
    with session_factory() as s:
        assert get_settings(s).group_size == 6  # nothing saved on error
    assert (
        "err="
        in client.post("/settings", data={"search_model": "claude-opus-5"}, follow_redirects=False).headers["location"]
    )
    r = client.post(
        "/settings",
        data={"group_size": "4", "schedule_time": "4:15", "search_model": "opus", "min_discount_pct": "12,5"},
        follow_redirects=False,
    )
    assert "msg=" in r.headers["location"]
    with session_factory() as s:
        st = get_settings(s)
        assert st.group_size == 4 and st.schedule_time == "04:15" and st.search_model == "opus"
        assert st.min_discount_pct == 12.5  # a comma is a decimal point
        assert st.schedule_enabled is False  # unchecked checkbox


def test_background_check_report_tracker_and_exports(client, session_factory):
    empty = client.get("/tracker")
    assert empty.status_code == 200 and "No price history yet" in empty.text
    assert client.get("/export/nope.csv").status_code == 404

    r = client.post("/runs/start", data={"force": "1"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    run = wait_for_check(client, session_factory)
    assert run.status == "success" and run.trigger == "manual"
    detail = client.get(f"/runs/{run.id}")
    assert (
        detail.status_code == 200
        and "All offers seen" in detail.text
        and "What Claude searched and read" in detail.text
    )
    home = client.get("/").text
    assert "Checked today" in home and "no problems" in home and f'href="/runs/{run.id}"' in home
    assert ('class="deal-card"' in home) == bool(run.deal_count), "the deals this check found, marked new"
    assert home.count('class="row linked"') >= 3 and "Watching" in home
    with session_factory() as s:
        due_now = s.scalar(select(Item).where(Item.name == "Kitchen towels"))
        due_now.last_checked_at = None
        s.commit()
    assert "Check 1 due" in client.get("/").text and "next check" in client.get("/items?status=due").text
    assert client.get(f"/runs/{run.id}/report.md").text.startswith("# Dealwatch report")

    csv_text = client.get("/export/price_points.csv").text
    header, first = csv_text.splitlines()[:2]
    assert header.startswith("id,item_id,run_id,observed_at") and "min_unit_price" in header
    item_id = int(first.split(",")[1])
    page = client.get(f"/items/{item_id}")
    assert page.status_code == 200 and "viz-line-chart" in page.text and "Price points (one per check)" in page.text
    assert client.get(f"/items/{item_id}/price_points.csv").headers["content-type"].startswith("text/csv")
    tracker = client.get("/tracker")
    assert tracker.status_code == 200 and "items with price history" in tracker.text and "viz-spark" not in tracker.text
    assert "unit_price" in client.get("/export/observations.csv").text.splitlines()[0]


def test_a_check_cut_off_by_a_restart_is_marked_failed(session_factory, env):
    with session_factory() as s:
        s.add(Run(status="running"))
        s.commit()
    create_app(env, session_factory=session_factory, claude=FakeClaude(enabled=False))
    with session_factory() as s:
        assert s.scalars(select(Run)).one().status == "failed"


def test_every_app_file_is_readable_in_the_image():
    """The image keeps file modes and runs as an ordinary user: an owner-only file would be a 500 on the Pi."""
    root = Path(__file__).parents[1]
    unreadable = [
        str(p.relative_to(root))
        for top in ("src",)
        for p in (root / top).rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and not p.stat().st_mode & 0o004
    ]
    assert unreadable == []
