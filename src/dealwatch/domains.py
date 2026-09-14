"""Domain normalisation and matching helpers shared by retailer config, planner and runner."""

from __future__ import annotations

import re
from urllib.parse import urlparse

_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def normalize_domain(raw: str) -> str:
    """'https://www.Bol.com/nl/' -> 'bol.com'. Returns '' for unusable input."""
    value = (raw or "").strip().lower()
    if not value:
        return ""
    if "://" in value:
        value = urlparse(value).netloc or value
    value = value.split("/")[0].split("?")[0].split("#")[0]
    value = value.split("@")[-1].split(":")[0]
    if value.startswith("www."):
        value = value[4:]
    return value.strip(".")


def is_valid_domain(domain: str) -> bool:
    """ASCII-only bare domains (the API rejects schemes/wildcards; unicode enables homograph tricks)."""
    return bool(domain) and domain.isascii() and bool(_DOMAIN_RE.match(domain))


def domain_of_url(url: str) -> str:
    try:
        return normalize_domain(urlparse(url).netloc)
    except ValueError:
        return ""


def domain_matches(domain: str, pattern: str) -> bool:
    """True when `domain` is `pattern` or a subdomain of it (mirrors the API's allow/block semantics)."""
    domain = normalize_domain(domain)
    pattern = normalize_domain(pattern)
    if not domain or not pattern:
        return False
    return domain == pattern or domain.endswith("." + pattern)
