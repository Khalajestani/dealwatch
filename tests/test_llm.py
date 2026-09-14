import json
from datetime import date
from types import SimpleNamespace

import pytest

from dealwatch.services.llm import AnthropicSearchClient, build_request, extract_json, model_profile, parse_group_output
from dealwatch.services.planner import PlannedGroup, PlannedItem
from dealwatch.settings import Settings

TODAY = date(2026, 9, 14)


def _group() -> PlannedGroup:
    item = PlannedItem(id=1, name="Kitchen towels", brand="Page", category="household", search_hints="8 rollen", unit="roll",
                       bulk_ok=True, priority="stock_up", target_price=None, target_unit_price=0.7, baseline=0.86, baseline_basis="unit")
    return PlannedGroup(index=1, category="household", items=[item], retailers=[{"name": "Kruidvat", "domain": "kruidvat.nl"}],
                        allowed_domains=["kruidvat.nl", "ah.nl"], blocked_domains=[], max_uses=3)


def _offer(**kw):
    base = dict(retailer_domain="https://www.Kruidvat.nl/x", title="Page keukenrol", url="https://www.kruidvat.nl/p/1", price=4.99,
                currency="eur", list_price=6.99, pack_quantity=8, unit="roll", promo_type="weird", promo_text=None,
                valid_until="2026-09-20", confidence=1.7, evidence="4,99 van 6,99")
    base.update(kw)
    return base


def test_extract_json_tolerates_prose_and_fences():
    payload = {"results": []}
    assert extract_json(json.dumps(payload)) == payload
    assert extract_json("Here you go:\n```json\n" + json.dumps(payload) + "\n```") == payload
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_parse_normalises_fields():
    out = parse_group_output(json.dumps({"results": [{"item_id": 1, "status": "found", "note": "", "offers": [_offer()]}]}))
    offer = out.results[0].offers[0]
    assert offer.retailer_domain == "kruidvat.nl" and offer.currency == "EUR"
    assert offer.promo_type == "none" and offer.confidence == 1.0
    assert offer.valid_until_date == date(2026, 9, 20)


def test_build_request_allowlist_and_options():
    req = build_request(_group(), Settings(), TODAY)
    tool = req["tools"][0]
    assert tool["type"] == "web_search_20260318" and tool["max_uses"] == 3
    assert tool["allowed_domains"] == ["kruidvat.nl", "ah.nl"] and "blocked_domains" not in tool
    assert tool["user_location"]["country"] == "NL" and tool["response_inclusion"] == "excluded"
    assert req["output_config"]["effort"] == "medium" and req["output_config"]["format"]["type"] == "json_schema"
    assert req["thinking"] == {"type": "adaptive"}
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    body = json.loads(req["messages"][0]["content"].split("\n\n", 1)[1])
    assert body["items"][0]["baseline"] == 0.86 and body["search_budget"] == 3 and body["date"] == "2026-09-14"


def test_build_request_legacy_model():
    req = build_request(_group(), Settings(search_model="claude-haiku-4-5", search_trim_response=True), TODAY)
    assert req["tools"][0]["type"] == "web_search_20250305"
    assert "effort" not in req["output_config"] and "thinking" not in req and "response_inclusion" not in req["tools"][0]
    assert model_profile("claude-sonnet-5").supports_effort


class _StubMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _stub_client(responses, beta=True):
    messages = _StubMessages(responses)
    holder = SimpleNamespace(messages=messages)
    return SimpleNamespace(beta=holder, messages=messages), messages


def _usage(inp, out, searches):
    return SimpleNamespace(input_tokens=inp, output_tokens=out, cache_read_input_tokens=0, cache_creation_input_tokens=100,
                           server_tool_use=SimpleNamespace(web_search_requests=searches))


def test_search_group_handles_pause_turn_and_sums_usage():
    query_block = SimpleNamespace(type="server_tool_use", name="web_search", input={"query": "Page keukenrol aanbieding"})
    final_json = json.dumps({"results": [{"item_id": 1, "status": "found", "note": "ok", "offers": [_offer()]}]})
    paused = SimpleNamespace(stop_reason="pause_turn", content=[query_block], usage=_usage(1000, 50, 1), model="claude-opus-5")
    done = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=final_json)], usage=_usage(1500, 400, 1), model="claude-opus-5")
    client, messages = _stub_client([paused, done])

    result = AnthropicSearchClient(client).search_group(_group(), Settings(), TODAY)

    assert result.ok and result.output.results[0].offers[0].price == 4.99
    assert result.usage.api_requests == 2 and result.usage.input_tokens == 2500 and result.usage.web_searches == 2
    assert result.usage.cache_write_tokens == 200 and result.queries == ["Page keukenrol aanbieding"]
    assert messages.calls[0]["betas"] == ["server-side-fallback-2026-07-01"] and messages.calls[0]["fallbacks"] == "default"
    second = messages.calls[1]["messages"]
    assert second[1]["role"] == "assistant" and second[1]["content"] == [query_block]


def test_search_group_without_fallbacks_uses_stable_endpoint():
    done = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text='{"results": []}')], usage=_usage(10, 5, 0), model="m")
    client, messages = _stub_client([done])
    result = AnthropicSearchClient(client).search_group(_group(), Settings(llm_fallbacks=False), TODAY)
    assert result.ok and "betas" not in messages.calls[0]


def test_search_group_reports_refusal_and_bad_json():
    refused = SimpleNamespace(stop_reason="refusal", content=[], usage=_usage(10, 0, 0), model="m", stop_details=SimpleNamespace(category="cyber"))
    client, _ = _stub_client([refused])
    result = AnthropicSearchClient(client).search_group(_group(), Settings(), TODAY)
    assert not result.ok and "declined" in result.error and "cyber" in result.error

    garbage = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="oops")], usage=_usage(10, 0, 0), model="m")
    client, _ = _stub_client([garbage])
    result = AnthropicSearchClient(client).search_group(_group(), Settings(), TODAY)
    assert not result.ok and "parse" in result.error
