"""Shared monitoring logic for the PlayStation gift card alert bot.

This module is the single source of truth used by both:

* ``github_runner.py`` -- a one-shot run used by GitHub Actions, and
* ``bot.py`` -- an optional long-running local process.

Design rules that must not be relaxed:

1. ``UNKNOWN`` never becomes ``OUT_OF_STOCK``. A missing selector, a missing
   variant, a parse failure or a network error all mean "we do not know", never
   "it is out of stock".
2. The first successful observation of a product establishes a baseline and
   raises **no** alerts. This prevents a burst of false alerts on the very first
   run and is what makes duplicate suppression reliable afterwards.
3. The Telegram token is read only from the ``TELEGRAM_BOT_TOKEN`` environment
   variable and is never written to logs, state or exceptions.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup

log = logging.getLogger("psn-alert")

# --------------------------------------------------------------------------- #
# Status vocabulary
# --------------------------------------------------------------------------- #
IN_STOCK = "IN_STOCK"
OUT_OF_STOCK = "OUT_OF_STOCK"
UNKNOWN = "UNKNOWN"
# A product with no state entry (or no usable reading) has not been checked.
# It is a fourth, distinct category: never merged into OUT_OF_STOCK.
NOT_CHECKED = "NOT_CHECKED"

#: Every status a report may display. Anything else is reported as
#: :data:`NOT_CHECKED` so the report totals always reconcile.
KNOWN_STATUSES = frozenset({IN_STOCK, OUT_OF_STOCK, UNKNOWN, NOT_CHECKED})

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
DEFAULT_CONFIG_PATH = PACKAGE_DIR / "config" / "products.yaml"
DEFAULT_STATE_PATH = REPO_ROOT / "state" / "state.json"

DEFAULT_TIMEZONE = "Asia/Kolkata"
DEFAULT_REPORT_HOUR = 9

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}

TELEGRAM_API = "https://api.telegram.org"
# Telegram's hard per-message limit is 4096 UTF-16 code units (what the API
# counts -- emoji and other astral characters weigh two units each).
TELEGRAM_MESSAGE_LIMIT = 4096
# Split a little below the hard limit so headers or escaping can never push a
# part over the edge.
TELEGRAM_SPLIT_LIMIT = 4000
# Markdown emphasis runs that must not be cut in half (HTML tags are handled
# separately by tag scanning in _safe_cut).
_FORMAT_MARKERS = ("**", "__", "~~", "||")
# Bound the amount of history kept in the committed state file.
MAX_EVENTS = 100


class ExtractionError(RuntimeError):
    """Raised when a page could not be parsed. Never implies out of stock."""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def get_timezone(name: str | None = None) -> ZoneInfo:
    return ZoneInfo(name or os.getenv("TIMEZONE", DEFAULT_TIMEZONE))


def now_iso(tz: ZoneInfo | None = None) -> str:
    return datetime.now(tz or get_timezone()).isoformat(timespec="seconds")


def parse_money(text: str | None) -> float | None:
    """Extract an INR amount from free text. Returns None when unparseable."""
    if not text:
        return None
    cleaned = str(text).replace(",", "")
    match = re.search(
        r"(?:₹|Rs\.?|INR)\s*([0-9]+(?:\.[0-9]{1,2})?)"
        r"|([0-9]+(?:\.[0-9]{1,2})?)\s*(?:₹|INR)",
        cleaned,
        re.I,
    )
    if not match:
        return None
    try:
        return float(match.group(1) or match.group(2))
    except (TypeError, ValueError):
        return None


def product_label(product: dict[str, Any]) -> str:
    return f'{product.get("retailer", "?")} PSN ₹{product.get("denomination", "?")}'


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def load_products(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Load product entries from YAML. Returns [] when the file is absent."""
    cfg = Path(path) if path else Path(
        os.getenv("PRODUCTS_PATH", str(DEFAULT_CONFIG_PATH))
    )
    if not cfg.exists():
        log.warning("Product config not found: %s", cfg)
        return []
    with cfg.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    products = data.get("products") or []
    if not isinstance(products, list):
        raise ValueError("products.yaml must contain a top-level 'products' list")
    return products


