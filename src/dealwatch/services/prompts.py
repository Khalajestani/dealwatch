"""Prompt, output schema and validation models for one search batch (group of items).

Keep SYSTEM_PROMPT static (it is cached across calls); anything per-run goes into the user message.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..domains import normalize_domain
from ..models import PROMO_TYPES

SYSTEM_PROMPT = """You are Dealwatch, a price-research assistant for one household in the Netherlands.
You receive a small batch of shopping-list items plus the shops (domains) you may use. Using web search, find the
current price and any promotion for each item at those shops, then answer ONLY with JSON matching the required schema.

How to search (be frugal - every search costs money):
- Start with ONE query per item: product name + brand + the pack hints, in Dutch, e.g. "Page keukenpapier 8 rollen aanbieding".
  Add one more query for an item only when the first gave nothing usable. Stay well within the search budget.
- Prefer retailer product/offer pages. Ignore blogs, forums, price-history sites and expired promo pages.

What to report:
- Only offers you actually saw in search results, with the exact URL of that result. Never invent prices, URLs, pack sizes
  or promotions. If a price is uncertain (old snippet, unclear pack), lower `confidence` and say why in `evidence`.
- `price` is what you pay now for the listed pack, in EUR as a decimal number (4.99). If the listing shows a promo price and
  a regular/"van" price, put the promo price in `price` and the regular price in `list_price`; otherwise `list_price` is null.
- `pack_quantity` = how many of the item's `unit` are in the pack (8 rolls, 1000 ml, 60 tablets, 40 washes). For unit
  "piece" and a single product use 1. Use null when you cannot tell. `unit` echoes the item's unit.
- Multi-buy promos ("1+1 gratis", "2e halve prijs", "3 voor 10"): report the effective price for the TOTAL quantity you get
  (price = total paid, pack_quantity = total units) with promo_type "multibuy" and explain in `promo_text`.
- Household bulk packs (voordeelverpakking, 2-6 packs) are welcome only when the item has bulk_ok = true; never pallets
  or commercial quantities. Report at most 3 offers per item, best value first.
- Only shops from the provided list. Skip marketplace/third-party sellers with unrealistic prices; note it in `note`.
- `promo_type`: one of none, sale, multibuy, bundle, bulk, clearance, member (member = loyalty-card price).
- `valid_until`: ISO date (YYYY-MM-DD) if the promotion end date is shown, else null.
- `evidence`: a short quote or paraphrase of the listing text showing the price and pack (max 200 characters).
- Use status "not_found" with a short `note` when nothing credible was found for an item.
- Write `note` and `promo_text` in English, briefly.
Finish with the JSON only - no commentary before or after it."""

_OFFER_SCHEMA = {
    "type": "object",
    "properties": {
        "retailer_domain": {"type": "string", "description": "Bare domain of the shop, e.g. bol.com"},
        "title": {"type": "string", "description": "Product title as listed"},
        "url": {"type": "string", "description": "URL of the listing, copied from the search result"},
        "price": {"type": "number", "description": "Price you pay now for the listed pack, in EUR"},
        "currency": {"type": "string"},
        "list_price": {"anyOf": [{"type": "number"}, {"type": "null"}], "description": "Regular price if a discount is shown"},
        "pack_quantity": {"anyOf": [{"type": "number"}, {"type": "null"}], "description": "Number of units in the pack"},
        "unit": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "promo_type": {"type": "string", "enum": PROMO_TYPES},
        "promo_text": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "valid_until": {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "YYYY-MM-DD or null"},
        "confidence": {"type": "number", "description": "0.0 - 1.0"},
        "evidence": {"type": "string"},
    },
    "required": [
        "retailer_domain", "title", "url", "price", "currency", "list_price", "pack_quantity", "unit",
        "promo_type", "promo_text", "valid_until", "confidence", "evidence",
    ],
    "additionalProperties": False,
}

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer"},
                    "status": {"type": "string", "enum": ["found", "not_found"]},
                    "note": {"type": "string"},
                    "offers": {"type": "array", "items": _OFFER_SCHEMA},
                },
                "required": ["item_id", "status", "note", "offers"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


class Offer(BaseModel):
    retailer_domain: str
    title: str
    url: str
    price: float
    currency: str = "EUR"
    list_price: float | None = None
    pack_quantity: float | None = None
    unit: str | None = None
    promo_type: str = "none"
    promo_text: str | None = None
    valid_until: str | None = None
    confidence: float = 0.5
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


class GroupOutput(BaseModel):
    results: list[ItemResult] = Field(default_factory=list)


def build_user_message(group_payload: dict, *, today: date, settings_payload: dict) -> str:
    """The per-batch user message. `group_payload` comes from PlannedGroup.to_prompt_dict()."""
    body = {
        "date": today.isoformat(),
        "locale": settings_payload,
        "search_budget": group_payload["max_uses"],
        "shops": group_payload["retailers"],
        "items": group_payload["items"],
    }
    return (
        "Find current prices and promotions for these items. Item fields: `baseline` is the normal price we have seen "
        "before (per `baseline_basis`: per unit or per pack) - a good deal is clearly below it; `target_price` / "
        "`target_unit_price` are prices at which we definitely buy.\n\n"
        + json.dumps(body, ensure_ascii=False, indent=1)
    )
