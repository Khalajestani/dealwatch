"""Direct deal sources (supermarket folders, retailer APIs): cheap, structured, no LLM.

This is the integration point for the planned Albert Heijn / Jumbo / Lidl adapters. A source represents one retailer
domain, receives the active items and returns offers in the same `Offer` shape the LLM produces. The runner stores
them as observations with `source=<name>`, folds them into price points, and assesses them for deals like any
other offer. Sources run on every run for all active items (they are cheap), independent of the LLM due-logic.

To add an adapter: create `services/sources/<shop>.py` with a class implementing `DealSource`, and append an
instance to `SOURCES`. Keep network access inside `fetch`, never in `__init__`. Match products to items by name/
brand/hints (and EAN when the API offers it); return `pack_quantity` + `unit` so per-unit prices work.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from ...models import Retailer
from ...settings import Settings
from ..planner import PlannedItem
from ..prompts import Offer


@dataclass
class SourceOffer:
    item_id: int
    offer: Offer


class DealSource(Protocol):
    name: str  # short id stored in observations.source, e.g. "ah"
    domain: str  # retailer domain, e.g. "ah.nl" - the source only runs when this retailer is enabled

    def fetch(self, items: list[PlannedItem], settings: Settings, today: date) -> list[SourceOffer]: ...


SOURCES: list[DealSource] = []  # register real adapters here (see docs/ROADMAP.md)


def enabled_sources(sources: list[DealSource], retailers: list[Retailer]) -> list[DealSource]:
    status = {r.domain: r.status for r in retailers}
    return [s for s in sources if status.get(s.domain) == "enabled"]


class FakeSource:
    """Deterministic stand-in used by tests and demos: acts like a supermarket folder for a few categories."""

    name = "fake_folder"
    domain = "ah.nl"
    categories = {"household", "personal_care", "groceries"}

    def __init__(self, seed: int = 0, fail: bool = False) -> None:
        self.seed = seed
        self.fail = fail

    def fetch(self, items: list[PlannedItem], settings: Settings, today: date) -> list[SourceOffer]:
        if self.fail:
            raise RuntimeError("simulated source outage")
        offers: list[SourceOffer] = []
        for item in items:
            if item.category not in self.categories:
                continue
            rng = random.Random(f"src:{self.seed}:{item.id}:{today.isoformat()}")
            pack = {"roll": 8, "tablet": 60, "wash": 40, "ml": 500, "kg": 1, "piece": 1}.get(item.unit or "piece", 1)
            price = round(rng.uniform(3.0, 9.0), 2)
            promo = rng.random() < 0.4
            offers.append(
                SourceOffer(
                    item_id=item.id,
                    offer=Offer(
                        retailer_domain=self.domain, title=f"{item.display_name} (folder)", url=f"https://www.{self.domain}/producten/fake-{item.id}",
                        price=price, list_price=round(price * 1.5, 2) if promo else None, pack_quantity=pack, unit=item.unit,
                        promo_type="multibuy" if promo else "none", promo_text="1+1 gratis (fake)" if promo else None,
                        confidence=0.95, evidence="fake folder entry",
                    ),
                )
            )
        return offers
