"""Smart entry: words (typed or spoken), a product photo and/or a product link become one pre-filled item.

One Claude run with no tools; when the words hold a product link it may also open that page (and search that shop),
nothing else. The reply is JSON in plain text, checked here. Nothing is stored: the review card shows the draft and
the owner confirms. Photos are rotated, shrunk and re-encoded as JPEG first (`prepare_image`).
"""

from __future__ import annotations

import io
import json
import logging
import re
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ValidationError, field_validator

from ..domains import domain_of_url, is_valid_domain
from ..models import CATEGORY_KEYS, PRIORITY_KEYS, UNITS
from .claude import Claude, Unavailable, WebPolicy

log = logging.getLogger(__name__)

MAX_IMAGE_SIDE = 1568  # as much as the model reads
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
# Reading a brand and pack size off a label or a page is worth the balanced model; low effort keeps it quick.
MODEL, EFFORT = "sonnet", "low"
NOTHING_FOUND = "Could not tell which product this is. Add a few words, or try the photo closer with the label in view."
URL_RE = re.compile(r"https?://[^\s<>\"']+")

SYSTEM = f"""You turn what a person typed or said, a product photo and/or a product page into ONE entry for a Dutch
household's shopping list, which a price watcher then checks at Dutch shops.

Reply with only one JSON object, no other text, with exactly these keys:
"name": generic product name in English, without the brand, e.g. "Kitchen towels", "Dishwasher tablets", "Cordless drill".
"brand": the brand as printed or said, e.g. "Page", "Dreft", "Bosch".
"category": one of {", ".join(CATEGORY_KEYS)}.
"priority": "need" when they are running low or need it soon, "wishlist" for something they would buy only at a great
  price, else "stock_up". One of {", ".join(PRIORITY_KEYS)}.
"search_hints": short Dutch keywords that find this exact product and pack in a shop search (variant, pack size,
  model number), e.g. "keukenpapier XXL 8 rollen" or "GSR 12V-35 2 accu's".
"unit": the unit to compare prices per, one of {", ".join(UNITS)}.
"pack_quantity": how many of that unit are in the pack (8 rolls, 500 ml, 60 tablets).
"target_price": the price per pack in EUR at or under which they want to buy, only if they said one.
"target_unit_price": the price per unit in EUR at or under which they want to buy, only if they said one
  ("under 30 cents a roll" is 0.30).
"notes": anything else they said that matters for buying it, in a few words.
"price": the price shown on the photo or page, in EUR.
"retailer_domain": the shop's bare domain if a shop page or shop label is shown.
"product_title": the product title as displayed, or a short description of what the photo shows.
"confidence": 0 to 1, how sure you are about name, brand and pack.
"summary": one short English sentence on what you saw and any caveat, e.g. "label partly hidden".

Use null for anything not shown or said; never present a guess as a label's text, and never invent a brand, pack
size or price. If there is no identifiable product, set name to "" and say why in summary. Metric units."""


class ExtractedItem(BaseModel):
    name: str = ""
    brand: str | None = None
    category: str = "other"
    priority: str = "stock_up"
    search_hints: str | None = None
    unit: str | None = None
    pack_quantity: float | None = None
    target_price: float | None = None
    target_unit_price: float | None = None
    notes: str | None = None
    price: float | None = None
    retailer_domain: str | None = None
    product_title: str | None = None
    confidence: float = 0.5
    summary: str = ""

    @field_validator("brand", "search_hints", "notes", "product_title", "retailer_domain", mode="before")
    @classmethod
    def _optional_text(cls, value: Any) -> str | None:
        if not isinstance(value, str | int | float) or str(value).strip().lower() in ("", "null", "unknown"):
            return None
        return str(value).strip()[:300]

    @field_validator("name", "summary", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        return str(value).strip()[:200] if value is not None else ""

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, value: Any) -> str:
        return value if value in CATEGORY_KEYS else "other"

    @field_validator("priority", mode="before")
    @classmethod
    def _priority(cls, value: Any) -> str:
        return value if value in PRIORITY_KEYS else "stock_up"

    @field_validator("unit", mode="before")
    @classmethod
    def _unit(cls, value: Any) -> str | None:
        return value if value in UNITS else None

    @field_validator("pack_quantity", "target_price", "target_unit_price", "price", mode="before")
    @classmethod
    def _positive(cls, value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, value: Any) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.5

    def to_draft(self, reference_url: str | None = None) -> dict[str, Any]:
        """The fields of the review card (the same names as the item form)."""
        hints = self.search_hints
        if self.pack_quantity and self.unit and (not hints or self.unit not in hints):
            hints = f"{hints + ', ' if hints else ''}{self.pack_quantity:g} {self.unit}"
        return {
            "name": self.name or (self.product_title or "")[:200],
            "brand": self.brand,
            "category": self.category,
            "priority": self.priority,
            "search_hints": hints,
            "unit": self.unit,
            "target_price": self.target_price,
            "target_unit_price": self.target_unit_price,
            "check_interval_days": None,
            "reference_url": reference_url,
            "notes": self.notes,
            "bulk_ok": True,
        }


def prepare_image(data: bytes, max_side: int = MAX_IMAGE_SIDE) -> bytes:
    """Rotate per EXIF, shrink to what the model reads and re-encode as JPEG. Raises ValueError for non-images."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("The photo is larger than 20 MB.")
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((max_side, max_side))
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=85, optimize=True)
            return buffer.getvalue()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("That file is not a photo this app can read. Use JPEG, PNG or WebP.") from exc


def find_link(text: str) -> str | None:
    """The first product link in the words, if its domain is a plain one."""
    match = URL_RE.search(text or "")
    if not match:
        return None
    url = match.group(0).rstrip(".,;)")
    return url if is_valid_domain(domain_of_url(url)) else None


def parse_reply(text: str) -> ExtractedItem | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        data = json.loads(text[start : end + 1])
        return ExtractedItem.model_validate(data) if isinstance(data, dict) else None
    except (json.JSONDecodeError, ValidationError):
        return None


def draft(claude: Claude, text: str, photo: bytes | None = None) -> tuple[ExtractedItem, str | None]:
    """(the suggested item, the product link it read). Raises Unavailable with the reason in words."""
    link = find_link(text)
    web = WebPolicy(allowed=[domain_of_url(link)], budget=3) if link else None
    words = text.strip() or "(no words, only the photo)"
    prompt = f"Make the shopping-list entry for this. Reply with the JSON object only.\n\nThey said: {words}"
    if link:
        prompt += f"\n\nRead the product page first with WebFetch: {link}"
    reply = claude.ask(prompt, SYSTEM, model=MODEL, effort=EFFORT, photo=photo, web=web, max_turns=6)
    item = parse_reply(reply)
    if item is None or not (item.name or item.product_title):
        log.info("nothing recognised: %r", reply[:200])
        raise Unavailable(NOTHING_FOUND)
    if link and not item.retailer_domain:
        item.retailer_domain = domain_of_url(link)
    return item, link
