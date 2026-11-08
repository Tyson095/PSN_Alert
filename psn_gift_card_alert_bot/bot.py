"""Optional long-running local process.

GitHub Actions runs ``github_runner.py`` once and exits, so this file is **not**
needed for monitoring. It exists for local use, and specifically so you can
pair the bot with ``/start`` while sitting at your machine.

It shares ``core`` with the GitHub Actions runner, so both paths use exactly
the same detection logic and the same ``state/state.json``. Do not run this and
the scheduled workflow at the same time -- two Telegram pollers will conflict
(Telegram returns HTTP 409) and the two processes would race on the state file.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

try:  # Works both as `python bot.py` and as `python -m psn_gift_card_alert_bot.bot`.
    from . import core
except ImportError:  # pragma: no cover - script execution
    import core  # type: ignore[no-redef]

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")],
)
log = logging.getLogger("psn-alert")
logging.getLogger("urllib3").setLevel(logging.WARNING)

TOKEN = core.telegram_token()
TZ = core.get_timezone()
INTERVAL = max(60, int(os.getenv("CHECK_INTERVAL_SECONDS", "1800")))
REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", str(core.DEFAULT_REPORT_HOUR)))
REPORT_MINUTE = int(os.getenv("DAILY_REPORT_MINUTE", "0"))

# Set only if you want a fixed destination instead of pairing via /start.
CHAT_ID_OVERRIDE = os.getenv("TELEGRAM_CHAT_ID", "").strip()

_state_lock = threading.Lock()


def get_chat_id(state: dict) -> str:
    return CHAT_ID_OVERRIDE or str(state.get("chat_id") or "")


def send(state: dict, text: str) -> bool:
    chat_id = get_chat_id(state)
    if not chat_id:
        log.info("No Telegram chat linked yet. Open the bot and send /start.")
        return False
    return core.send_message(TOKEN, chat_id, text)


# --------------------------------------------------------------------------- #
# Telegram polling: pairing and simple commands
# --------------------------------------------------------------------------- #
def telegram_poll() -> None:
    """Accept /start and basic commands; the first private chat pairs the bot."""
    offset = None
    while True:
        try:
            params: dict = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            response = requests.get(
                f"{core.TELEGRAM_API}/bot{TOKEN}/getUpdates",
                params=params, timeout=35,
            )
            payload = response.json() if response.content else {}
            if not payload.get("ok"):
                # 409 means another poller (e.g. a stray github_runner --report)
                # is holding the updates. Wait and retry rather than spin.
                log.warning("getUpdates rejected: %s",
                            core.scrub(str(payload.get("description") or ""), TOKEN))
                time.sleep(10)
                continue

            for update in payload.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message") or update.get("edited_message")
                if not message or not message.get("text"):
                    continue
                chat = message.get("chat") or {}
                if chat.get("type") != "private":
                    continue

                chat_id = str(chat.get("id") or "")
                command = message["text"].strip().split()[0].lower()

                with _state_lock:
                    state = core.load_state()
                    existing = get_chat_id(state)

                    if not existing and command == "/start":
                        state["chat_id"] = chat_id
                        core.save_state(state)
                        send(state, "✅ PSN Gift Card Alert Bot connected.\n"
                                    "Use /status, /report or /help.")
                        continue
                    if existing and chat_id != existing:
                        core.send_message(
                            TOKEN, chat_id,
                            "This bot is already linked to another private chat.")
                        continue
                    if command == "/start":
                        send(state, "Bot is active. Use /status, /report or /help.")
                    elif command == "/help":
                        send(state, "Commands:\n/status — latest monitored status\n"
                                    "/report — availability summary\n/help — this message")
                    elif command == "/status":
                        send(state, core.build_report(
                            core.load_products(), state, core.now_iso(TZ),
                            "Current product status"))
                    elif command == "/report":
                        send(state, core.build_report(
                            core.load_products(), state, core.now_iso(TZ),
                            "PSN gift card availability report"))
        except Exception as exc:  # noqa: BLE001 - polling must survive anything
            log.warning("Telegram polling error: %s: %s", type(exc).__name__,
                        core.scrub(str(exc), TOKEN))
            time.sleep(5)


# --------------------------------------------------------------------------- #
# Monitoring cycle (identical logic to github_runner.py)
# --------------------------------------------------------------------------- #
def monitor_all() -> None:
    products = core.enabled_products(core.load_products())
    if not products:
        log.warning("No enabled products in config/products.yaml")
        return

    with _state_lock:
        state = core.load_state()

    now = core.now_iso(TZ)
    entries = state.setdefault("products", {})
    alerts: list[core.Alert] = []

    for product in products:
        pid = str(product.get("id") or "")
        observation = core.observe(product)
        evaluation = core.evaluate(product, entries.get(pid), observation, now)
        entries[pid] = evaluation.entry

        if observation.get("ok"):
            price = evaluation.entry.get("price")
            price_text = f" ₹{price:,.0f}" if isinstance(price, (int, float)) else ""
            log.info("%s: %s%s", pid, evaluation.entry.get("status"), price_text)
        else:
            log.warning("%s: failed (%s)", pid, observation.get("error"))

        for alert in evaluation.alerts:
            log.info("ALERT %s for %s", alert.type, pid)
            alerts.append(alert)
            core.record_event(state, pid, alert.type, alert.message, now)
            send(state, alert.message)

        time.sleep(float(os.getenv("REQUEST_DELAY_SECONDS", "2")))

    with _state_lock:
        core.save_state(state)
    log.info("Cycle complete: %d alert(s)", len(alerts))


def daily_report() -> None:
    with _state_lock:
        state = core.load_state()
    text = core.build_report(core.load_products(), state, core.now_iso(TZ),
                             "Daily PSN gift card availability report")
    send(state, text)
    state["last_daily_report_date"] = datetime.now(TZ).strftime("%Y-%m-%d")
    with _state_lock:
        core.save_state(state)


def main() -> None:
    if not TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env before starting. "
                         "For GitHub Actions, store it as an Actions secret instead.")

    log.info("Local bot starting. Interval=%ss, timezone=%s", INTERVAL, TZ)
    log.info("State file: %s", core.state_path())
    log.info("Open your bot in Telegram and send /start.")

    threading.Thread(target=telegram_poll, daemon=True).start()

    scheduler = BackgroundScheduler(timezone=TZ)
    scheduler.add_job(monitor_all, "interval", seconds=INTERVAL, id="monitor",
                      max_instances=1, coalesce=True,
                      next_run_time=datetime.now(TZ))
    scheduler.add_job(daily_report, "cron", hour=REPORT_HOUR, minute=REPORT_MINUTE,
                      id="daily_report", max_instances=1)
    scheduler.start()

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        scheduler.shutdown()
        log.info("Stopped.")


if __name__ == "__main__":
    main()
