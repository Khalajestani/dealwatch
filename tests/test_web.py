import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from dealwatch.models import Item, Retailer
from dealwatch.services.llm import FakeSearchClient
from dealwatch.settings import get_settings
from dealwatch.web.app import create_app


@pytest.fixture
def client(session_factory, env):
    app = create_app(env, session_factory=session_factory, search_client=FakeSearchClient(), start_scheduler=False)
    with TestClient(app) as c:
        yield c


def test_pages_render(client):
    for path in ["/", "/items", "/retailers", "/runs", "/settings", "/items/1"]:
        r = client.get(path)
        assert r.status_code == 200, path
    assert client.get("/api/health").json()["ok"] is True
    assert client.get("/items/999").status_code == 404


def test_add_and_edit_item(client, session_factory):
    r = client.post("/items", data={"name": "Bin bags", "brand": "Komo", "category": "household", "priority": "need",
                                    "unit": "piece", "target_price": "3,50", "bulk_ok": "1", "search_hints": "60 liter"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    with session_factory() as s:
        item = s.scalar(select(Item).where(Item.name == "Bin bags"))
        assert item.target_price == 3.5 and item.priority == "need" and item.bulk_ok
    r = client.post(f"/items/{item.id}", data={"name": "Bin bags", "category": "household", "priority": "stock_up", "target_price": "abc"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    r = client.post(f"/items/{item.id}/action", data={"action": "bought"}, follow_redirects=False)
    assert r.status_code == 303
    with session_factory() as s:
        assert s.get(Item, item.id).snooze_until is not None
    r = client.post(f"/items/{item.id}/action", data={"action": "delete"}, follow_redirects=False)
    with session_factory() as s:
        assert s.get(Item, item.id) is None


def test_retailer_config(client, session_factory):
    r = client.post("/retailers", data={"name": "Blokker", "domain": "https://www.Blokker.nl/winkel", "status": "enabled", "categories": "household, bogus"}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    with session_factory() as s:
        blokker = s.scalar(select(Retailer).where(Retailer.domain == "blokker.nl"))
        assert blokker.categories == "household"
    assert "err=" in client.post("/retailers", data={"domain": "blokker.nl"}, follow_redirects=False).headers["location"]
    assert "err=" in client.post("/retailers", data={"domain": "*.bad"}, follow_redirects=False).headers["location"]
    client.post(f"/retailers/{blokker.id}", data={"action": "blocked"}, follow_redirects=False)
    with session_factory() as s:
        assert s.get(Retailer, blokker.id).status == "blocked"
    client.post(f"/retailers/{blokker.id}", data={"action": "save", "name": "Blokker NL", "categories": "tools_diy", "notes": "n"}, follow_redirects=False)
    with session_factory() as s:
        r2 = s.get(Retailer, blokker.id)
        assert r2.name == "Blokker NL" and r2.categories == "tools_diy" and r2.status == "blocked"
    client.post(f"/retailers/{blokker.id}", data={"action": "delete"}, follow_redirects=False)
    with session_factory() as s:
        assert s.get(Retailer, blokker.id) is None


def test_settings_validation(client, session_factory):
    r = client.post("/settings", data={"group_size": "4", "schedule_cron": "not a cron", "search_model": "claude-opus-5"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    with session_factory() as s:
        assert get_settings(s).group_size == 6  # nothing saved on error
    r = client.post("/settings", data={"group_size": "4", "schedule_cron": "0 8 * * 2", "search_model": "claude-opus-5", "search_model_custom": "claude-sonnet-5", "schedule_enabled": "1"}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    with session_factory() as s:
        st = get_settings(s)
        assert st.group_size == 4 and st.schedule_cron == "0 8 * * 2" and st.search_model == "claude-sonnet-5"
        assert st.search_trim_response is False  # unchecked checkbox -> false


def test_dry_run_and_background_run(client):
    r = client.post("/runs/plan", follow_redirects=False)
    assert r.status_code == 303 and "/runs/" in r.headers["location"]
    page = client.get(r.headers["location"].split("?")[0])
    assert page.status_code == 200 and "Dealwatch plan" in page.text

    r = client.post("/runs/start", data={"force": "1"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    for _ in range(100):
        latest = client.get("/api/runs/latest").json()["run"]
        if latest and latest["status"] != "running":
            break
        time.sleep(0.1)
    assert latest["status"] == "success"
    detail = client.get(f"/runs/{latest['id']}")
    assert detail.status_code == 200 and "All offers seen" in detail.text
    assert "Latest deals" in client.get("/").text
    assert client.get(f"/runs/{latest['id']}/report.md").text.startswith("# Dealwatch report")