def enabled_products(products: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [p for p in products if p.get("enabled")]


def enable_candidates(products: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Disabled entries that still carry a URL and can therefore be re-checked.

    These are configured-but-unverified products (see products.yaml): the
    data path exists, but the entry was never proven readable, so it stays
    off the monitoring loop. A URL is required -- an entry with no URL has
    nothing to probe and no way to ever become verifiable.
    """
    return [p for p in products if not p.get("enabled") and p.get("url")]


# --------------------------------------------------------------------------- #
# Persistent state
# --------------------------------------------------------------------------- #
def state_path() -> Path:
    override = os.getenv("STATE_PATH", "").strip()
    return Path(override) if override else DEFAULT_STATE_PATH


def default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "last_daily_report_date": "",
        "products": {},
        "events": [],
    }


def load_state(path: str | Path | None = None) -> dict[str, Any]:
    """Load state. A missing or corrupt file yields a clean default state.

    A corrupt state file must never crash the run and must never be mistaken
    for 'everything is out of stock' -- it simply resets the baseline.
    """
    target = Path(path) if path else state_path()
    if not target.exists():
        return default_state()
    try:
        with target.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        log.warning("State file %s unreadable (%s); starting fresh", target.name, exc)
        return default_state()
    if not isinstance(data, dict):
        log.warning("State file %s is not an object; starting fresh", target.name)
        return default_state()

    base = default_state()
    # Only known keys survive: legacy files that still carry a "chat_id"
    # have it dropped here (the chat id lives in TELEGRAM_CHAT_ID, never in
    # state), while products and event history are preserved verbatim.
    base.update({k: v for k, v in data.items() if k in base})
    if not isinstance(base["products"], dict):
        base["products"] = {}
    if not isinstance(base["events"], list):
        base["events"] = []
    return base


def save_state(state: dict[str, Any], path: str | Path | None = None) -> Path:
    """Atomically write state so a crashed run cannot corrupt the file.

    The chat id is configuration, never state: it is stripped from the
    payload on every write so it can never reach the committed state file,
    even if a caller set it in the dict.
    """
    target = Path(path) if path else state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    events = state.get("events", [])
    if len(events) > MAX_EVENTS:
        state["events"] = events[-MAX_EVENTS:]
    persistable = {k: v for k, v in state.items() if k != "chat_id"}
    payload = json.dumps(persistable, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(target)
    return target


def record_event(state: dict[str, Any], product_id: str, event_type: str,
                 message: str, ts: str) -> None:
    state.setdefault("events", []).append(
        {"at": ts, "product_id": product_id, "type": event_type, "message": message}
    )
    if len(state["events"]) > MAX_EVENTS:
        state["events"] = state["events"][-MAX_EVENTS:]


# --------------------------------------------------------------------------- #
# Next.js React Server Components payload helpers
# --------------------------------------------------------------------------- #
def rsc_payload(html: str) -> str:
    """Concatenate Next.js App Router flight payloads from raw HTML.

    Retailers such as Rooter render with Next.js. The product data is not in
    the visible DOM but is shipped to the browser inside
    ``self.__next_f.push([1, "..."])`` script tags. This is ordinary HTML the
    server already sends to every visitor -- it requires no special access.
    """
    parts: list[str] = []
    for match in re.finditer(
        r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html
    ):
        try:
            parts.append(json.loads('"' + match.group(1) + '"'))
        except json.JSONDecodeError:
            continue
    return "".join(parts)


def extract_json_value(text: str, start: int) -> tuple[str, int]:
    """Return the JSON object/array starting at ``text[start]`` and its end index.

    String-aware brace matching, so braces inside string values do not confuse
    the scan.
    """
    if start >= len(text) or text[start] not in "{[":
        raise ExtractionError("expected '{' or '[' at extraction point")
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == open_ch:
            depth += 1
        elif char == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:index + 1], index + 1
    raise ExtractionError("unbalanced JSON while extracting product data")


def find_rooter_product_data(html: str) -> dict[str, Any]:
    """Locate and parse the ``productData`` object from a Rooter page."""
    payload = rsc_payload(html)
    marker = '"productData":'
    idx = payload.find(marker)
    if idx < 0:
        # Fall back to the unquoted form used by some flight encodings.
        idx = payload.find("productData:")
        if idx < 0:
            raise ExtractionError("productData not present in page payload")
        brace = payload.index("{", idx + len("productData:"))
    else:
        brace = payload.index("{", idx + len(marker))
    raw, _ = extract_json_value(payload, brace)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"productData is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ExtractionError("productData is not an object")
    return data


# --------------------------------------------------------------------------- #
# Availability helpers
# --------------------------------------------------------------------------- #
def best_price(sellers: list[dict[str, Any]]) -> float | None:
    """Lowest price among in-stock sellers, else lowest advertised price.

    Preferring a purchasable seller avoids reporting a price that cannot
    actually be paid.
    """
    prices = []
    for seller in sellers or []:
        value = seller.get("sellPrice")
        if isinstance(value, (int, float)):
            prices.append((bool(seller.get("outOfStock")), float(value)))
    if not prices:
        return None
    in_stock = [p for p in prices if not p[0]]
    pool = in_stock or prices
    return min(p[1] for p in pool)


def cashback_text(*groups: Any) -> str:
    """Flatten any cashback structures found on a variation or seller."""
    seen: list[str] = []

    def walk(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, dict):
            label = node.get("label") or node.get("title") or node.get("text") or node.get("name")
            amount = node.get("amount") or node.get("value")
            if label and amount:
                seen.append(f"{label}: {amount}")
            elif label:
                seen.append(str(label))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str) and node.strip():
            seen.append(node.strip())

    for group in groups:
        walk(group)
    ordered: list[str] = []
    for item in seen:
        if item and item not in ordered:
            ordered.append(item)
    return "; ".join(ordered)[:250]


def extract_rooter(html: str, product: dict[str, Any]) -> dict[str, Any]:
    """Extract one denomination from a Rooter product page.

    Returns an observation dict. A missing variant is ``UNKNOWN``, never
    ``OUT_OF_STOCK``.
    """
    data = find_rooter_product_data(html)
    pattern = product.get("variant_pattern")
    if not pattern:
        denomination = product.get("denomination")
        pattern = rf"Rs\.\s*{denomination}\b" if denomination else None

    variation = None
    if pattern:
        regex = re.compile(str(pattern), re.I)
        for candidate in data.get("baseVariations") or []:
            name = str(candidate.get("name") or "")
            if regex.search(name):
                variation = candidate
                break
    if variation is None:
        return {
            "ok": True,
            "status": UNKNOWN,
            "price": None,
            "discount": "",
            "cashback": "",
            "error": "",
            "note": "denomination variant not found on page",
        }

    out_of_stock = variation.get("outOfStock")
    if out_of_stock is None:
        status = UNKNOWN
    else:
        status = OUT_OF_STOCK if bool(out_of_stock) else IN_STOCK

    sellers = variation.get("sellers") or []
    price = best_price(sellers)
    if price is None:
        price = best_price([variation]) if isinstance(variation.get("sellPrice"), (int, float)) else None

    cashback = cashback_text(variation.get("cashback"), [s.get("cashback") for s in sellers])

    discount_parts: list[str] = []
    pro_price = None
    for seller in sellers:
        value = seller.get("proSellPrice")
        sell = seller.get("sellPrice")
        if isinstance(value, (int, float)) and isinstance(sell, (int, float)) and value < sell:
            pro_price = value
    if pro_price is not None and price is not None and pro_price < price:
        discount_parts.append(f"Pro price ₹{pro_price:,.2f}")
    if data.get("coinsOffer"):
        discount_parts.append(str(data["coinsOffer"])[:120])

    return {
        "ok": True,
        "status": status,
        "price": price,
        "discount": "; ".join(discount_parts)[:250],
        "cashback": cashback,
        "error": "",
        "note": "",
    }


def selected_text(soup: BeautifulSoup, selector: str | None) -> str:
    if not selector:
        return ""
    element = soup.select_one(selector)
    return element.get_text(" ", strip=True) if element else ""


def status_from_classes(element: Any, product: dict[str, Any]) -> str | None:
    """Read availability off the element's CSS classes.

    Some stores (WooCommerce among them) put the real signal in the class list
    -- ``class="stock out-of-stock"`` -- while the visible text is free-form
    and changes per product ("Delivery By: 10/11 - 18/11"). Matching that text
    is guesswork, so when class lists are configured they take priority.

    Returns ``None`` when nothing is configured or nothing matched, which tells
    the caller to fall back to the text patterns.
    """
    if element is None:
        return None
    out_classes = {str(c).lower() for c in product.get("out_of_stock_classes") or []}
    in_classes = {str(c).lower() for c in product.get("in_stock_classes") or []}
    if not out_classes and not in_classes:
        return None

    present = {str(c).lower() for c in element.get("class") or []}
    out_hit = bool(out_classes & present)
    in_hit = bool(in_classes & present)

    if out_hit and not in_hit:
        return OUT_OF_STOCK
    if in_hit and not out_hit:
        return IN_STOCK
    if out_hit and in_hit:
        # Contradictory signal. Guessing either way could invent a restock or
        # hide one, so report that we could not tell.
        return UNKNOWN
    return None


def extract_html(html: str, product: dict[str, Any]) -> dict[str, Any]:
    """Generic CSS-selector extractor for server-rendered pages.

    This is the path for retailers whose product data is present in the HTML.
    If an ``availability_selector`` is configured but matches nothing, the
    result is ``UNKNOWN`` -- the page-wide fallback is deliberately not used,
    because unrelated "out of stock" text elsewhere on the page would produce a
    false negative.
    """
    soup = BeautifulSoup(html, "html.parser")
    selector = product.get("availability_selector") or ""

    element = None
    if selector:
        element = soup.select_one(selector)
        if element is None:
            return {
                "ok": True, "status": UNKNOWN, "price": None, "discount": "",
                "cashback": "", "error": "",
                "note": f"availability selector matched nothing: {selector}",
            }
        haystack = element.get_text(" ", strip=True).lower()
    else:
        haystack = soup.get_text(" ", strip=True).lower()

    status = status_from_classes(element, product)
    if status is None:
        out_patterns = [str(p).lower() for p in product.get("out_of_stock_patterns") or []]
        in_patterns = [str(p).lower() for p in product.get("in_stock_patterns") or []]

        if any(pattern in haystack for pattern in out_patterns):
            status = OUT_OF_STOCK
        elif any(pattern in haystack for pattern in in_patterns):
            status = IN_STOCK
        else:
            status = UNKNOWN

    price_text = selected_text(soup, product.get("price_selector"))
    price = parse_money(price_text)
    if price is None and not product.get("price_selector"):
        price = parse_money(haystack)

    return {
        "ok": True,
        "status": status,
        "price": price,
        "discount": selected_text(soup, product.get("discount_selector"))[:250],
        "cashback": selected_text(soup, product.get("cashback_selector"))[:250],
        "error": "",
        "note": "",
    }


def collect_products(node: Any, out: list[dict[str, Any]]) -> None:
    """Gather every schema.org Product node from a parsed JSON-LD tree.

    Two shapes are in the wild: a Product sitting at the top level, and the
    ``@graph`` shape emitted by WordPress SEO plugins where the Product is a
    sibling of BreadcrumbList/Organization/WebSite nodes. Only reading the
    top level silently finds nothing on the second kind.
    """
    if isinstance(node, dict):
        if node.get("@type") == "Product":
            out.append(node)
        for value in node.values():
            collect_products(value, out)
    elif isinstance(node, list):
        for item in node:
            collect_products(item, out)


def iter_jsonld_offers(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Collect every schema.org Offer published in the page's JSON-LD.

    Sites publish this specifically so machines can read price and
    availability, and unlike the visible card text the vocabulary is fixed
    (``schema.org/InStock`` / ``schema.org/OutOfStock``). Handles the three
    offer shapes seen in the wild: a bare offer, a list, and an AggregateOffer
    wrapping a nested list -- and the ``@graph`` document shape above.
    """
    offers: list[dict[str, Any]] = []
    for block in soup.find_all("script", type="application/ld+json"):
        raw = block.string or block.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue

        products: list[dict[str, Any]] = []
        collect_products(data, products)

        for item in products:
            nested = item.get("offers")
            if isinstance(nested, dict):
                inner = nested.get("offers")
                candidates = inner if isinstance(inner, list) else [nested]
            elif isinstance(nested, list):
                candidates = nested
            else:
                candidates = []
            offers.extend(c for c in candidates if isinstance(c, dict))
    return offers


def extract_jsonld(html: str, product: dict[str, Any]) -> dict[str, Any]:
    """Read one denomination from schema.org JSON-LD offers.

    Used by stores that publish per-denomination offers with an explicit
    availability URL. A missing offer is ``UNKNOWN``, never ``OUT_OF_STOCK``:
    "not listed" and "not in stock" are different facts.
    """
    soup = BeautifulSoup(html, "html.parser")
    denomination = product.get("denomination")
    offers = iter_jsonld_offers(soup)

    if not offers:
        return {
            "ok": True, "status": UNKNOWN, "price": None, "discount": "",
            "cashback": "", "error": "",
            "note": "no schema.org offers found in page JSON-LD",
        }

    match: dict[str, Any] | None = None
    for offer in offers:
        raw_price = offer.get("price")
        if raw_price is None or denomination is None:
            continue
        try:
            if float(raw_price) == float(denomination):  # type: ignore[arg-type]
                match = offer
                break
        except (TypeError, ValueError):
            continue

    if match is None:
        return {
            "ok": True, "status": UNKNOWN, "price": None, "discount": "",
            "cashback": "", "error": "",
            "note": f"denomination {denomination} not among published offers",
        }

    availability = str(match.get("availability") or "")
    if "OutOfStock" in availability:
        status = OUT_OF_STOCK
    elif "InStock" in availability:
        status = IN_STOCK
    else:
        # PreOrder, BackOrder, Discontinued, or an absent field. None of those
        # mean "buy it now", and none mean "sold out" either.
        status = UNKNOWN

    price: float | None = None
    try:
        price = float(match.get("price"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        price = None

    # The visible cards carry the offer wording that JSON-LD does not: the
    # cashback percentage and any coin bonus. Scope the scan to the card that
    # names this denomination so another value's offer is never attributed here.
    cashback = ""
    discount = ""
    needle = f"Coupon Worth ₹{denomination}" if denomination is not None else None
    if needle:
        for anchor in soup.find_all("a"):
            text = anchor.get_text(" ", strip=True)
            if needle not in text:
                continue
            cb = re.search(r"(\d+)\s*%\s*cashback", text, re.I)
            if cb:
                cashback = f"{cb.group(1)}% cashback"
            coins = re.search(r"(Get Upto [\d,]+ Coins)", text, re.I)
            if coins:
                discount = coins.group(1)
            break

    return {
        "ok": True,
        "status": status,
        "price": price,
        "discount": discount[:250],
        "cashback": cashback[:250],
        "error": "",
        "note": "",
    }


def _gyftr_denomination(candidate: dict[str, Any]) -> float | None:
    """The denomination a GYFTR product row represents, if it can be read.

    The feed publishes ``mrp`` (e.g. ``1000``); the name is a fallback for
    rows where it is missing ("Sony Playstation Rs - 1000").
    """
    try:
        return float(candidate.get("mrp"))
    except (TypeError, ValueError):
        pass
    name = str(candidate.get("product_name") or "")
    # Prefer the denomination the name states ("Sony Playstation Rs - 1000")
    # over any other number that happens to appear in it.
    digits = re.search(r"(?i)rs\D*(\d[\d,]*)", name) or re.search(r"(\d[\d,]*)", name)
    if digits:
        try:
            return float(digits.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


def extract_gyftr(html: str, product: dict[str, Any]) -> dict[str, Any]:
    """Read one denomination from GYFTR's public brand-detail JSON feed.

    GYFTR's brand pages are empty shells: denominations, prices and a
    per-product ``stock_left`` count are served by the site's own
    unauthenticated JSON endpoint, which every visitor's browser calls. The
    stock count is an explicit reading, so:

    * ``stock_left > 0`` -> ``IN_STOCK``
    * ``stock_left == 0`` -> ``OUT_OF_STOCK`` (the feed keeps sold-out rows
      listed, so zero is a real count, not an absence)
    * the brand publishes no products, or this denomination is not among
      them -> ``UNKNOWN``: "not listed" and "not in stock" are different
      facts, exactly as in :func:`extract_jsonld`.

    A response that is not the expected JSON shape raises
    :class:`ExtractionError`, so a blocked or mangled reply preserves the
    last known status instead of overwriting it.
    """
    try:
        payload = json.loads(html)
    except (ValueError, TypeError) as exc:
        raise ExtractionError(f"brand detail response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("code") != 200:
        code = payload.get("code") if isinstance(payload, dict) else type(payload).__name__
        raise ExtractionError(f"unexpected brand detail payload (code={code!r})")
    data = payload.get("data")
    candidates = data.get("products") if isinstance(data, dict) else None
    if not isinstance(candidates, list):
        raise ExtractionError("brand detail payload has no product list")

    if not candidates:
        return {
            "ok": True, "status": UNKNOWN, "price": None, "discount": "",
            "cashback": "", "error": "",
            "note": "GYFTR currently publishes no products for this brand",
        }

    denomination = product.get("denomination")
    wanted: float | None = None
    try:
        wanted = float(denomination) if denomination is not None else None
    except (TypeError, ValueError):
        wanted = None
    if wanted is None:
        return {
            "ok": True, "status": UNKNOWN, "price": None, "discount": "",
            "cashback": "", "error": "",
            "note": "no usable denomination configured for this entry",
        }

    match: dict[str, Any] | None = None
    for candidate in candidates:
        if isinstance(candidate, dict) and _gyftr_denomination(candidate) == wanted:
            match = candidate
            break
    if match is None:
        return {
            "ok": True, "status": UNKNOWN, "price": None, "discount": "",
            "cashback": "", "error": "",
            "note": f"denomination {denomination} not among GYFTR's published products",
        }

    try:
        stock = float(match.get("stock_left"))
    except (TypeError, ValueError):
        return {
            "ok": True, "status": UNKNOWN, "price": None, "discount": "",
            "cashback": "", "error": "",
            "note": "GYFTR published no stock count for this product",
        }

    price = _gyftr_denomination(match)
    return {
        "ok": True,
        "status": IN_STOCK if stock > 0 else OUT_OF_STOCK,
        "price": price,
        "discount": "",
        "cashback": "",
        "error": "",
        "note": "",
    }


# --------------------------------------------------------------------------- #
# Woohoo
# --------------------------------------------------------------------------- #
def woohoo_initial_state(html: str) -> dict[str, Any]:
    """Parse ``window.__INITIAL_STATE`` out of a server-rendered Woohoo page.

    Woohoo pre-renders its product pages and embeds the whole Redux state as
    a single JSON assignment. The payload contains strings (terms HTML) that
    themselves hold braces, so the object is delimited by a depth scan that
    tracks string literals rather than by a greedy regex.
    """
    match = re.search(r"window\.__INITIAL_STATE\s*=\s*", html or "")
    if match is None:
        raise ExtractionError("Woohoo product page has no __INITIAL_STATE payload")
    blob = html[match.end():]
    depth = 0
    end: int | None = None
    in_string = False
    escaped = False
    for index, char in enumerate(blob):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end is None:
        raise ExtractionError("Woohoo __INITIAL_STATE payload is truncated")
    try:
        state = json.loads(blob[:end])
    except ValueError as exc:
        raise ExtractionError(
            f"Woohoo __INITIAL_STATE is not valid JSON: {exc}") from exc
    if not isinstance(state, dict):
        raise ExtractionError("Woohoo __INITIAL_STATE is not a JSON object")
    return state


def woohoo_inventory_url(upc_id: str | int) -> str:
    """Woohoo's public inventory-ledger URL for one product UPC.

    Exactly the query the site's own shipped JavaScript builds for
    ``GET product/inventories``: a Magento-style ``searchCriteria`` filter on
    ``universalProductCodes.entityId``. It runs on the unauthenticated
    ``/proxy`` path with no cookie or credential, and Woohoo's robots.txt
    disallows only ``/en-in/`` -- this path is unrestricted.
    """
    query = urlencode({
        "searchCriteria[filter_groups][0][filters][0][field]":
            "universalProductCodes.entityId",
        "searchCriteria[filter_groups][0][filters][0][condition_type]": "eq",
        "searchCriteria[filter_groups][0][filters][0][value]": str(upc_id),
        "searchCriteria[limit]": "2000",
        "searchCriteria[offset]": "0",
    })
    return f"https://www.woohoo.in/proxy/locale/en-in/product/inventories?{query}"


class _ResponseMemo:
    """Process-local cache of one URL's response text or failure.

    The five Woohoo entries watch a single product page and a single
    inventory endpoint, observed seconds apart inside one cycle. In
    production a cycle *is* one process (a GitHub Actions job), so the memo
    collapses those reads to one request; the TTL keeps shorter-lived reuse
    (tests, local loops) from replaying data captured an earlier cycle --
    it expires well before the next cron tick. Failures are memoized for
    the same reason ``github_runner.observe_cached`` memoizes them: a
    hanging endpoint must cost one timeout per cycle, not one per entry.
    """

    def __init__(self, ttl: float = 45.0) -> None:
        self._ttl = ttl
        self._entries: dict[str, tuple[float, str | BaseException]] = {}

    def get(self, url: str) -> str | BaseException | None:
        hit = self._entries.get(url)
        if hit is None:
            return None
        age, value = hit
        if time.monotonic() - age > self._ttl:
            self._entries.pop(url, None)
            return None
        return value

    def put(self, url: str, value: str | BaseException) -> None:
        self._entries[url] = (time.monotonic(), value)

    def clear(self) -> None:
        self._entries.clear()


_WOOHOO_INVENTORY = _ResponseMemo(ttl=45.0)


def fetch_woohoo_inventory(url: str, timeout: float = 30.0) -> str:
    """GET Woohoo's inventory ledger at most once per cycle per URL.

    A failure already recorded this cycle is re-raised verbatim, so one bad
    endpoint costs one timeout for the whole cycle instead of one per entry.
    """
    cached = _WOOHOO_INVENTORY.get(url)
    if cached is not None:
        if isinstance(cached, BaseException):
            raise cached
        return cached
    try:
        text = fetch_html(url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - memoized, then reported by observe
        _WOOHOO_INVENTORY.put(url, exc)
        raise
    _WOOHOO_INVENTORY.put(url, text)
    return text


def extract_woohoo(html: str, product: dict[str, Any]) -> dict[str, Any]:
    """Read one Woohoo denomination: offered slabs from the page, stock from
    the site's own public inventory ledger.

    The product page is server-rendered and embeds
    ``window.__INITIAL_STATE`` with the product's UPC id, its offered
    denomination slabs and their amounts -- but it carries no stock field at
    all. Stock lives on the unauthenticated endpoint the page's own bundle
    calls (``/proxy/locale/en-in/product/inventories?...entityId=<upc>``),
    which serves the *out-of-stock* ledger. The direction of that mapping is
    proven by the shipped bundle: it collects every returned row's
    ``denomination`` into ``outOfStockDenominations`` and blocks cart items
    whose price is in that set ("Item is out of stock. Please remove this
    item from your cart to continue"), the denomination selector switches
    away from a listed value, and control brands that are normally
    purchasable return an empty list.

    Readings for the configured denomination:

    * offered on the page and absent from the ledger -> ``IN_STOCK``;
    * present in the ledger -> ``OUT_OF_STOCK`` (explicit signal);
    * a ledger row without any denomination -> the whole product is sold
      out (the bundle's ``isProductOutOfStock``);
    * not among the offered slabs, no slab list published, or a partial
      ledger (``total`` larger than the rows returned) -> ``UNKNOWN``:
      "not offered" and "out of stock" are different facts, and neither the
      default slab display nor the quantity limit says anything about
      stock.

    Fetch failures and unexpected payload shapes raise, so a blocked or
    mangled reply preserves the last known status instead of overwriting it.
    """
    state = woohoo_initial_state(html)
    app = state.get("appReducer")
    details = app.get("product") if isinstance(app, dict) else None
    if not isinstance(details, dict) or not details:
        raise ExtractionError("Woohoo page payload has no product data")
    upc = details.get("upc")
    upc_id = upc.get("id") if isinstance(upc, dict) else None
    if not upc_id:
        raise ExtractionError("Woohoo page payload has no product UPC id")

    price_cfg = details.get("price")
    slabs = price_cfg.get("denominations") if isinstance(price_cfg, dict) else None
    try:
        wanted = float(product.get("denomination"))
    except (TypeError, ValueError):
        wanted = None
    if wanted is None:
        return {
            "ok": True, "status": UNKNOWN, "price": None, "discount": "",
            "cashback": "", "error": "",
            "note": "no usable denomination configured for this entry",
        }
    if not isinstance(slabs, list) or not slabs:
        return {
            "ok": True, "status": UNKNOWN, "price": None, "discount": "",
            "cashback": "", "error": "",
            "note": "Woohoo publishes no denomination list for this product",
        }
    offered: dict[float, float] = {}
    for slab in slabs:
        try:
            amount = float(slab)
        except (TypeError, ValueError):
            continue
        offered[amount] = amount
    if wanted not in offered:
        return {
            "ok": True, "status": UNKNOWN, "price": None, "discount": "",
            "cashback": "", "error": "",
            "note": (f"denomination {product.get('denomination')} is not among "
                     "Woohoo's offered slabs"),
        }

    body = fetch_woohoo_inventory(woohoo_inventory_url(upc_id))
    try:
        payload = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise ExtractionError(
            f"inventory response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("code") not in ("0", 0):
        code = (payload.get("code") if isinstance(payload, dict)
                else type(payload).__name__)
        raise ExtractionError(f"unexpected inventory payload (code={code!r})")
    rows = payload.get("inventories")
    if not isinstance(rows, list):
        raise ExtractionError("inventory payload has no inventory list")
    try:
        total = int(payload.get("total"))
    except (TypeError, ValueError):
        total = None
    if total is not None and total != len(rows):
        return {
            "ok": True, "status": UNKNOWN, "price": None, "discount": "",
            "cashback": "", "error": "",
            "note": (f"inventory response was incomplete "
                     f"(total={total}, rows={len(rows)})"),
        }

    def row_denomination(row: Any) -> str:
        value = row.get("denomination") if isinstance(row, dict) else None
        return str(value or "").strip()

    if any(not row_denomination(row) for row in rows):
        # The bundle's isProductOutOfStock: a row without a denomination
        # means the product itself, not one slab, is sold out.
        status = OUT_OF_STOCK
    else:
        ledger: set[float] = set()
        for row in rows:
            try:
                ledger.add(float(row_denomination(row)))
            except ValueError:
                continue
        status = OUT_OF_STOCK if wanted in ledger else IN_STOCK

    return {
        "ok": True,
        "status": status,
        "price": offered[wanted],
        "discount": "",
        "cashback": "",
        "error": "",
        "note": "",
    }


def extract_wc_store_api(html: str, product: dict[str, Any]) -> dict[str, Any]:
    """Read one denomination from a public WooCommerce Store API response.

    Two permitted endpoints answer this function, both under
    ``/wp-json/wc/store/v1/products`` and both allowed by the store's
    ``robots.txt`` (``User-agent: * / Allow: /``):

    * the **collection query** (``?slug=…``) returns a JSON array containing
      **only the products the catalogue lists** -- on the store that
      publishes it, out-of-stock products are excluded from that query
      (verified 2026-11-13: all 2762 listed products read
      ``is_in_stock: true``, while 8 of 8 sampled sitemap products absent
      from the listing read ``schema.org/OutOfStock`` on their readable
      pages);
    * the **single-product path route** (``/<slug>``) returns one product
      object *even when the catalogue hides it*, with the same explicit
      stock fields -- verified 2026-11-13 on all six hidden wallet products
      (``is_in_stock: false`` with exact prices), an in-stock listed
      control, a hidden out-of-stock control, and 404 for a slug that does
      not exist (404 raises in ``fetch_html`` before this code runs).

    Answer shapes, and only two of them are stock facts:

    * listed with ``is_in_stock: true`` *and* ``is_purchasable: true`` (and a
      price matching the configured denomination) -> ``IN_STOCK``;
    * listed with ``is_in_stock: false`` -> ``OUT_OF_STOCK``: an explicit
      stock field, not an absence;
    * **not present** in the payload -> ``UNKNOWN``. Absence is never
      ``OUT_OF_STOCK``: a product can also be missing because it was
      renamed, hidden, or the payload changed, and misreading that as a
      stockout is exactly the failure the monitoring rules forbid.

    The slug is derived from the configured page URL so an exact match ties
    the response to this denomination, and the price (the Store API reports
    paise) must equal the configured value before any status is believed.
    """
    try:
        data = json.loads(html)
    except (ValueError, TypeError) as exc:
        raise ExtractionError(
            f"Store API response is not valid JSON: {exc}") from exc
    if isinstance(data, list):
        items = data
        shape = "catalogue"
    elif isinstance(data, dict) and isinstance(data.get("slug"), str):
        items = [data]
        shape = "single product"
    else:
        raise ExtractionError(
            f"unexpected Store API payload: {type(data).__name__}, "
            "expected list or product object")

    path = urlparse(str(product.get("url") or "")).path.rstrip("/")
    expected = path.rsplit("/", 1)[-1] if path else ""
    if not expected:
        raise ExtractionError("cannot derive product slug from the configured url")

    match = next((item for item in items
                  if isinstance(item, dict) and item.get("slug") == expected),
                 None)
    if match is None:
        if shape == "single product":
            got = items[0].get("slug")
            return {
                "ok": True, "status": UNKNOWN, "price": None, "discount": "",
                "cashback": "", "error": "",
                "note": f"endpoint answered for a different product ({got}); "
                        "refusing to claim a status",
            }
        return {
            "ok": True, "status": UNKNOWN, "price": None, "discount": "",
            "cashback": "", "error": "",
            "note": "not in the public catalogue; the store excludes "
                    "out-of-stock products, so absence claims no stock",
        }

    prices = match.get("prices") if isinstance(match.get("prices"), dict) else {}
    price: float | None
    try:
        price = float(prices.get("price")) / 100.0  # Store API prices are paise
    except (TypeError, ValueError):
        price = None

    denomination = product.get("denomination")
    if denomination is not None:
        if price is None:
            return {
                "ok": True, "status": UNKNOWN, "price": None, "discount": "",
                "cashback": "", "error": "",
                "note": "listed without a usable price; refusing to claim a status",
            }
        try:
            matches_denomination = price == float(denomination)
        except (TypeError, ValueError):
            matches_denomination = False
        if not matches_denomination:
            return {
                "ok": True, "status": UNKNOWN, "price": price, "discount": "",
                "cashback": "", "error": "",
                "note": (f"listed price {price:,.0f} does not match the "
                         f"configured denomination {denomination}"),
            }

    in_stock = match.get("is_in_stock")
    purchasable = match.get("is_purchasable")
    if in_stock is True and purchasable is True:
        status = IN_STOCK
    elif in_stock is False:
        # An explicit stock field on the exact product: a real reading.
        status = OUT_OF_STOCK
    else:
        # Missing stock field, or "in stock" without purchasability: neither
        # half alone means "buy it now", and nothing here means "sold out".
        status = UNKNOWN

    return {
        "ok": True, "status": status, "price": price, "discount": "",
        "cashback": "", "error": "", "note": "",
    }


EXTRACTORS = {
    "rooter_rsc": extract_rooter,
    "html_text": extract_html,
    "jsonld_offers": extract_jsonld,
    "gyftr_api": extract_gyftr,
    "woohoo_api": extract_woohoo,
    "wc_store_api": extract_wc_store_api,
}


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def _headers_for(url: str) -> dict[str, str]:
    """Request headers for ``url``.

    GYFTR's public JSON API answers only to requests that identify where
    they come from, so for that host we send the same ``Origin``/``Referer``
    its own JavaScript attaches to every call from the brand page. The
    endpoint is unauthenticated, published in the site's shipped bundles,
    and neither host's robots.txt restricts it: no cookie, credential,
    challenge or fingerprint is involved -- only the standard headers a
    normal browser sends. Every other URL gets the plain :data:`HEADERS`.
    """
    headers = dict(HEADERS)
    if (urlparse(url).hostname or "").lower() == "api.gyftr.com":
        headers.update({
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.gyftr.com",
            "Referer": "https://www.gyftr.com/",
        })
    return headers


#: Extra attempts allowed for a *transient* failure (see :func:`fetch_html`).
#: Access denials are never retried: re-asking a server that just answered
#: 403/429 is precisely the behaviour the monitoring rules forbid.
FETCH_RETRY_ATTEMPTS = 1
#: Base pause between attempts, in seconds. Tests set this to 0 so the suite
#: never sleeps; a little jitter keeps retries off a fixed metronome.
FETCH_RETRY_BACKOFF = 1.0


def _is_retryable(exc: BaseException) -> bool:
    """True only for faults that a second attempt can plausibly fix.

    Connection resets, read timeouts and HTTP 5xx are transient. HTTP 4xx
    -- 403 and 429 especially -- is a decision, not a glitch: it is
    returned as-is, once, and surfaces as a failed observation that can
    never become a stock status.
    """
    if isinstance(exc, (requests.Timeout, requests.ConnectionError,
                        ConnectionError)):
        return True
    if isinstance(exc, requests.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        return isinstance(status, int) and status >= 500
    return False


def fetch_html(url: str, timeout: float = 30.0,
               session: requests.Session | None = None,
               retries: int | None = None) -> str:
    """GET a product page. Raises on HTTP/network failure.

    Deliberately performs a plain request: no headless browser, no cookie
    jar, no CAPTCHA handling, no anti-bot circumvention.

    ``retries`` bounds the number of *extra* attempts (default
    :data:`FETCH_RETRY_ATTEMPTS`) and applies only to transient faults --
    connection errors, timeouts and HTTP 5xx. HTTP 403, 429 and other 4xx
    answers are final and are never re-asked. When attempts are exhausted
    the original exception propagates, so callers record a failure and the
    previous stock status is preserved.
    """
    client = session or requests
    attempts = FETCH_RETRY_ATTEMPTS if retries is None else max(0, retries)
    while True:
        try:
            response = client.get(url, headers=_headers_for(url),
                                  timeout=timeout)
            response.raise_for_status()
            return response.text
        except Exception as exc:  # noqa: BLE001 - classified, then re-raised
            if not _is_retryable(exc):
                raise
            attempts -= 1
            if attempts < 0:
                raise
            if FETCH_RETRY_BACKOFF > 0:
                time.sleep(FETCH_RETRY_BACKOFF
                           + random.uniform(0.0, FETCH_RETRY_BACKOFF * 0.25))


def fetch_failed_observation(exc: BaseException) -> dict[str, Any]:
    """Observation for a product whose page could not be read.

    Defined separately from :func:`observe` so callers that cache pages -- and
    may therefore replay an earlier failure -- report it identically to a live
    attempt, instead of drifting into a second copy of the same shape.
    """
    return {
        "ok": False, "status": None, "price": None, "discount": "",
        "cashback": "", "error": f"{type(exc).__name__}: {exc}"[:400], "note": "",
    }


def _fallback_observation(product: dict[str, Any], primary_error: BaseException,
                          session: requests.Session | None) -> dict[str, Any]:
    """Read a product through its configured secondary source.

    Used only when the primary page could not be read (fetch *or* parse).
    A product without a ``fallback`` behaves exactly as before -- the
    primary failure is reported verbatim -- so failure preservation for
    every other integration is untouched. The fallback is attempted at
    most once per cycle: the runner caches the primary failure, and
    replays it through this function instead of re-asking the endpoint
    that just refused us.
    """
    fallback = product.get("fallback")
    if not isinstance(fallback, dict):
        return fetch_failed_observation(primary_error)

    url = fallback.get("url")
    name = fallback.get("extractor") or product.get("extractor", "html_text")
    if not url:
        return {
            "ok": False, "status": None, "price": None, "discount": "",
            "cashback": "", "error": "fallback config has no url", "note": "",
        }
    extractor = EXTRACTORS.get(str(name))
    if extractor is None:
        return {
            "ok": False, "status": None, "price": None, "discount": "",
            "cashback": "", "error": f"unknown fallback extractor: {name}",
            "note": "",
        }
    try:
        page = fetch_html(str(url), session=session)
        result = extractor(page, product)
        result.setdefault("ok", True)
        result.setdefault("error", "")
        result.setdefault("note", "")
        return result
    except Exception as exc:  # noqa: BLE001 - one product must never kill the run
        log.warning("Fallback read failed for %s: %s",
                    product.get("id"), type(exc).__name__)
        return fetch_failed_observation(exc)


def observe(product: dict[str, Any], html: str | None = None,
            session: requests.Session | None = None,
            primary_error: BaseException | None = None) -> dict[str, Any]:
    """Return an observation for a product. Never raises.

    ``primary_error`` carries a failure that already happened this cycle
    (the runner caches one fetch per URL), so a configured fallback source
    gets its turn without re-asking the primary endpoint -- which matters
    because re-asking a 403 is exactly what the request rules forbid.
    """
    extractor_name = product.get("extractor", "html_text")
    extractor = EXTRACTORS.get(extractor_name)
    if extractor is None:
        return {
            "ok": False, "status": None, "price": None, "discount": "",
            "cashback": "", "error": f"unknown extractor: {extractor_name}", "note": "",
        }
    url = product.get("url")
    if not url:
        return {
            "ok": False, "status": None, "price": None, "discount": "",
            "cashback": "", "error": "product has no url", "note": "",
        }
    if primary_error is not None:
        return _fallback_observation(product, primary_error, session)
    try:
        page = html if html is not None else fetch_html(url, session=session)
        result = extractor(page, product)
        result.setdefault("ok", True)
        result.setdefault("error", "")
        result.setdefault("note", "")
        return result
    except Exception as exc:  # noqa: BLE001 - one product must never kill the run
        log.warning("Fetch/parse failed for %s: %s", product.get("id"), type(exc).__name__)
        if isinstance(product.get("fallback"), dict):
            log.info("Primary source failed for %s; reading the configured "
                     "fallback source", product.get("id"))
            return _fallback_observation(product, exc, session)
        return fetch_failed_observation(exc)


# --------------------------------------------------------------------------- #
# Transition evaluation and alert generation
# --------------------------------------------------------------------------- #
@dataclass
class Alert:
    type: str
    product_id: str
    message: str


@dataclass
class Evaluation:
    entry: dict[str, Any]
    alerts: list[Alert] = field(default_factory=list)
    changed: bool = False


def _blank_entry(now: str) -> dict[str, Any]:
    return {
        "status": UNKNOWN,
        "price": None,
        "discount": "",
        "cashback": "",
        "last_error": "",
        "first_seen_at": now,
        "last_changed_at": now,
        "baseline": False,
        "last_known_status": None,
        "note": "",
    }


def evaluate(product: dict[str, Any], previous: dict[str, Any] | None,
             observation: dict[str, Any], now: str) -> Evaluation:
    """Compare an observation against the previous state and emit alerts.

    Guarantees:

    * a failed fetch never changes ``status`` (so it can never become
      ``OUT_OF_STOCK``);
    * the first successful observation only seeds the baseline;
    * after an alert fires, state advances so the identical condition cannot
      fire again on the next run.
    """
    entry = dict(previous) if previous else _blank_entry(now)
    if "baseline" not in entry:
        entry["baseline"] = bool(previous)
    alerts: list[Alert] = []
    label = product_label(product)
    # An optional per-product ``link`` overrides the fetched URL when those
    # differ (GYFTR's feed URL is JSON a person cannot open in a browser).
    link = product.get("link") or product.get("url", "")

    if not observation.get("ok"):
        # Network/parse failure: keep the last known status verbatim.
        entry["last_error"] = str(observation.get("error") or "unknown error")[:400]
        entry["note"] = str(observation.get("note") or "")
        entry["first_seen_at"] = entry.get("first_seen_at") or now
        return Evaluation(entry=entry, alerts=[], changed=entry != previous)

    entry["last_error"] = ""
    entry["note"] = str(observation.get("note") or "")

    new_status = observation.get("status") or UNKNOWN
    new_price = observation.get("price")
    new_discount = str(observation.get("discount") or "")
    new_cashback = str(observation.get("cashback") or "")

    # The most recent *known* status. UNKNOWN is deliberately excluded so that
    # "we could not read the page" can never stand in for a real stock reading.
    previous_known = entry.get("last_known_status")

    if not entry.get("baseline"):
        # First successful observation: establish baseline, raise nothing.
        entry.update({
            "status": new_status,
            "price": new_price,
            "discount": new_discount,
            "cashback": new_cashback,
            "baseline": True,
            "first_seen_at": now,
            "last_changed_at": now,
        })
        if new_status != UNKNOWN:
            entry["last_known_status"] = new_status
        return Evaluation(entry=entry, alerts=[], changed=True)

    old_status = entry.get("status")
    old_price = entry.get("price")
    old_discount = str(entry.get("discount") or "")
    old_cashback = str(entry.get("cashback") or "")

    # 1. Back in stock.
    #    Only a previously *observed* out-of-stock counts. UNKNOWN -> IN_STOCK
    #    means we simply learned how to read the page, so it must not claim a
    #    restock we never witnessed. Tracking the last known status separately
    #    also means OUT_OF_STOCK -> UNKNOWN -> IN_STOCK still alerts correctly.
    if new_status == IN_STOCK and previous_known == OUT_OF_STOCK:
        message = f"🟢 BACK IN STOCK\n{label}\nLink: {link}"
        if new_price is not None:
            message += f"\nPrice: ₹{new_price:,.2f}"
        alerts.append(Alert("RESTOCK", product.get("id", ""), message))

    # 2. Price drop. Increases are silent.
    if (isinstance(old_price, (int, float)) and isinstance(new_price, (int, float))
            and new_price < old_price):
        alerts.append(Alert(
            "PRICE_DROP",
            product.get("id", ""),
            f"🔻 PRICE DROP\n{label}\n"
            f"₹{old_price:,.2f} → ₹{new_price:,.2f}\nLink: {link}",
        ))

    # 3. Discount / cashback offer appearing or changing.
    if new_discount and new_discount != old_discount:
        alerts.append(Alert(
            "DISCOUNT",
            product.get("id", ""),
            f"🏷️ DISCOUNT / OFFER UPDATE\n{label}\n{new_discount}\nLink: {link}",
        ))
    if new_cashback and new_cashback != old_cashback:
        alerts.append(Alert(
            "CASHBACK",
            product.get("id", ""),
            f"💰 CASHBACK UPDATE\n{label}\n{new_cashback}\nLink: {link}",
        ))

    changed_fields = {
        "status": old_status != new_status,
        "price": old_price != new_price,
        "discount": old_discount != new_discount,
        "cashback": old_cashback != new_cashback,
    }
    changed = any(changed_fields.values())

    entry.update({
        "status": new_status,
        "price": new_price,
        "discount": new_discount,
        "cashback": new_cashback,
    })
    if new_status != UNKNOWN:
        entry["last_known_status"] = new_status
    if changed:
        entry["last_changed_at"] = now
    return Evaluation(entry=entry, alerts=alerts, changed=changed or bool(alerts))


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def telegram_token() -> str:
    """Read the bot token from the environment only."""
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def chat_id() -> str:
    """Read the destination chat id from the environment only.

    On GitHub Actions this is the ``TELEGRAM_CHAT_ID`` repository secret; in
    local development it is an optional ``TELEGRAM_CHAT_ID`` variable in the
    gitignored ``.env``. It is never read from, or written to, the state
    file, and never logged.
    """
    return os.getenv("TELEGRAM_CHAT_ID", "").strip()


def scrub(text: str, token: str | None = None) -> str:
    """Remove the bot token from any text destined for a log or error message."""
    secret = token if token is not None else telegram_token()
    if not secret:
        return text
    return text.replace(secret, "***TOKEN***")


def utf16_length(text: str) -> int:
    """Length of ``text`` in UTF-16 code units -- what Telegram actually counts."""
    return len(text.encode("utf-16-le")) // 2


def _keep_run_intact(text: str, cut: int, budget: int, token: str) -> int:
    """Adjust ``cut`` so a delimited run of ``token`` is not cut in half.

    If the run opens before the cut, move the cut in front of it. If it opens
    at position 0 (so there is nowhere to move back to), close it instead when
    the closing token still fits inside the budget.
    """
    if text[:cut].count(token) % 2 == 0:
        return cut
    open_at = text.rfind(token, 0, cut)
    if open_at > 0:
        return open_at
    close_at = text.find(token, cut)
    if close_at != -1 and close_at + len(token) <= budget:
        return close_at + len(token)
    return cut


def _safe_cut(text: str, limit: int) -> int:
    """Index at which ``text`` may be cut while staying within ``limit``.

    ``limit`` is measured in UTF-16 code units (Telegram's accounting). The cut
    prefers a word boundary and then adjusts so it cannot land inside an
    HTML tag, a Markdown code span or an unclosed emphasis run. The messages
    this bot sends are plain text (no ``parse_mode``), so such entities are
    never interpreted -- the guards exist so that stays true even if markup is
    added later. Always returns at least 1, so callers make progress.
    """
    if utf16_length(text) <= limit:
        return len(text)

    # Convert the UTF-16 budget into a Python string index.
    budget = 0
    units = 0
    for budget in range(len(text)):
        step = 2 if ord(text[budget]) > 0xFFFF else 1
        if units + step > limit:
            break
        units += step
    else:
        budget = len(text)
    if budget <= 0:
        # The limit is smaller than the first character; still make progress.
        return 1

    # Prefer a word boundary inside the budget (consume the space so the next
    # part does not start with it).
    space_at = text.rfind(" ", 0, budget)
    cut = space_at + 1 if space_at > 0 else budget

    # Never end inside an HTML tag: if the last "<" has no matching ">" before
    # the cut, move the cut in front of that "<" (or just past the tag's ">"
    # when the tag starts at 0 and fits inside the budget).
    open_at = text.rfind("<", 0, cut)
    if open_at > text.rfind(">", 0, cut):
        if open_at > 0:
            cut = open_at
        else:
            close_at = text.find(">", cut)
            if close_at != -1 and close_at + 1 <= budget:
                cut = close_at + 1

    # Never end inside a Markdown code span or an unclosed emphasis run.
    for token in ("`",) + _FORMAT_MARKERS:
        cut = _keep_run_intact(text, cut, budget, token)

    return cut if cut > 0 else 1


def split_message(text: str, limit: int = TELEGRAM_SPLIT_LIMIT) -> list[str]:
    """Split ``text`` into ordered parts that each fit Telegram's limit.

    Guarantees:

    * lossless -- ``"".join(parts) == text``; nothing is ever discarded;
    * every part is within ``limit`` UTF-16 code units (<= Telegram's 4096);
    * cuts prefer line boundaries, so headings and their content stay
      together and each part reads from the start of a line;
    * a single line longer than the limit is cut at a formatting-safe point
      rather than dropped.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if not text:
        return []
    if utf16_length(text) <= limit:
        return [text]

    parts: list[str] = []
    chunk = ""
    for line in text.splitlines(keepends=True):
        # Flush the held chunk first so a long line starts a fresh part.
        while utf16_length(line) > limit:
            if chunk:
                parts.append(chunk)
                chunk = ""
            cut = _safe_cut(line, limit)
            parts.append(line[:cut])
            line = line[cut:]
        if utf16_length(chunk) + utf16_length(line) > limit:
            parts.append(chunk)
            chunk = line
        else:
            chunk += line
    if chunk:
        parts.append(chunk)
    return parts


def _send_part(token: str, chat_id: str, text: str,
               session: requests.Session | None = None) -> bool:
    """POST one already-sized message part. Returns True on success.

    Exceptions are logged through :func:`scrub` so the token -- which appears
    in the request path -- can never leak into GitHub Actions logs.
    """
    client = session or requests
    try:
        response = client.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=25,
        )
        payload = response.json() if response.content else {}
        if response.status_code != 200 or not payload.get("ok"):
            log.error("Telegram sendMessage failed (HTTP %s): %s",
                      response.status_code, scrub(str(payload), token))
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("Telegram request failed: %s: %s",
                  type(exc).__name__, scrub(str(exc), token))
        return False


def send_message(token: str, chat_id: str, text: str,
                 session: requests.Session | None = None) -> bool:
    """Send a Telegram message, splitting it when it exceeds the limit.

    Long content (the daily report) is split at line boundaries and every
    part is sent in order -- report content is never silently truncated. A
    failed part is logged with its position and makes the overall send report
    failure, so the caller retries and the content is eventually delivered in
    full. Returns True only when every part was delivered.
    """
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN not set; notification skipped")
        return False
    if not chat_id:
        log.warning("No destination chat (TELEGRAM_CHAT_ID); notification skipped")
        return False

    parts = split_message(text)
    if not parts:
        log.warning("Refusing to send an empty Telegram message")
        return False

    delivered = 0
    for index, part in enumerate(parts, start=1):
        if _send_part(token, chat_id, part, session):
            delivered += 1
        else:
            log.error("Telegram message part %d of %d could not be delivered",
                      index, len(parts))

    if len(parts) > 1:
        log.info("Telegram message split into %d parts; %d delivered",
                 len(parts), delivered)
    return delivered == len(parts)


def claim_chat(token: str, session: requests.Session | None = None) -> str:
    """One-shot ``getUpdates`` to pick up a pending ``/start``.

    Returns "" when nothing is pending. A 409 conflict (another poller such as
    ``bot.py`` is already running) is logged and treated as "nothing to do"
    rather than an error.
    """
    if not token:
        return ""
    client = session or requests
    try:
        response = client.get(
            f"{TELEGRAM_API}/bot{token}/getUpdates",
            params={"timeout": 0, "allowed_updates": json.dumps(["message"])},
            timeout=25,
        )
        payload = response.json() if response.content else {}
    except Exception as exc:  # noqa: BLE001
        log.warning("getUpdates failed: %s: %s", type(exc).__name__, scrub(str(exc), token))
        return ""

    if not payload.get("ok"):
        description = str(payload.get("description") or "")
        if "409" in description or "terminated by other getUpdates" in description:
            log.info("Another Telegram poller is active; skipping chat claim")
        else:
            log.warning("getUpdates rejected: %s", scrub(description, token))
        return ""

    updates = payload.get("result") or []
    if not updates:
        log.info("getUpdates: no pending updates at all - nothing has been "
                 "sent to the bot yet")
        return ""

    chat_id = ""
    for update in updates:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        ctype = str(chat.get("type") or "unknown")
        raw = str(message.get("text") or "").strip()
        command = raw.split()[0].lower() if raw else ""
        # Telegram sends "/start" for a typed command but "/start@BotName"
        # when the command is picked from the bot's command menu.
        if ctype == "private" and command.split("@")[0] == "/start":
            chat_id = str(chat.get("id") or "")
            # No digits in the log: chat ids are never written to logs.
            log.info("Claimed a private chat for local pairing")
            continue
        log.info("Ignoring update: chat_type=%s command=%r", ctype,
                 command[:30] or "<no text>")

    if not chat_id:
        log.info("Saw %d update(s) but none was a /start in a private chat.",
                 len(updates))
    return chat_id


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def report_status(entry: dict[str, Any]) -> str:
    """Status to display for a state entry, defaulting to NOT_CHECKED.

    A missing, null or unrecognised stored status means "we have no usable
    reading for this product" -- it is reported as NOT_CHECKED and never as
    OUT_OF_STOCK.
    """
    status = entry.get("status")
    return status if status in KNOWN_STATUSES else NOT_CHECKED


def build_report(products: list[dict[str, Any]], state: dict[str, Any],
                 now: str, title: str) -> str:
    """Build the availability report text sent to Telegram.

    Every enabled product is accounted for exactly once across
    IN_STOCK / OUT_OF_STOCK / UNKNOWN / NOT_CHECKED, and the displayed total
    is reconciled against the enabled-product count.
    """
    lines = [f"🎮 {title}", now, ""]
    active = enabled_products(products)
    if not active:
        lines.append("No products enabled yet. Add verified URLs/selectors to "
                     "config/products.yaml and set enabled: true.")
        return "\n".join(lines)

    entries = state.get("products", {})
    counts = {status: 0 for status in (IN_STOCK, OUT_OF_STOCK, UNKNOWN, NOT_CHECKED)}
    for product in active:
        # report_status() maps anything unmapped to NOT_CHECKED, so the sum of
        # the four buckets is exactly len(active) by construction.
        counts[report_status(entries.get(product["id"]) or {})] += 1

    total = sum(counts.values())
    lines.append(
        f"In stock: {counts[IN_STOCK]} · Out of stock: {counts[OUT_OF_STOCK]} "
        f"· Unknown: {counts[UNKNOWN]} · Not checked: {counts[NOT_CHECKED]}"
        f" · Total: {total}/{len(active)} enabled"
    )
    lines.append("")

    last_error_seen = False
    for product in active:
        entry = entries.get(product["id"]) or {}
        status = report_status(entry)
        price = entry.get("price")
        price_text = f" · ₹{price:,.2f}" if isinstance(price, (int, float)) else ""
        lines.append(f"{product_label(product)}: {status}{price_text}")
        if entry.get("discount"):
            lines.append(f"  Offer: {entry['discount']}")
        if entry.get("cashback"):
            lines.append(f"  Cashback: {entry['cashback']}")
        if entry.get("last_error"):
            last_error_seen = True
            lines.append(f"  ⚠️ {str(entry['last_error'])[:120]}")
        elif entry.get("note"):
            lines.append(f"  ℹ️ {str(entry['note'])[:120]}")
        lines.append(product.get("url", ""))

    disabled = [p for p in products if not p.get("enabled")]
    if disabled:
        lines.append("")
        lines.append(f"({len(disabled)} configured product(s) disabled / unverified)")

    if last_error_seen:
        lines.append("")
        lines.append("⚠️ Some products could not be read. Unknown status is NOT "
                     "treated as out of stock.")
    return "\n".join(lines)
