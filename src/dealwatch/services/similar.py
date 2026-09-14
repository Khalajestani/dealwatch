"""Near-duplicate detection for items, so 'Page keukenrol' is recognised as the existing 'Page Kitchen towels'."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable

from ..models import Item

SUGGEST_THRESHOLD = 0.45  # show as "similar" while typing / on the review card
DUPLICATE_THRESHOLD = 0.8  # treat as a likely duplicate (needs explicit confirmation)

_STOPWORDS = {"the", "a", "an", "of", "de", "het", "een", "en", "and", "for", "voor", "met", "with", "x", "stuks", "pack", "pak", "st", "per"}
# Dutch -> English product words so names typed in either language match.
_SYNONYMS: dict[str, str] = {
    "keukenrol": "kitchen towels", "keukenrollen": "kitchen towels", "keukenpapier": "kitchen towels",
    "toiletpapier": "toilet paper", "wc papier": "toilet paper", "wc-papier": "toilet paper", "wcpapier": "toilet paper",
    "vaatwastabletten": "dishwasher tablets", "vaatwasmiddel": "dishwasher tablets", "vaatwasser": "dishwasher",
    "wasmiddel": "laundry detergent", "waspods": "laundry pods", "wasverzachter": "fabric softener",
    "afwasmiddel": "dish soap", "allesreiniger": "all-purpose cleaner", "schoonmaakmiddel": "cleaner",
    "tandpasta": "toothpaste", "tandenborstel": "toothbrush", "shampoo": "shampoo", "deodorant": "deodorant",
    "luiers": "diapers", "koffiebonen": "coffee beans", "koffie": "coffee", "thee": "tea",
    "accuboormachine": "cordless drill", "boormachine": "drill", "schroevendraaier": "screwdriver",
    "batterijen": "batteries", "lampen": "bulbs", "zakken": "bags", "vuilniszakken": "bin bags",
}


def normalize(text: str) -> str:
    value = unicodedata.normalize("NFKD", (text or "").lower())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^a-z0-9]+", " ", value).strip()
    for dutch, english in _SYNONYMS.items():
        value = re.sub(rf"\b{re.escape(dutch)}\b", english, value)
    return re.sub(r"\s+", " ", value)


def tokens(text: str) -> set[str]:
    return {t for t in normalize(text).split() if t not in _STOPWORDS}


def similarity(a: str, b: str) -> float:
    """0..1 - token Jaccard, dampened token containment, or character similarity when it is high (typos)."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return round(ratio, 3) if ratio >= 0.75 else 0.0
    inter = len(ta & tb)
    jaccard = inter / len(ta | tb)
    containment = inter / min(len(ta), len(tb))
    score = max(jaccard, 0.9 * containment)
    if ratio >= 0.75:  # near-identical strings (typos, plural forms) even without exact token overlap
        score = max(score, ratio)
    return round(score, 3)


@dataclass
class Match:
    item: Item
    score: float

    @property
    def likely_duplicate(self) -> bool:
        return self.score >= DUPLICATE_THRESHOLD


def find_similar(name: str, brand: str | None, items: Iterable[Item], *, limit: int = 5, threshold: float = SUGGEST_THRESHOLD, exclude_id: int | None = None) -> list[Match]:
    query_full = f"{brand or ''} {name or ''}".strip()
    if not query_full:
        return []
    matches: list[Match] = []
    for item in items:
        if exclude_id is not None and item.id == exclude_id:
            continue
        score = max(similarity(query_full, item.display_name), similarity(name or "", item.name))
        if brand and item.brand and normalize(brand) == normalize(item.brand):
            score = max(score, similarity(name or "", item.name) * 0.5 + 0.5)  # same brand: name similarity counts more
        if score >= threshold:
            matches.append(Match(item=item, score=score))
    matches.sort(key=lambda m: -m.score)
    return matches[:limit]
