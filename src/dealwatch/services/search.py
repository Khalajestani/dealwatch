"""The price search: one Claude Code run per batch of items, with web search held to the batch's shops.

Contract: `SearchClient.search_group(group, settings, today) -> GroupSearchResult`. `ClaudeSearchClient` is the real
one. Claude reports each item through the `record_item` tool, whose arguments are checked like a form: a wrong
field comes back to the model as an error to fix, and whatever was recorded is kept even if the run is cut short.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Protocol

from pydantic import BaseModel, Field, field_validator

from ..domains import normalize_domain
from ..models import PROMO_TYPES
from ..settings import Settings
from .claude import CannotRun, Claude, Unavailable, WebPolicy
from .planner import PlannedGroup
from .tool import tool

log = logging.getLogger(__name__)
BATCH_TIMEOUT_S = 600  # a batch of six items with a few searches each takes one to three minutes on the Pi

# Static, so it reads the same for every batch; what changes per batch is in the message.
SYSTEM_PROMPT = """You are Dealwatch, a price-research assistant for one household in the Netherlands.
You receive a small batch of shopping-list items plus the shops (domains) you may use. Find the current price and
any promotion for each item at those shops with WebSearch, and open a product page with WebFetch only when the search
result does not show the price or the pack size. Then call record_item once for every item in the batch.

How to search (the app enforces the shops and a budget of web lookups, so be frugal):
- Start with ONE query per item: product name + brand + the pack hints, in Dutch, e.g. "Page keukenpapier 8 rollen
  aanbieding". Add one more query for an item only when the first gave nothing usable.
- Prefer retailer product and offer pages. Ignore blogs, forums, price-history sites and expired promotion pages.

What to record:
- Only offers you actually saw, with the exact URL of that page or search result. Never invent prices, URLs, pack
  sizes or promotions. If a price is uncertain (old snippet, unclear pack), lower confidence and say why in evidence.
- price is what you pay now for the listed pack, in EUR as a decimal number (4.99). If the listing shows a promotion
  price and a regular ("van") price, put the promotion price in price and the regular price in list_price.
- pack_quantity: how many of the item's unit are in the pack (8 rolls, 1000 ml, 60 tablets, 40 washes). For unit
  "piece" and a single product use 1. Null when you cannot tell. unit echoes the item's unit.
- Multi-buy promotions ("1+1 gratis", "2e halve prijs", "3 voor 10"): record the effective price for the TOTAL
  quantity you get (price = total paid, pack_quantity = total units) with promo_type "multibuy", explained in promo_text.
- Household bulk packs (voordeelverpakking, 2 to 6 packs) only when the item has bulk_ok true; never pallets or
  commercial quantities. At most 3 offers per item, best value first.
