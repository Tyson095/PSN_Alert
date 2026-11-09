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
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
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


# --------------------------------------------------------------------------- #
# Persistent state
# --------------------------------------------------------------------------- #
def state_path() -> Path:
    override = os.getenv("STATE_PATH", "").strip()
    return Path(override) if override else DEFAULT_STATE_PATH


def default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "chat_id": "",
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
    base.update({k: v for k, v in data.items() if k in base})
    if not isinstance(base["products"], dict):
        base["products"] = {}
    if not isinstance(base["events"], list):
        base["events"] = []
    return base


def save_state(state: dict[str, Any], path: str | Path | None = None) -> Path:
    """Atomically write state so a crashed run cannot corrupt the file."""
    target = Path(path) if path else state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    events = state.get("events", [])
    if len(events) > MAX_EVENTS:
        state["events"] = events[-MAX_EVENTS:]
    payload = json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
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


EXTRACTORS = {
    "rooter_rsc": extract_rooter,
    "html_text": extract_html,
    "jsonld_offers": extract_jsonld,
}


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def fetch_html(url: str, timeout: float = 30.0, session: requests.Session | None = None) -> str:
    """GET a product page. Raises on HTTP/network failure.

    Deliberately performs a single plain request: no headless browser, no
    cookie jar, no CAPTCHA handling, no anti-bot circumvention.
    """
    client = session or requests
    response = client.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.text


def observe(product: dict[str, Any], html: str | None = None,
            session: requests.Session | None = None) -> dict[str, Any]:
    """Return an observation for a product. Never raises."""
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
    try:
        page = html if html is not None else fetch_html(url, session=session)
        result = extractor(page, product)
        result.setdefault("ok", True)
        result.setdefault("error", "")
        result.setdefault("note", "")
        return result
    except Exception as exc:  # noqa: BLE001 - one product must never kill the run
        log.warning("Fetch/parse failed for %s: %s", product.get("id"), type(exc).__name__)
        return {
            "ok": False, "status": None, "price": None, "discount": "",
            "cashback": "", "error": f"{type(exc).__name__}: {exc}"[:400], "note": "",
        }


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
    link = product.get("url", "")

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


def scrub(text: str, token: str | None = None) -> str:
    """Remove the bot token from any text destined for a log or error message."""
    secret = token if token is not None else telegram_token()
    if not secret:
        return text
    return text.replace(secret, "***TOKEN***")


def send_message(token: str, chat_id: str, text: str,
                 session: requests.Session | None = None) -> bool:
    """Send one Telegram message. Returns True on success.

    Exceptions are logged through :func:`scrub` so the token -- which appears in
    the request path -- can never leak into GitHub Actions logs.
    """
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN not set; notification skipped")
        return False
    if not chat_id:
        log.warning("No chat_id yet; send /start to the bot first")
        return False
    client = session or requests
    try:
        response = client.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text[:4000],
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
            log.info("Claimed private chat ...%s", chat_id[-4:])
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
def build_report(products: list[dict[str, Any]], state: dict[str, Any],
                 now: str, title: str) -> str:
    """Build the availability report text sent to Telegram."""
    lines = [f"🎮 {title}", now, ""]
    active = enabled_products(products)
    if not active:
        lines.append("No products enabled yet. Add verified URLs/selectors to "
                     "config/products.yaml and set enabled: true.")
        return "\n".join(lines)

    entries = state.get("products", {})
    counts = {IN_STOCK: 0, OUT_OF_STOCK: 0, UNKNOWN: 0}
    for product in active:
        entry = entries.get(product["id"]) or {}
        status = entry.get("status", "NOT_CHECKED")
        if status in counts:
            counts[status] += 1

    lines.append(
        f"In stock: {counts[IN_STOCK]} · Out of stock: {counts[OUT_OF_STOCK]} "
        f"· Unknown: {counts[UNKNOWN]}"
    )
    lines.append("")

    last_error_seen = False
    for product in active:
        entry = entries.get(product["id"]) or {}
        status = entry.get("status", "NOT_CHECKED")
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
