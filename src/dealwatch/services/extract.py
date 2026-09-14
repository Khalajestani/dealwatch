"""Turn a product link or a product photo into a pre-filled item (name, brand, category, pack, hints).

`AnthropicExtractor` uses Claude with the web fetch tool (links) or vision (photos) and structured JSON output.
`FakeExtractor` derives fields from the URL/filename for tests and fake mode. Photos are downscaled to JPEG first.
"""

from __future__ import annotations

import base64
import io
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ValidationError, field_validator

from ..domains import domain_of_url
from ..models import CATEGORY_KEYS, UNITS
from ..settings import Settings
from .llm import UsageTotals, complete_with_continuations, describe_api_error, extract_json, model_profile

log = logging.getLogger(__name__)

MAX_IMAGE_SIDE = 1568  # Anthropic's recommended long edge
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

EXTRACT_SYSTEM_PROMPT = """You turn a product page or a product photo into an entry for a Dutch household's shopping list.
Answer ONLY with JSON matching the schema.

Fields:
- name: generic product name in English, without the brand (e.g. "Kitchen towels", "Dishwasher tablets", "Cordless drill").
- brand: the brand as printed (e.g. "Page", "Dreft", "Bosch"), or null.
- category: one of household, personal_care, groceries, health, tools_diy, electronics, home_garden, pets_baby, other.
- search_hints: short Dutch keywords that find this exact product and pack in a shop search (variant, pack size, model
  number), e.g. "keukenpapier XXL 8 rollen" or "GSR 12V-35 2 accu's". Null if nothing specific is visible.
- unit: the unit to compare prices per, one of piece, roll, wash, tablet, capsule, bag, ml, l, g, kg, m, pair - or null.
- pack_quantity: how many of that unit are in the pack (8 rolls, 500 ml, 60 tablets) or null if not visible.
- price: the price shown, as a number in EUR, or null. currency: "EUR" or the shown currency, or null.
- retailer_domain: the shop's bare domain if this is a shop page, else null.
- product_title: the product title exactly as displayed, or a short description of what the photo shows.
- confidence: 0-1, how sure you are about name/brand/pack.
- summary: one short English sentence: what you saw and any caveat (e.g. "label partly hidden", "page is a category listing").
If there is no identifiable product, set name to "" and explain in summary. Never invent a brand, pack size or price."""

_nullable_str = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_nullable_num = {"anyOf": [{"type": "number"}, {"type": "null"}]}
EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "brand": _nullable_str,
        "category": {"type": "string", "enum": CATEGORY_KEYS},
        "search_hints": _nullable_str,
        "unit": {"anyOf": [{"type": "string", "enum": UNITS}, {"type": "null"}]},
        "pack_quantity": _nullable_num,
        "price": _nullable_num,
        "currency": _nullable_str,
        "retailer_domain": _nullable_str,
        "product_title": _nullable_str,
        "confidence": {"type": "number"},
        "summary": {"type": "string"},
    },
    "required": ["name", "brand", "category", "search_hints", "unit", "pack_quantity", "price", "currency", "retailer_domain", "product_title", "confidence", "summary"],
    "additionalProperties": False,
}


class ExtractedItem(BaseModel):
    name: str = ""
    brand: str | None = None
    category: str = "other"
    search_hints: str | None = None
    unit: str | None = None
    pack_quantity: float | None = None
    price: float | None = None
    currency: str | None = None
    retailer_domain: str | None = None
    product_title: str | None = None
    confidence: float = 0.5
    summary: str = ""

    @field_validator("brand", "search_hints", "product_title", "currency", mode="before")
    @classmethod
    def _strip_optional(cls, value):
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    @field_validator("name", "summary", mode="before")
    @classmethod
    def _strip_required(cls, value):
        return str(value).strip()[:300] if value is not None else ""

    @field_validator("category")
    @classmethod
    def _category(cls, value):
        return value if value in CATEGORY_KEYS else "other"

    @field_validator("unit")
    @classmethod
    def _unit(cls, value):
        return value if value in UNITS else None

    @field_validator("confidence")
    @classmethod
    def _confidence(cls, value):
        return max(0.0, min(1.0, float(value)))

    @field_validator("pack_quantity", "price")
    @classmethod
    def _positive(cls, value):
        return float(value) if value is not None and value > 0 else None


@dataclass
class ExtractResult:
    source_kind: str  # "url" | "image"
    source_ref: str  # the URL or the uploaded filename
    item: ExtractedItem | None = None
    error: str | None = None
    usage: UsageTotals = field(default_factory=UsageTotals)
    model: str = ""

    @property
    def ok(self) -> bool:
        return self.item is not None and self.error is None


class Extractor(Protocol):
    name: str

    def from_url(self, url: str, settings: Settings) -> ExtractResult: ...

    def from_image(self, data: bytes, settings: Settings, filename: str = "") -> ExtractResult: ...


