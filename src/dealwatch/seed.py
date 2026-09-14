"""Default retailers (Dutch market) and optional sample items."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Item, Retailer

# name, domain, status, categories ("" = all), notes, sort_order
DEFAULT_RETAILERS: list[tuple[str, str, str, str, str, int]] = [
    ("bol.com", "bol.com", "enabled", "", "Broad range. Marketplace sellers can be pricey - the model is told to prefer bol itself.", 10),
    ("Amazon.nl", "amazon.nl", "enabled", "", "Multipacks and Subscribe & Save.", 20),
    ("Albert Heijn", "ah.nl", "enabled", "household,personal_care,groceries,health,pets_baby", "Bonus offers change weekly (Monday).", 30),
    ("Jumbo", "jumbo.com", "enabled", "household,personal_care,groceries,health,pets_baby", "Weekly offers.", 40),
    ("Kruidvat", "kruidvat.nl", "enabled", "household,personal_care,health,pets_baby", "1+1 gratis and 2e halve prijs weeks are the big wins.", 50),
    ("Etos", "etos.nl", "enabled", "personal_care,health,pets_baby", "", 60),
    ("Drogisterij.net", "drogisterij.net", "enabled", "household,personal_care,health", "Online drugstore, good bulk packs.", 70),
    ("Action", "action.com", "enabled", "household,tools_diy,home_garden,other", "In-store only; website lists prices.", 80),
    ("Lidl", "lidl.nl", "enabled", "household,groceries,tools_diy,home_garden", "Weekly folder; Parkside tools.", 90),
    ("Coolblue", "coolblue.nl", "enabled", "electronics,tools_diy,home_garden", "", 100),
    ("MediaMarkt", "mediamarkt.nl", "enabled", "electronics", "", 110),
    ("Gamma", "gamma.nl", "enabled", "tools_diy,home_garden", "", 120),
    ("Praxis", "praxis.nl", "enabled", "tools_diy,home_garden", "", 130),
    ("Hornbach", "hornbach.nl", "enabled", "tools_diy,home_garden", "", 140),
    ("Toolstation", "toolstation.nl", "enabled", "tools_diy", "", 150),
    ("Tweakers Pricewatch", "tweakers.net", "disabled", "electronics,tools_diy", "Price aggregator - enable to widen electronics coverage.", 200),
    ("Dirk", "dirk.nl", "disabled", "groceries,household,personal_care", "Example of a temporarily disabled shop.", 210),
]

# name, brand, category, hints, unit, bulk_ok, priority, target_price, target_unit_price, notes
SAMPLE_ITEMS: list[tuple[str, str | None, str, str | None, str | None, bool, str, float | None, float | None, str | None]] = [
    ("Kitchen towels", "Page", "household", "keukenpapier, 8+ rollen, XXL", "roll", True, "stock_up", None, 0.70, "Sample item"),
    ("Toilet paper", "Page", "household", "toiletpapier Kussenzacht, 3-laags, 24 rollen", "roll", True, "stock_up", None, 0.30, "Sample item"),
    ("Dishwasher tablets", "Dreft", "household", "Platinum All in One, 60+ tabs", "tablet", True, "stock_up", None, 0.15, "Sample item"),
    ("Laundry detergent pods", "Ariel", "household", "All-in-1 Pods, 40+ wasbeurten", "wash", True, "stock_up", None, 0.22, "Sample item"),
    ("Shampoo", "Head & Shoulders", "personal_care", "Classic Clean, 500 ml of groter", "ml", True, "stock_up", None, 0.012, "Sample item"),
    ("Toothpaste", "Oral-B", "personal_care", "Pro-Expert, 75 ml, multipack ok", "piece", True, "stock_up", 2.50, None, "Sample item"),
    ("Cordless drill", "Bosch", "tools_diy", "Professional GSR 12V-35, 2 accu's", "piece", False, "wishlist", 150.0, None, "Sample wishlist item"),
    ("Espresso beans", "Lavazza", "groceries", "Qualità Rossa, 1 kg", "kg", True, "stock_up", None, 12.0, "Sample item"),
]


def seed_retailers(session: Session) -> int:
    existing = {domain for domain in session.scalars(select(Retailer.domain))}
    added = 0
    for name, domain, status, categories, notes, sort_order in DEFAULT_RETAILERS:
        if domain in existing:
            continue
        session.add(Retailer(name=name, domain=domain, status=status, categories=categories or None, notes=notes or None, sort_order=sort_order))
        added += 1
    session.commit()
    return added


def seed_sample_items(session: Session) -> int:
    existing = {(name, brand) for name, brand in session.execute(select(Item.name, Item.brand))}
    added = 0
    for name, brand, category, hints, unit, bulk_ok, priority, target_price, target_unit_price, notes in SAMPLE_ITEMS:
        if (name, brand) in existing:
            continue
        session.add(
            Item(
                name=name, brand=brand, category=category, search_hints=hints, unit=unit, bulk_ok=bulk_ok,
                priority=priority, target_price=target_price, target_unit_price=target_unit_price, notes=notes,
            )
        )
        added += 1
    session.commit()
    return added
