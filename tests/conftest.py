"""Fixtures and stand-ins. Nothing here reaches Claude: `FakeSearchClient` makes up prices, `FakeClaude` replays
scripted replies (and can call the tools it was given, as the model would), `claude_code` scripts the messages
Claude Code itself sends back."""

from __future__ import annotations

import io
import random
from datetime import timedelta

import pytest
from claude_agent_sdk import ResultMessage
from PIL import Image

from dealwatch.config import Env
from dealwatch.db import init_db, make_engine, make_session_factory
from dealwatch.seed import seed_retailers, seed_sample_items
from dealwatch.services import claude as claude_module
from dealwatch.services.search import GroupSearchResult, ItemResult, Offer

_UNIT_PACKS = {
    "roll": 8,
    "tablet": 60,
    "wash": 40,
    "capsule": 30,
    "bag": 10,
    "ml": 500,
    "l": 1,
    "g": 500,
    "kg": 1,
    "m": 10,
    "pair": 1,
    "piece": 1,
}
_UNIT_BASE_PRICE = {
    "roll": 0.85,
    "tablet": 0.18,
    "wash": 0.28,
    "capsule": 0.30,
    "bag": 0.25,
    "ml": 0.014,
    "l": 3.5,
    "g": 0.02,
    "kg": 14.0,
    "m": 2.0,
    "pair": 8.0,
    "piece": 9.0,
}


class FakeSearchClient:
    """Deterministic stand-in: same item, same normal price, with wobble per day and occasional promotions."""

    def __init__(self, seed: int = 0, fail_group: int | None = None, stop_group: int | None = None) -> None:
        self.seed, self.fail_group, self.stop_group = seed, fail_group, stop_group
        self.calls = 0

    def search_group(self, group, settings, today):
        self.calls += 1
        result = GroupSearchResult(group_index=group.index, model="fake")
        if group.index in (self.fail_group, self.stop_group):
            result.error, result.stop = "simulated batch failure", group.index == self.stop_group
            return result
        domains = [r["domain"] for r in group.retailers] or ["example.com"]
        for item in group.items:
            rng = random.Random(f"{self.seed}:{item.id}:{today.isoformat()}")
            result.lookups.append(f"search: {item.display_name} aanbieding")
            if rng.random() < 0.08:
                result.results.append(
                    ItemResult(item_id=item.id, status="not_found", note="fake: nothing credible found")
                )
                continue
            unit = item.unit or "piece"
            pack = _UNIT_PACKS.get(unit, 1)
            normal_unit = _UNIT_BASE_PRICE.get(unit, 9.0) * (0.8 + (item.id % 7) * 0.07)
            offers = []
            for n, domain in enumerate(rng.sample(domains, k=min(len(domains), 2))):
                promo = rng.random() < 0.3
                price = round(normal_unit * pack * (rng.uniform(0.6, 0.8) if promo else rng.uniform(0.92, 1.06)), 2)
                offers.append(
                    Offer(
                        retailer_domain=domain,
                        title=f"{item.display_name} {pack} {unit} (fake listing {n + 1})",
                        url=f"https://www.{domain}/p/fake-{item.id}-{n}",
                        price=price,
                        list_price=round(normal_unit * pack * 1.02, 2) if promo else None,
                        pack_quantity=pack,
                        unit=unit,
                        promo_type="sale" if promo else "none",
                        promo_text="fake promotion" if promo else None,
                        valid_until=(today + timedelta(days=6)).isoformat() if promo else None,
                        confidence=round(rng.uniform(0.6, 0.95), 2),
                        evidence=f"fake evidence: {price:.2f} for {pack} {unit}",
                    )
                )
            offers.sort(key=lambda o: o.price)
            result.results.append(ItemResult(item_id=item.id, status="found", note="fake data", offers=offers))
        return result


class FakeClaude:
    """Stands in for services.claude.Claude. Each scripted reply is a string, an exception to raise, or a function
    that gets the tools by name (to call them as the model would) and returns the reply."""

    def __init__(self, *replies, enabled: bool = True) -> None:
        self.replies, self.enabled, self.calls = list(replies), enabled, []
        self.timezone = "Europe/Amsterdam"

    def script(self, *replies) -> FakeClaude:
        self.replies = list(replies)
        return self

    def ask(self, text, system, **kwargs):
        self.calls.append({"text": text, "system": system, **kwargs})
        reply = self.replies.pop(0) if self.replies else "ok"
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply({t.name: t for t in kwargs.get("tools") or []})
        return reply


def make_image(size=(800, 600), fmt="JPEG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "orange").save(buffer, fmt)
    return buffer.getvalue()


def result(text: str | None, *, is_error: bool = False, subtype: str | None = None) -> ResultMessage:
    return ResultMessage(
        subtype=subtype or ("error_during_execution" if is_error else "success"),
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="test",
        result=text,
    )


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(_env_file=None, data_dir=tmp_path / "data", backups_dir=tmp_path / "backups", scheduler=False)


@pytest.fixture
def session_factory(env):
    engine = make_engine(env.db_path)
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        seed_retailers(session)
        seed_sample_items(session)
    return factory


@pytest.fixture
def claude_code(monkeypatch):
    """Stand in for Claude Code itself: `claude_code(*messages)` scripts what the next run sends back."""
    seen: dict = {}

    def script(*messages, raises: Exception | None = None):
        async def fake_query(*, prompt, options):
            seen["prompt"], seen["options"] = prompt, options
            with open(options.system_prompt["path"], encoding="utf-8") as f:
                seen["system"] = f.read()
            if raises:
                raise raises
            for m in messages:
                yield m

        monkeypatch.setattr(claude_module, "query", fake_query)
        return seen

    return script