def prepare_image(data: bytes, max_side: int = MAX_IMAGE_SIDE) -> tuple[bytes, str]:
    """Rotate per EXIF, downscale to the recommended size and re-encode as JPEG. Raises ValueError for non-images."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("image is larger than 20 MB")
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((max_side, max_side))
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=85, optimize=True)
            return buffer.getvalue(), "image/jpeg"
    except UnidentifiedImageError as exc:
        raise ValueError("unsupported image format - use JPEG, PNG or WebP (iPhone HEIC photos need to be shared as JPEG)") from exc


def _base_request(settings: Settings, content: Any, *, max_tokens: int = 4000) -> dict[str, Any]:
    profile = model_profile(settings.search_model)
    request: dict[str, Any] = {
        "model": settings.search_model,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": EXTRACT_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": content}],
        "output_config": {"format": {"type": "json_schema", "schema": EXTRACT_SCHEMA}},
    }
    if profile.supports_effort:
        request["output_config"]["effort"] = "medium"
    if profile.supports_adaptive_thinking:
        request["thinking"] = {"type": "adaptive"}
    return request


def build_url_request(url: str, settings: Settings) -> dict[str, Any]:
    profile = model_profile(settings.search_model)
    domain = domain_of_url(url)
    request = _base_request(settings, f"Read this product page and extract the item for my shopping list: {url}")
    request["tools"] = [{"type": profile.fetch_tool_type, "name": "web_fetch", "max_uses": 2, "allowed_domains": [domain], "max_content_tokens": 20000}]
    return request


def build_image_request(jpeg: bytes, settings: Settings) -> dict[str, Any]:
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.standard_b64encode(jpeg).decode("ascii")}},
        {"type": "text", "text": "Extract the product on this photo (brand, product, variant, pack size) for my shopping list."},
    ]
    return _base_request(settings, content)


class AnthropicExtractor:
    name = "anthropic"

    def __init__(self, client: anthropic.Anthropic | None = None) -> None:
        self._client = client or anthropic.Anthropic()

    def _run(self, result: ExtractResult, request: dict[str, Any], settings: Settings) -> ExtractResult:
        result.model = settings.search_model
        try:
            done = complete_with_continuations(self._client, request, settings)
            result.usage = done.usage
            if done.error:
                result.error = done.error
                return result
            result.item = ExtractedItem.model_validate(extract_json(done.text))
        except (anthropic.APIError, ValueError, ValidationError) as exc:
            result.error = describe_api_error(exc)
        if result.error:
            log.warning("extraction from %s failed: %s", result.source_kind, result.error)
        return result

    def from_url(self, url: str, settings: Settings) -> ExtractResult:
        result = ExtractResult(source_kind="url", source_ref=url)
        if not domain_of_url(url) or not url.lower().startswith(("http://", "https://")):
            result.error = "that does not look like a product URL"
            return result
        self._run(result, build_url_request(url, settings), settings)
        if result.item and not result.item.retailer_domain:
            result.item.retailer_domain = domain_of_url(url)
        return result

    def from_image(self, data: bytes, settings: Settings, filename: str = "") -> ExtractResult:
        result = ExtractResult(source_kind="image", source_ref=filename or "photo")
        try:
            jpeg, _ = prepare_image(data)
        except ValueError as exc:
            result.error = str(exc)
            return result
        return self._run(result, build_image_request(jpeg, settings), settings)


class FakeExtractor:
    """Derives a plausible item from the URL slug / filename. No network, no credits."""

    name = "fake"

    @staticmethod
    def _guess(text: str) -> ExtractedItem:
        words = [w for w in re.split(r"[^a-zA-Z0-9]+", text) if w and not w.isdigit()]
        words = [w for w in words if w.lower() not in {"www", "com", "nl", "p", "product", "producten", "jpg", "jpeg", "png", "webp", "img", "image"}]
        brand = words[0].capitalize() if words else None
        name = " ".join(w.lower() for w in words[1:4]).capitalize() if len(words) > 1 else (words[0].capitalize() if words else "")
        return ExtractedItem(name=name, brand=brand, category="household", search_hints=" ".join(words[1:6]).lower() or None, confidence=0.4, summary="fake extraction (no API key): guessed from the URL/filename - please review")

    def from_url(self, url: str, settings: Settings) -> ExtractResult:
        result = ExtractResult(source_kind="url", source_ref=url, model="fake")
        domain = domain_of_url(url)
        if not domain:
            result.error = "that does not look like a product URL"
            return result
        path = url.split(domain, 1)[-1]
        result.item = self._guess(path)
        result.item.retailer_domain = domain
        return result

    def from_image(self, data: bytes, settings: Settings, filename: str = "") -> ExtractResult:
        result = ExtractResult(source_kind="image", source_ref=filename or "photo", model="fake")
        try:
            prepare_image(data)
        except ValueError as exc:
            result.error = str(exc)
            return result
        result.item = self._guess(filename.rsplit(".", 1)[0] if filename else "Photographed item")
        result.item.summary = "fake extraction (no API key): guessed from the file name - please review"
        return result
