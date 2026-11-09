#!/usr/bin/env python3
"""One-shot monitoring run for GitHub Actions.

Performs a single complete monitoring cycle and exits. There is no scheduler
and no long-lived Telegram polling loop -- GitHub Actions starts a fresh
container for every run, so anything that must survive between runs lives in
the committed state file (``state/state.json``).

Typical usage::

    python github_runner.py                 # normal cycle
    python github_runner.py --dry-run       # fetch + report, send nothing
    python github_runner.py --report        # also force the daily report now

Exit codes::

    0  cycle completed (individual product failures are logged as warnings)
    1  fatal configuration or state error
    2  every enabled product failed -- almost certainly a blocking problem
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Allow running as a plain script from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from psn_gift_card_alert_bot import core  # noqa: E402

log = logging.getLogger("psn-alert")


def setup_logging() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        handlers.append(logging.FileHandler("runner.log", encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )
    # Never let HTTP library debug logging print request URLs (they contain
    # the Telegram token in the path).
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def gh_notice(message: str) -> None:
    """Emit a GitHub Actions annotation when running inside a workflow."""
    if os.getenv("GITHUB_ACTIONS") == "true":
        print(f"::notice::{message}")


def gh_warning(message: str) -> None:
    if os.getenv("GITHUB_ACTIONS") == "true":
        print(f"::warning::{message}")


def gh_error(message: str) -> None:
    if os.getenv("GITHUB_ACTIONS") == "true":
        print(f"::error::{message}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="One-shot PSN gift card monitoring run")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and evaluate but send no Telegram messages "
                             "and write no state file")
    parser.add_argument("--report", action="store_true",
                        help="Force the daily availability report this run")
    parser.add_argument("--config", default=None, help="Path to products.yaml")
    parser.add_argument("--state", default=None, help="Path to the state JSON file")
    parser.add_argument("--delay", type=float, default=None,
                        help="Seconds to sleep between product requests")
    return parser.parse_args(argv)


def report_due(state: dict, now: datetime, report_hour: int) -> bool:
    """True when the daily report has not yet been sent today.

    Checked on every run rather than only at the scheduled minute, so a delayed
    or skipped cron tick still produces exactly one report per day.
    """
    if now.hour < report_hour:
        return False
    return state.get("last_daily_report_date") != now.strftime("%Y-%m-%d")


def observe_cached(product: dict, cache: dict[str, str | BaseException]) -> tuple[dict, bool]:
    """Observe a product, fetching its URL at most once per cycle.

    Returns ``(observation, fetched)``. ``fetched`` is True only when this call
    issued a network request, so the caller can pace requests without sleeping
    between products that reused an earlier fetch.

    Failures are cached as well as pages: a host which is already down should
    be attempted once per cycle rather than once per product. That distinction
    matters because a hanging host costs a full timeout per attempt, so
    retrying per product would multiply a stalled cycle by the number of
    products sharing that URL.
    """
    url = str(product.get("url") or "")
    if not url:
        return core.observe(product), False

    if url in cache:
        fetched = False
    else:
        try:
            cache[url] = core.fetch_html(url)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            cache[url] = exc
        fetched = True

    cached = cache[url]
    if isinstance(cached, BaseException):
        return core.fetch_failed_observation(cached), fetched
    return core.observe(product, html=cached), fetched


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = parse_args(argv)

    tz_name = os.getenv("TIMEZONE", core.DEFAULT_TIMEZONE)
    try:
        tz: ZoneInfo = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        log.error("Unknown TIMEZONE %r; falling back to %s", tz_name, core.DEFAULT_TIMEZONE)
        tz = ZoneInfo(core.DEFAULT_TIMEZONE)

    report_hour = int(os.getenv("DAILY_REPORT_HOUR", str(core.DEFAULT_REPORT_HOUR)))
    delay = args.delay
    if delay is None:
        delay = float(os.getenv("REQUEST_DELAY_SECONDS", "2"))

    try:
        products = core.load_products(args.config)
    except Exception as exc:  # noqa: BLE001
        gh_error(f"Could not load product config: {exc}")
        log.error("Could not load product config: %s", exc)
        return 1

    if not products:
        gh_warning("No products configured in products.yaml")
        log.warning("No products configured; nothing to monitor")
        return 0

    active = core.enabled_products(products)
    log.info("Loaded %d product(s), %d enabled", len(products), len(active))
    if not active:
        gh_warning("products.yaml has no enabled products")
        log.warning("No enabled products. Set enabled: true on verified entries.")
        return 0

    state = core.load_state(args.state)
    now = datetime.now(tz)
    now_text = now.isoformat(timespec="seconds")

    # ------------------------------------------------------------------ #
    # Claim a Telegram chat if nobody has been linked yet.
    # ------------------------------------------------------------------ #
    token = core.telegram_token()
    if not token:
        gh_warning("TELEGRAM_BOT_TOKEN is not set; notifications are disabled")
        log.warning("TELEGRAM_BOT_TOKEN is not set. Add it under "
                    "Settings -> Secrets and variables -> Actions.")
    if token and not state.get("chat_id"):
        claimed = core.claim_chat(token)
        if claimed:
            state["chat_id"] = claimed
            log.info("Linked Telegram chat id ending ...%s", claimed[-4:])
            # Send a confirmation so pairing is provable from Telegram itself
            # rather than only from the Actions log.
            hello = (
                "✅ *PSN gift card monitor linked*\n\n"
                f"Monitoring {len(active)} product(s). You will be alerted when "
                "a card comes back in stock, drops in price, or shows a "
                "cashback offer.\n\n"
                "A daily availability report arrives at 09:00 IST."
            )
            if core.send_message(token, claimed, hello):
                log.info("Pairing confirmation delivered")
            else:
                log.warning("Pairing confirmation failed to send")
        else:
            log.info("No pending /start. Send /start to the bot, then re-run.")

    chat_id = str(state.get("chat_id") or "")
    if token and not chat_id:
        gh_warning("No Telegram chat linked yet - send /start to your bot")

    # ------------------------------------------------------------------ #
    # Monitor every enabled product.
    # ------------------------------------------------------------------ #
    entries = state.setdefault("products", {})
    all_alerts: list[core.Alert] = []
    failures = 0

    # Several entries deliberately share one URL: Rooter's five variants live
    # on a single page, as do StanShop's four. Fetching per product would hit
    # the same host repeatedly for no new information, so each URL is fetched
    # at most once per cycle. With checks running every few minutes this keeps
    # our per-host request rate down to one request per interval.
    html_cache: dict[str, str | BaseException] = {}

    for index, product in enumerate(active, start=1):
        pid = str(product.get("id") or f"product-{index}")
        observation, fetched = observe_cached(product, html_cache)
        evaluation = core.evaluate(product, entries.get(pid), observation, now_text)
        entries[pid] = evaluation.entry

        status = evaluation.entry.get("status")
        if observation.get("ok"):
            price = evaluation.entry.get("price")
            price_text = f" ₹{price:,.0f}" if isinstance(price, (int, float)) else ""
            log.info("[%d/%d] %s: %s%s", index, len(active), pid, status, price_text)
            if evaluation.entry.get("note"):
                log.info("      note: %s", evaluation.entry["note"])
        else:
            failures += 1
            log.warning("[%d/%d] %s: FAILED (%s)", index, len(active), pid,
                        observation.get("error", "unknown"))
            gh_warning(f"{pid}: could not be read ({observation.get('error', '')[:120]})")

        for alert in evaluation.alerts:
            log.info("      ALERT %s", alert.type)
            all_alerts.append(alert)
            core.record_event(state, pid, alert.type, alert.message, now_text)
            if not args.dry_run:
                if chat_id:
                    core.send_message(token, chat_id, alert.message)
                else:
                    log.warning("Alert generated but no chat is linked yet; not sent")

        # Pace only real network requests. Products that reused a cached page
        # cost no bandwidth, so sleeping after them would just slow the cycle.
        if fetched and delay and index < len(active):
            time.sleep(delay)

    # ------------------------------------------------------------------ #
    # Daily availability report.
    # ------------------------------------------------------------------ #
    send_report = args.report or report_due(state, now, report_hour)
    if send_report:
        if args.dry_run:
            log.info("Dry run: daily report suppressed")
        elif not chat_id:
            # Do not retry forever when no chat is linked; record it as handled
            # so the log does not fill with repeated attempts.
            log.warning("Daily report due but no chat is linked; skipping send")
            state["last_daily_report_date"] = now.strftime("%Y-%m-%d")
        else:
            text = core.build_report(
                products, state, now_text, "Daily PSN gift card availability report"
            )
            if core.send_message(token, chat_id, text):
                state["last_daily_report_date"] = now.strftime("%Y-%m-%d")
                log.info("Daily report sent")
            else:
                log.warning("Daily report send failed; will retry next run")
    elif now.hour < report_hour:
        # Distinguish "not due yet" from "already sent": report_due() returns
        # False for both, and claiming it was already sent would be a lie.
        log.info("Daily report not due yet; next window is %02d:00 %s",
                 report_hour, tz_name)
    else:
        log.info("Daily report already sent for %s", now.strftime("%Y-%m-%d"))

    # ------------------------------------------------------------------ #
    # Persist state.
    # ------------------------------------------------------------------ #
    if args.dry_run:
        log.info("Dry run: state not written")
    else:
        try:
            saved = core.save_state(state, args.state)
            log.info("State saved to %s", saved)
        except Exception as exc:  # noqa: BLE001
            gh_error(f"Could not save state: {exc}")
            log.error("Could not save state: %s", exc)
            return 1

    # ------------------------------------------------------------------ #
    # Summary.
    # ------------------------------------------------------------------ #
    statuses: dict[str, int] = {}
    for pid in entries:
        value = str(entries[pid].get("status") or core.UNKNOWN)
        statuses[value] = statuses.get(value, 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(statuses.items()))
    log.info("Cycle complete: %d alert(s), failures=%d, %s",
             len(all_alerts), failures, summary or "no state")

    if failures and failures == len(active):
        gh_error(f"All {failures} enabled product(s) failed to load. "
                 "The retailer may be blocking requests or the page changed.")
        log.error("Every enabled product failed - check blocking/robots/page changes")
        return 2

    gh_notice(f"Cycle complete: {len(all_alerts)} alert(s), {failures} failure(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
