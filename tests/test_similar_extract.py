import io
import json
from types import SimpleNamespace

from PIL import Image
from sqlalchemy import select

from dealwatch.models import Item
from dealwatch.services.extract import AnthropicExtractor, ExtractedItem, FakeExtractor, build_image_request, build_url_request, prepare_image
from dealwatch.services.similar import find_similar, normalize, similarity
from dealwatch.settings import Settings


def test_normalize_and_similarity():
    assert normalize("Page Keukenrol XXL") == "page kitchen towels xxl"
    assert similarity("Page keukenrol", "Page Kitchen towels") >= 0.8
    assert similarity("Kitchen towels", "Page Kitchen towels 8 rolls") >= 0.8
    assert similarity("Toilet paper", "Dishwasher tablets") < 0.45
    assert similarity("Kitchen towls", "Kitchen towels") >= 0.75  # typo tolerance
    assert similarity("Garden hose", "Head & Shoulders Shampoo") == 0.0
    assert similarity("", "x") == 0.0


def test_find_similar_ranks_duplicates(session_factory):
    with session_factory() as s:
        items = list(s.scalars(select(Item)))
        matches = find_similar("keukenpapier", "Page", items)
        assert matches and matches[0].item.name == "Kitchen towels" and matches[0].likely_duplicate
        assert find_similar("Garden hose", None, items) == []
        towels = next(i for i in items if i.name == "Kitchen towels")
        assert all(m.item.id != towels.id for m in find_similar("Kitchen towels", "Page", items, exclude_id=towels.id))


def _png_bytes(size=(3000, 2000)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def test_prepare_image_downscales_to_jpeg():
    jpeg, media = prepare_image(_png_bytes())
    assert media == "image/jpeg"
    with Image.open(io.BytesIO(jpeg)) as img:
        assert img.format == "JPEG" and max(img.size) == 1568
    try:
        prepare_image(b"not an image")
    except ValueError as exc:
        assert "unsupported image format" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_fake_extractor_from_url_and_image():
    fake = FakeExtractor()
    res = fake.from_url("https://www.bol.com/nl/nl/p/page-keukenpapier-xxl-8-rollen/930000", Settings())
    assert res.ok and res.item.retailer_domain == "bol.com" and res.item.brand == "Page" and "keukenpapier" in res.item.name.lower()
    assert not fake.from_url("nonsense", Settings()).ok
    res = fake.from_image(_png_bytes((100, 100)), Settings(), filename="dreft-platinum-60.jpg")
    assert res.ok and res.item.brand == "Dreft" and "please review" in res.item.summary
    assert not fake.from_image(b"junk", Settings()).ok


def test_request_shapes():
    req = build_url_request("https://www.ah.nl/producten/product/wi1234/page-keukenrol", Settings())
    tool = req["tools"][0]
    assert tool["type"] == "web_fetch_20260209" and tool["allowed_domains"] == ["ah.nl"] and tool["max_uses"] == 2
    assert req["output_config"]["format"]["schema"]["properties"]["category"]["enum"][0] == "household"
    assert req["output_config"]["effort"] == "medium" and req["thinking"] == {"type": "adaptive"}
    legacy = build_url_request("https://www.ah.nl/x", Settings(search_model="claude-haiku-4-5"))
    assert legacy["tools"][0]["type"] == "web_fetch_20250910" and "thinking" not in legacy
    img = build_image_request(b"\xff\xd8jpegbytes", Settings())
    block = img["messages"][0]["content"][0]
    assert block["type"] == "image" and block["source"]["media_type"] == "image/jpeg" and block["source"]["data"]


class _Stub:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _client(responses):
    messages = _Stub(responses)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages), messages=messages), messages


def _usage():
    return SimpleNamespace(input_tokens=500, output_tokens=80, cache_read_input_tokens=0, cache_creation_input_tokens=0, server_tool_use=SimpleNamespace(web_search_requests=0))


def test_anthropic_extractor_parses_and_validates():
    payload = {"name": "Kitchen towels", "brand": "Page", "category": "household", "search_hints": "keukenpapier XXL 8 rollen", "unit": "roll",
               "pack_quantity": 8, "price": 5.49, "currency": "EUR", "retailer_domain": None, "product_title": "Page Keukenpapier XXL 8 rollen",
               "confidence": 1.4, "summary": "Product page with price."}
    done = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(payload))], usage=_usage(), model="claude-opus-5")
    client, messages = _client([done])
    res = AnthropicExtractor(client).from_url("https://www.bol.com/nl/nl/p/x/1", Settings())
    assert res.ok and res.item.confidence == 1.0 and res.item.retailer_domain == "bol.com" and res.item.pack_quantity == 8
    assert res.usage.api_requests == 1 and messages.calls[0]["tools"][0]["name"] == "web_fetch"

    bad = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="nope")], usage=_usage(), model="m")
    client, _ = _client([bad])
    assert "parse" in AnthropicExtractor(client).from_url("https://www.bol.com/p/1", Settings()).error
    assert "product URL" in AnthropicExtractor(client).from_url("bol.com", Settings()).error

    photo_payload = dict(payload, price=None, currency=None, product_title="Photo of a Page pack", unit="bogus", category="weird")
    done = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(photo_payload))], usage=_usage(), model="m")
    client, messages = _client([done])
    res = AnthropicExtractor(client).from_image(_png_bytes((50, 50)), Settings(), filename="x.png")
    assert res.ok and res.item.unit is None and res.item.category == "other" and res.item.price is None
    assert messages.calls[0]["messages"][0]["content"][0]["type"] == "image"


def test_extracted_item_defaults():
    item = ExtractedItem.model_validate({"name": "  ", "brand": "", "category": "household", "search_hints": None, "unit": None, "pack_quantity": -1,
                                         "price": 0, "currency": None, "retailer_domain": None, "product_title": None, "confidence": 0.2, "summary": None})
    assert item.name == "" and item.brand is None and item.pack_quantity is None and item.price is None and item.summary == ""