- Only shops from the list. Skip marketplace or third-party sellers with unrealistic prices; say so in note.
- promo_type: none, sale, multibuy, bundle, bulk, clearance or member (member = loyalty-card price).
- valid_until: the promotion's end date as YYYY-MM-DD when shown, else null.
- evidence: a short quote or paraphrase of the listing showing price and pack (at most 200 characters).
- Nothing credible found: record_item with status "not_found" and a short note.
- note and promo_text in English, briefly.
When every item is recorded, reply with one short line and stop."""


class Offer(BaseModel):
    """One price seen at one shop."""

    retailer_domain: str = Field(description="Bare domain of the shop, e.g. bol.com")
    title: str = Field(description="Product title as listed")
    url: str = Field(description="URL of the listing")
    price: float = Field(description="Price you pay now for the listed pack, in EUR")
    currency: str = "EUR"
    list_price: float | None = Field(None, description="Regular price, when a discount is shown")
    pack_quantity: float | None = Field(None, description="Number of the item's unit in the pack")
    unit: str | None = None
    promo_type: str = Field("none", description=", ".join(PROMO_TYPES))
    promo_text: str | None = None
    valid_until: str | None = Field(None, description="YYYY-MM-DD or null")
    confidence: float = Field(0.5, description="0.0 to 1.0")
    evidence: str = ""

    @field_validator("retailer_domain")
    @classmethod
    def _norm_domain(cls, value: str) -> str:
        return normalize_domain(value)

    @field_validator("currency")
    @classmethod
    def _norm_currency(cls, value: str) -> str:
        return (value or "EUR").strip().upper()[:10] or "EUR"

    @field_validator("promo_type")
    @classmethod
    def _norm_promo(cls, value: str) -> str:
        value = (value or "none").strip().lower()
        return value if value in PROMO_TYPES else "none"

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    @field_validator("pack_quantity", "list_price")
    @classmethod
    def _positive_or_none(cls, value: float | None) -> float | None:
        if value is None or value <= 0:
            return None
        return float(value)

    @property
    def valid_until_date(self) -> date | None:
        if not self.valid_until:
            return None
        try:
            return date.fromisoformat(self.valid_until.strip()[:10])
        except ValueError:
            return None


class ItemResult(BaseModel):
    item_id: int
    status: Literal["found", "not_found"] = "found"
    note: str = ""
    offers: list[Offer] = Field(default_factory=list)


@dataclass
class GroupSearchResult:
    group_index: int
    model: str
    results: list[ItemResult] = field(default_factory=list)
    lookups: list[str] = field(default_factory=list)
    error: str | None = None
    stop: bool = False  # nothing more can run now (plan limit, sign-in): the check stops here


class SearchClient(Protocol):
    def search_group(self, group: PlannedGroup, settings: Settings, today: date) -> GroupSearchResult: ...


def build_user_message(group: PlannedGroup, settings: Settings, today: date) -> str:
    body = {
        "date": today.isoformat(),
        "locale": {"country": settings.country, "city": settings.city, "currency": settings.currency, "language": "nl"},
        "web_lookup_budget": group.max_uses,
        "shops": group.retailers,
        "items": [item.to_prompt_dict() for item in group.items],
    }
    return (
        "Find current prices and promotions for these items. Item fields: baseline is the normal price we have seen "
        "before (per baseline_basis: per unit or per pack), a good deal is clearly below it; target_price and "
        "target_unit_price are prices at which we definitely buy.\n\n" + json.dumps(body, ensure_ascii=False, indent=1)
    )


class ClaudeSearchClient:
    def __init__(self, claude: Claude) -> None:
        self.claude = claude

    def search_group(self, group: PlannedGroup, settings: Settings, today: date) -> GroupSearchResult:
        recorded: dict[int, ItemResult] = {}
        ids = {item.id for item in group.items}

        @tool
        def record_item(
            item_id: int, status: Literal["found", "not_found"], note: str = "", offers: list[Offer] | None = None
        ) -> str:
            """Record what you found for one item of the batch. Call it once per item; a second call replaces the first.

            Args:
                item_id: The item's item_id from the batch.
                status: found when at least one credible offer is recorded, else not_found.
                note: One short English line: what you found, or why nothing.
                offers: Up to 3 offers, best value first. Empty for not_found.
            """
            if item_id not in ids:
                raise ValueError(f"item {item_id} is not in this batch; use one of {sorted(ids)}")
            recorded[item_id] = ItemResult(item_id=item_id, status=status, note=note, offers=offers or [])
            left = sorted(ids - recorded.keys())
            return f"Recorded item {item_id}. " + (f"Still to record: {left}." if left else "All recorded; finish now.")

        web = WebPolicy(allowed=group.allowed_domains, blocked=group.blocked_domains, budget=group.max_uses)
        result = GroupSearchResult(group_index=group.index, model=settings.search_model)
        try:
            self.claude.ask(
                build_user_message(group, settings, today),
                SYSTEM_PROMPT,
                model=settings.search_model,
                effort=settings.search_effort,
                tools=[record_item],
                web=web,
                timeout=BATCH_TIMEOUT_S,
                max_turns=group.max_uses + 2 * len(group.items) + 4,
            )
        except CannotRun as exc:
            result.error, result.stop = str(exc), True
        except Unavailable as exc:
            result.error = str(exc)
        if result.error:
            log.warning("batch %s: %s", group.index, result.error)
        result.results = list(recorded.values())
        result.lookups = web.lookups
        return result
