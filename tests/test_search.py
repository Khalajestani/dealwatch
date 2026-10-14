"""The price search around Claude: the batch message, the record_item tool, and what a cut-short batch keeps."""

import json
from datetime import date

import pytest
from conftest import FakeClaude

from dealwatch.services.claude import CannotRun, Unavailable
from dealwatch.services.planner import PlannedGroup, PlannedItem
from dealwatch.services.search import SYSTEM_PROMPT, ClaudeSearchClient, Offer
from dealwatch.settings import Settings

TODAY = date(2026, 9, 14)


def group() -> PlannedGroup:
    items = [
        PlannedItem(
            id=1,
            name="Kitchen towels",
            brand="Page",
            category="household",
            search_hints="8 rollen",
            unit="roll",
            bulk_ok=True,
            priority="stock_up",
            target_price=None,
            target_unit_price=0.7,
            baseline=0.86,
            baseline_basis="unit",
        ),
        PlannedItem(
            id=2,
            name="Toilet paper",
            brand="Page",
            category="household",
            search_hints=None,
            unit="roll",
            bulk_ok=True,
            priority="need",
            target_price=None,
            target_unit_price=None,
        ),
    ]
    return PlannedGroup(
        index=1,
        category="household",
        items=items,
        retailers=[{"name": "Kruidvat", "domain": "kruidvat.nl"}],
        allowed_domains=["kruidvat.nl", "ah.nl"],
        blocked_domains=[],
        max_uses=5,
    )


OFFER = {
    "retailer_domain": "https://www.Kruidvat.nl/x",
    "title": "Page keukenrol",
    "url": "https://www.kruidvat.nl/p/1",
    "price": 4.99,
    "currency": "eur",
    "list_price": 6.99,
    "pack_quantity": 8,
    "unit": "roll",
    "promo_type": "weird",
    "valid_until": "2026-09-20",
    "confidence": 1.7,
    "evidence": "4,99 van 6,99",
}


def test_the_batch_runs_with_its_shops_budget_and_one_tool():
    claude = FakeClaude("done")
    ClaudeSearchClient(claude).search_group(group(), Settings(), TODAY)
    call = claude.calls[0]
    assert call["system"] == SYSTEM_PROMPT and (call["model"], call["effort"]) == ("sonnet", "medium")
    assert call["web"].allowed == ["kruidvat.nl", "ah.nl"] and call["web"].budget == 5
    assert [t.name for t in call["tools"]] == ["record_item"] and call["max_turns"] == 5 + 4 + 4
    body = json.loads(call["text"].split("\n\n", 1)[1])
    assert body["web_lookup_budget"] == 5 and body["items"][0]["target_unit_price"] == 0.7
    assert "offers" in json.dumps(call["tools"][0].input_schema)


def test_recorded_items_are_validated_and_normalised():
    def model(tools):
        record = tools["record_item"]
        assert "Still to record: [2]" in record.call({"item_id": 1, "status": "found", "note": "ok", "offers": [OFFER]})
        with pytest.raises(ValueError, match="not in this batch"):
            record.call({"item_id": 99, "status": "not_found"})
        with pytest.raises(ValueError):
            record.call({"item_id": 2, "status": "maybe"})
        assert "All recorded" in record.call({"item_id": 2, "status": "not_found", "note": "nothing"})
        return "done"

    result = ClaudeSearchClient(FakeClaude(model)).search_group(group(), Settings(), TODAY)
    assert result.error is None and [r.item_id for r in result.results] == [1, 2]
    offer = result.results[0].offers[0]
    assert (offer.retailer_domain, offer.currency, offer.promo_type, offer.confidence) == (
        "kruidvat.nl",
        "EUR",
        "none",
        1.0,
    )
    assert offer.valid_until_date == date(2026, 9, 20)


def test_a_batch_cut_short_keeps_what_it_recorded():
    def recorded_one_then_failed(tools):
        tools["record_item"].call({"item_id": 1, "status": "found", "offers": [OFFER]})
        raise Unavailable("Claude took too long and was stopped.")

    result = ClaudeSearchClient(FakeClaude(recorded_one_then_failed)).search_group(group(), Settings(), TODAY)
    assert result.error.startswith("Claude took too long") and not result.stop
    assert [r.item_id for r in result.results] == [1]

    result = ClaudeSearchClient(FakeClaude(CannotRun("limit"))).search_group(group(), Settings(), TODAY)
    assert result.stop and result.results == []


def test_offer_fields_are_cleaned():
    offer = Offer(**OFFER | {"pack_quantity": 0, "list_price": -1, "valid_until": "soon"})
    assert offer.pack_quantity is None and offer.list_price is None and offer.valid_until_date is None
