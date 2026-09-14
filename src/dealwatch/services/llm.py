"""LLM search layer: one Claude call per batch, with server-side web search and structured JSON output.

Contract: `SearchClient.search_group(group, settings, today) -> GroupSearchResult`.
`AnthropicSearchClient` is the only code that talks to the API. `FakeSearchClient` produces deterministic
offers for tests and for trying the app without spending credits (DEALWATCH_FAKE_LLM=1 or `--fake`).

Cost controls live here and in the planner: allow-listed domains, `max_uses` per batch, a cached static
system prompt, trimmed search payloads, structured output (no re-prompting for JSON), effort setting.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol

import anthropic
from pydantic import ValidationError

from ..settings import Settings
from .planner import PlannedGroup
from .prompts import OUTPUT_SCHEMA, SYSTEM_PROMPT, GroupOutput, ItemResult, Offer, build_user_message

log = logging.getLogger(__name__)

MAX_CONTINUATIONS = 6  # how many `pause_turn` continuations we allow per batch
MAX_TOKENS = 16_000
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class UsageTotals:
    api_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    web_searches: int = 0

    def add_usage(self, usage: Any) -> None:
        self.api_requests += 1
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
        server = getattr(usage, "server_tool_use", None)
        if server is not None:
            self.web_searches += getattr(server, "web_search_requests", 0) or 0


@dataclass
class GroupSearchResult:
    group_index: int
    model: str
    output: GroupOutput | None = None
    usage: UsageTotals = field(default_factory=UsageTotals)
    queries: list[str] = field(default_factory=list)
    error: str | None = None
    raw_text: str | None = None
    served_by: str | None = None
    stop_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.output is not None and self.error is None


class SearchClient(Protocol):
    name: str

    def search_group(self, group: PlannedGroup, settings: Settings, today: date) -> GroupSearchResult: ...


# --- model capabilities -----------------------------------------------------------------------

@dataclass(frozen=True)
class ModelProfile:
    search_tool_type: str
    supports_effort: bool
    supports_adaptive_thinking: bool
    supports_response_inclusion: bool


_LEGACY_MARKERS = ("haiku-4-5", "sonnet-4-5", "opus-4-5", "opus-4-1", "haiku-3", "sonnet-3", "opus-4-0", "sonnet-4-0")


def model_profile(model: str) -> ModelProfile:
    """Older models need the basic search tool and reject `effort` / adaptive thinking."""
    if any(marker in model for marker in _LEGACY_MARKERS):
        return ModelProfile("web_search_20250305", False, False, False)
    return ModelProfile("web_search_20260318", True, True, True)


def locale_payload(settings: Settings) -> dict[str, str]:
    return {"country": settings.country, "city": settings.city, "currency": settings.currency, "language": "nl"}


def build_request(group: PlannedGroup, settings: Settings, today: date) -> dict[str, Any]:
    """The exact Messages API request for one batch (also shown on the run page for transparency)."""
    profile = model_profile(settings.search_model)
    tool: dict[str, Any] = {"type": profile.search_tool_type, "name": "web_search", "max_uses": group.max_uses}
    if group.allowed_domains:
        tool["allowed_domains"] = list(group.allowed_domains)
    elif group.blocked_domains:
        tool["blocked_domains"] = list(group.blocked_domains)
    location: dict[str, str] = {"type": "approximate"}
    if settings.country:
        location["country"] = settings.country
    if settings.city:
        location["city"] = settings.city
    if settings.timezone:
        location["timezone"] = settings.timezone
    tool["user_location"] = location
    if profile.supports_response_inclusion and settings.search_trim_response:
        tool["response_inclusion"] = "excluded"

    output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}}
    if profile.supports_effort:
        output_config["effort"] = settings.search_effort

    request: dict[str, Any] = {
        "model": settings.search_model,
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "tools": [tool],
        "messages": [
            {
                "role": "user",
                "content": build_user_message(group.to_prompt_dict(), today=today, settings_payload=locale_payload(settings)),
            }
        ],
        "output_config": output_config,
    }
    if profile.supports_adaptive_thinking:
        request["thinking"] = {"type": "adaptive"}
    return request


def extract_queries(content: list[Any]) -> list[str]:
    queries: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "server_tool_use" and getattr(block, "name", "") == "web_search":
            query = (getattr(block, "input", None) or {}).get("query")
            if query:
                queries.append(str(query))
    return queries


def final_text(content: list[Any]) -> str:
    texts = [getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text"]
    for text in reversed(texts):
        if "{" in text:
            return text
    return "\n".join(texts)


def extract_json(text: str) -> Any:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in model output")
    return json.loads(text[start : end + 1])


def parse_group_output(text: str) -> GroupOutput:
    return GroupOutput.model_validate(extract_json(text))


class AnthropicSearchClient:
    name = "anthropic"

    def __init__(self, client: anthropic.Anthropic | None = None) -> None:
        # Credentials: ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / `ant auth login` profile.
        self._client = client or anthropic.Anthropic()

    def _create(self, request: dict[str, Any], settings: Settings) -> Any:
        if settings.llm_fallbacks:
            return self._client.beta.messages.create(**request, betas=[FALLBACK_BETA], fallbacks="default")
        return self._client.messages.create(**request)

    def search_group(self, group: PlannedGroup, settings: Settings, today: date) -> GroupSearchResult:
        result = GroupSearchResult(group_index=group.index, model=settings.search_model)
        request = build_request(group, settings, today)
        user_message = request["messages"][0]
        assistant_content: list[Any] = []
        try:
            response: Any = None
            for _ in range(MAX_CONTINUATIONS + 1):
                response = self._create(request, settings)
                result.usage.add_usage(response.usage)
                result.queries.extend(extract_queries(response.content))
                assistant_content.extend(response.content)
                result.stop_reason = response.stop_reason
                if response.stop_reason != "pause_turn":
                    break
                # Server-side search loop paused: send the assistant turn back unchanged and let it continue.
                request["messages"] = [user_message, {"role": "assistant", "content": list(assistant_content)}]
            else:
                result.error = f"batch still paused after {MAX_CONTINUATIONS} continuations"
                return result

            result.served_by = getattr(response, "model", None)
            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                result.error = "model declined the request" + (f" ({category})" if category else "")
                return result
            if response.stop_reason == "max_tokens":
                result.error = "response cut off by max_tokens"
                return result
            result.raw_text = final_text(assistant_content)
            result.output = parse_group_output(result.raw_text)
        except anthropic.AuthenticationError:
            result.error = "authentication failed - check ANTHROPIC_API_KEY"
        except anthropic.RateLimitError as exc:
            result.error = f"rate limited: {exc.message}"
        except anthropic.BadRequestError as exc:
            result.error = f"bad request: {exc.message}"
        except anthropic.APIStatusError as exc:
            result.error = f"API error {exc.status_code}: {exc.message}"
        except anthropic.APIConnectionError as exc:
            result.error = f"connection error: {exc}"
        except (ValueError, ValidationError) as exc:
            result.error = f"could not parse model output: {exc}"
        if result.error:
            log.warning("batch %s failed: %s", group.index, result.error)
        return result


# --- fake client (tests / demo) ---------------------------------------------------------------

_UNIT_PACKS = {"roll": 8, "tablet": 60, "wash": 40, "capsule": 30, "bag": 10, "ml": 500, "l": 1, "g": 500, "kg": 1, "m": 10, "pair": 1, "piece": 1}
_UNIT_BASE_PRICE = {"roll": 0.85, "tablet": 0.18, "wash": 0.28, "capsule": 0.30, "bag": 0.25, "ml": 0.014, "l": 3.5, "g": 0.02, "kg": 14.0, "m": 2.0, "pair": 8.0, "piece": 9.0}


class FakeSearchClient:
    """Deterministic stand-in: same item -> same 'normal' price, with run-to-run wobble and occasional promos."""

    name = "fake"

    def __init__(self, seed: int = 0, fail_group: int | None = None) -> None:
        self.seed = seed
        self.fail_group = fail_group

    def search_group(self, group: PlannedGroup, settings: Settings, today: date) -> GroupSearchResult:
        result = GroupSearchResult(group_index=group.index, model="fake")
        if self.fail_group == group.index:
            result.error = "simulated batch failure"
            return result
        domains = [r["domain"] for r in group.retailers] or ["example.com"]
        results: list[ItemResult] = []
        for item in group.items:
            rng = random.Random(f"{self.seed}:{item.id}:{today.isoformat()}")
            result.queries.append(f"{item.display_name} {item.search_hints or ''} aanbieding".strip())
            if rng.random() < 0.08:
                results.append(ItemResult(item_id=item.id, status="not_found", note="fake: nothing credible found"))
                continue
            unit = item.unit or "piece"
            pack = _UNIT_PACKS.get(unit, 1)
            normal_unit = _UNIT_BASE_PRICE.get(unit, 9.0) * (0.8 + (item.id % 7) * 0.07)
            offers: list[Offer] = []
            for offer_idx, domain in enumerate(rng.sample(domains, k=min(len(domains), 2))):
                wobble = rng.uniform(0.92, 1.06)
                promo = rng.random() < 0.3
                factor = rng.uniform(0.6, 0.8) if promo else wobble
                price = round(normal_unit * pack * factor, 2)
                offers.append(
                    Offer(
                        retailer_domain=domain,
                        title=f"{item.display_name} {pack} {unit} (fake listing {offer_idx + 1})",
                        url=f"https://www.{domain}/p/fake-{item.id}-{offer_idx}",
                        price=price,
                        currency="EUR",
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
            results.append(ItemResult(item_id=item.id, status="found", note="fake data", offers=offers))
        result.output = GroupOutput(results=results)
        result.usage = UsageTotals(api_requests=1, input_tokens=2500 * len(group.items), output_tokens=350 * len(group.items), web_searches=len(group.items))
        result.stop_reason = "end_turn"
        return result
