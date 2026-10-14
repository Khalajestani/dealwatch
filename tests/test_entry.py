"""Duplicate detection and the smart entry (words, photo or link -> one draft item)."""

import io
import json

import pytest
from conftest import FakeClaude, make_image
from PIL import Image
from sqlalchemy import select

from dealwatch.models import Item
from dealwatch.services.claude import Unavailable
from dealwatch.services.extract import ExtractedItem, draft, find_link, prepare_image
from dealwatch.services.similar import find_similar, normalize, similarity


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


def test_prepare_image_shrinks_to_jpeg():
    jpeg = prepare_image(make_image((3000, 2000), "PNG"))
    with Image.open(io.BytesIO(jpeg)) as img:
        assert img.format == "JPEG" and max(img.size) == 1568
    with pytest.raises(ValueError, match="not a photo"):
        prepare_image(b"not an image")


PAYLOAD = {
    "name": "Toilet paper",
    "brand": "Page",
    "category": "household",
    "priority": "need",
    "search_hints": "toiletpapier",
    "unit": "roll",
    "pack_quantity": 24,
    "target_price": None,
    "target_unit_price": "0.30",
    "notes": None,
    "price": None,
    "retailer_domain": None,
    "product_title": None,
    "confidence": 1.4,
    "summary": "From what they said.",
}


def test_words_become_a_draft_with_targets_and_priority():
    claude = FakeClaude("Here it is:\n" + json.dumps(PAYLOAD))
    item, link = draft(claude, "Page toilet paper 24 rolls, running low, buy under 30 cents a roll")
    assert link is None and claude.calls[0]["web"] is None and claude.calls[0]["photo"] is None
    assert (item.priority, item.target_unit_price, item.confidence) == ("need", 0.30, 1.0)
    form = item.to_draft()
    assert (
        form["search_hints"] == "toiletpapier, 24 roll"
        and form["priority"] == "need"
        and form["target_unit_price"] == 0.3
    )


def test_a_link_in_the_words_is_read_on_that_shop_only():
    claude = FakeClaude(json.dumps(PAYLOAD | {"retailer_domain": None, "price": 5.49}))
    item, link = draft(claude, "this one https://www.bol.com/nl/nl/p/page-toiletpapier/9300.")
    assert link == "https://www.bol.com/nl/nl/p/page-toiletpapier/9300" and item.retailer_domain == "bol.com"
    web = claude.calls[0]["web"]
    assert web.allowed == ["bol.com"] and web.budget == 3 and link in claude.calls[0]["text"]
    assert find_link("see https://bad_domain!/x") is None and find_link("no link") is None


def test_a_photo_goes_with_the_words_and_nothing_found_is_a_sentence():
    claude = FakeClaude(json.dumps(PAYLOAD | {"unit": "bogus", "category": "weird"}), "I cannot tell.")
    item, _ = draft(claude, "", photo=b"jpeg")
    assert claude.calls[0]["photo"] == b"jpeg" and item.unit is None and item.category == "other"
    with pytest.raises(Unavailable, match="Could not tell"):
        draft(claude, "", photo=b"jpeg")


def test_extracted_item_cleans_odd_values():
    item = ExtractedItem.model_validate(
        {"name": "  ", "brand": "null", "pack_quantity": -1, "price": "abc", "confidence": "x", "summary": None}
    )
    assert item.name == "" and item.brand is None and item.pack_quantity is None and item.price is None
    assert item.confidence == 0.5 and item.summary == "" and item.priority == "stock_up"
