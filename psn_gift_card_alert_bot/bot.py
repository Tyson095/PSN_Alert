from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")],
)
log = logging.getLogger("psn-alert")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))
INTERVAL = max(60, int(os.getenv("CHECK_INTERVAL_SECONDS", "60")))
REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "9"))
REPORT_MINUTE = int(os.getenv("DAILY_REPORT_MINUTE", "0"))
DB_PATH = Path(os.getenv("DATABASE_PATH", "data/alerts.sqlite3"))
CONFIG_PATH = Path("config/products.yaml")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PersonalStockAlert/1.0; +personal-use)",
    "Accept-Language": "en-IN,en;q=0.9",
}

import sqlite3
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS snapshots (
            product_id TEXT PRIMARY KEY, status TEXT, price REAL, discount TEXT,
            cashback TEXT, checked_at TEXT, last_error TEXT
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, product_id TEXT, event_type TEXT,
            message TEXT, created_at TEXT
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT
        )""")

def get_chat_id():
    global CHAT_ID
    if CHAT_ID:
        return CHAT_ID
    with db() as con:
        row = con.execute("SELECT value FROM settings WHERE key='chat_id'").fetchone()
    return row["value"] if row else ""

def save_chat_id(chat_id):
    global CHAT_ID
    CHAT_ID = str(chat_id)
    with db() as con:
        con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('chat_id',?)", (CHAT_ID,))

def tg(method, payload=None):
    if not TOKEN:
        log.warning("Telegram token missing; notification skipped.")
        return None
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/{method}",
            json=payload or {}, timeout=20
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            log.error("Telegram API error: %s", data)
        return data
    except requests.RequestException as exc:
        log.exception("Telegram request failed: %s", exc)
        return None

def send(text):
    chat_id = get_chat_id()
    if not chat_id:
        log.info("No Telegram chat linked yet. Send /start to your bot.")
        return
    # Telegram message limit is 4096 characters.
    tg("sendMessage", {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True})

def telegram_poll():
    """Accept /start and basic commands; first private chat claims an unconfigured bot."""
    offset = None
    while True:
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                             params=params, timeout=35)
            r.raise_for_status()
            for update in r.json().get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message") or update.get("edited_message")
                if not msg or not msg.get("text"):
                    continue
                chat = msg["chat"]
                if chat.get("type") != "private":
                    continue
                text = msg["text"].strip().split()[0].lower()
                chat_id = str(chat["id"])
                existing = get_chat_id()
                if not existing and text == "/start":
                    save_chat_id(chat_id)
                    send("✅ PSN Gift Card Alert Bot connected.\nUse /status, /report or /help.")
                elif existing and chat_id != existing:
                    tg("sendMessage", {"chat_id": chat_id, "text": "This bot is already linked to another private chat."})
                elif text == "/start":
                    send("Bot is active. Use /status, /report or /help.")
                elif text == "/help":
                    send("Commands:\n/status — latest monitored status\n/report — availability summary\n/help — this message")
                elif text == "/status":
                    send(build_report(title="Current product status"))
                elif text == "/report":
                    send(build_report(title="PSN gift card availability report"))
        except Exception as exc:
            log.exception("Telegram polling error: %s", exc)
            time.sleep(5)

def load_products():
    if not CONFIG_PATH.exists():
        return []
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f).get("products", [])

def parse_money(text):
    if not text:
        return None
    cleaned = text.replace(",", "")
    match = re.search(r"(?:₹|Rs\.?|INR)\s*([0-9]+(?:\.[0-9]{1,2})?)|([0-9]+(?:\.[0-9]{1,2})?)\s*(?:₹|INR)", cleaned, re.I)
    if not match:
        return None
    return float(match.group(1) or match.group(2))

def selected_text(soup, selector):
    if not selector:
        return ""
    el = soup.select_one(selector)
    return el.get_text(" ", strip=True) if el else ""

def fetch_product(p):
    response = requests.get(p["url"], headers=HEADERS, timeout=25)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    page_text = soup.get_text(" ", strip=True).lower()
    availability_text = selected_text(soup, p.get("availability_selector", "")).lower()
    if not availability_text:
        availability_text = page_text

    out_patterns = [x.lower() for x in p.get("out_of_stock_patterns", [])]
    in_patterns = [x.lower() for x in p.get("in_stock_patterns", [])]

    # Out-of-stock wins if both phrases occur somewhere on a page. For variant pages,
    # configure a selector that scopes text to the selected denomination.
    if any(x in availability_text for x in out_patterns):
        status = "OUT_OF_STOCK"
    elif any(x in availability_text for x in in_patterns):
        status = "IN_STOCK"
    else:
        status = "UNKNOWN"

    price_text = selected_text(soup, p.get("price_selector", ""))
    if not price_text:
        price_text = ""
    discount = selected_text(soup, p.get("discount_selector", "")) or ""
    cashback = selected_text(soup, p.get("cashback_selector", "")) or ""
    return {
        "status": status,
        "price": parse_money(price_text),
        "discount": discount[:250],
        "cashback": cashback[:250],
        "error": "",
    }

def record_event(product_id, event_type, message):
    with db() as con:
        con.execute(
            "INSERT INTO events(product_id,event_type,message,created_at) VALUES(?,?,?,?)",
            (product_id, event_type, message, datetime.now(TZ).isoformat())
        )

def monitor_one(p):
    if not p.get("enabled", False):
        return
    pid = p["id"]
    try:
        result = fetch_product(p)
        now = datetime.now(TZ).isoformat()
        with db() as con:
            old = con.execute("SELECT * FROM snapshots WHERE product_id=?", (pid,)).fetchone()
            old_status = old["status"] if old else None
            old_price = old["price"] if old else None
            old_discount = old["discount"] if old else None
            old_cashback = old["cashback"] if old else None
            con.execute("""INSERT OR REPLACE INTO snapshots
                (product_id,status,price,discount,cashback,checked_at,last_error)
                VALUES(?,?,?,?,?,?,?)""",
                (pid, result["status"], result["price"], result["discount"],
                 result["cashback"], now, ""))
        name = f'{p["retailer"]} PSN ₹{p["denomination"]}'
        link = p["url"]

        # First successful observation establishes baseline; it does not create a false restock alert.
        if old_status and old_status != "IN_STOCK" and result["status"] == "IN_STOCK":
            msg = f"🟢 BACK IN STOCK\n{name}\nLink: {link}"
            if result["price"] is not None:
                msg += f'\nPrice: ₹{result["price"]:,.2f}'
            send(msg); record_event(pid, "RESTOCK", msg)

        if old_price is not None and result["price"] is not None and result["price"] < old_price:
            msg = f"🔻 PRICE DROP\n{name}\n₹{old_price:,.2f} → ₹{result['price']:,.2f}\nLink: {link}"
            send(msg); record_event(pid, "PRICE_DROP", msg)

        if result["discount"] and result["discount"] != (old_discount or ""):
            msg = f"🏷️ DISCOUNT / OFFER UPDATE\n{name}\n{result['discount']}\nLink: {link}"
            send(msg); record_event(pid, "DISCOUNT", msg)

        if result["cashback"] and result["cashback"] != (old_cashback or ""):
            msg = f"💰 CASHBACK UPDATE\n{name}\n{result['cashback']}\nLink: {link}"
            send(msg); record_event(pid, "CASHBACK", msg)

        log.info("%s: %s", pid, result["status"])
    except Exception as exc:
        log.warning("Monitor failed for %s: %s", pid, exc)
        with db() as con:
            old = con.execute("SELECT * FROM snapshots WHERE product_id=?", (pid,)).fetchone()
            if old:
                con.execute("UPDATE snapshots SET last_error=?,checked_at=? WHERE product_id=?",
                            (str(exc)[:500], datetime.now(TZ).isoformat(), pid))
            else:
                con.execute("""INSERT INTO snapshots(product_id,status,checked_at,last_error)
                    VALUES(?,?,?,?)""", (pid, "UNKNOWN", datetime.now(TZ).isoformat(), str(exc)[:500]))

def monitor_all():
    products = [p for p in load_products() if p.get("enabled", False)]
    log.info("Checking %d enabled products", len(products))
    for p in products:
        monitor_one(p)
        # Small spacing prevents a burst of simultaneous requests.
        time.sleep(2)

def build_report(title="PSN availability report"):
    products = load_products()
    lines = [f"🎮 {title}", datetime.now(TZ).strftime("%d %b %Y, %I:%M %p IST"), ""]
    enabled = [p for p in products if p.get("enabled", False)]
    if not enabled:
        lines.append("No products enabled yet. Add product URLs/selectors and set enabled: true.")
        return "\n".join(lines)
    with db() as con:
        rows = {r["product_id"]: r for r in con.execute("SELECT * FROM snapshots")}
    for p in enabled:
        row = rows.get(p["id"])
        status = row["status"] if row else "NOT CHECKED"
        price = f' · ₹{row["price"]:,.2f}' if row and row["price"] is not None else ""
        lines.append(f'{p["retailer"]} · ₹{p["denomination"]}: {status}{price}')
        if row and row["last_error"]:
            lines.append(f'  Last error: {row["last_error"][:120]}')
        lines.append(p["url"])
    return "\n".join(lines)

def daily_report():
    send(build_report("Daily PSN gift card availability report"))

def main():
    if not TOKEN or TOKEN == "123456789:replace_me":
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env before starting.")
    init_db()
    threading.Thread(target=telegram_poll, daemon=True).start()
    scheduler = BackgroundScheduler(timezone=TZ)
    scheduler.add_job(monitor_all, "interval", seconds=INTERVAL, id="monitor",
                      max_instances=1, coalesce=True, next_run_time=datetime.now(TZ))
    scheduler.add_job(daily_report, "cron", hour=REPORT_HOUR, minute=REPORT_MINUTE,
                      id="daily_report", max_instances=1)
    scheduler.start()
    log.info("Bot running. Check interval=%ss, timezone=%s", INTERVAL, TZ)
    log.info("Open your bot in Telegram and send /start.")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        scheduler.shutdown()
        log.info("Stopped.")

if __name__ == "__main__":
    main()
