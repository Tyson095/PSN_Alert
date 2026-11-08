# PSN Gift Card Alert Bot

A Telegram alert bot that watches Indian retailers for PlayStation gift cards
(₹1,000 / ₹2,000 / ₹3,000 / ₹4,000 / ₹5,000) and notifies you about:

- **Back in stock** (including `OUT_OF_STOCK → UNKNOWN → IN_STOCK`)
- **Price drops**
- **Discount / cashback text changes**
- **A daily availability report** at 09:00 Asia/Kolkata

It runs as a **one-shot job on GitHub Actions**: start, check each enabled
product once, send any notifications, write state, exit. No server, no
long-lived process, no database — state lives in the committed JSON file
`state/state.json`.

Detection logic is shared by both entry points in
`psn_gift_card_alert_bot/core.py`, so local runs and CI runs behave identically.

---

## Repository layout

| Path | Purpose |
| --- | --- |
| `github_runner.py` | CI entry point: one monitoring cycle, then exit |
| `.github/workflows/monitor.yml` | Schedule, secrets, state persistence |
| `psn_gift_card_alert_bot/core.py` | Config, state, extractors, alert rules, Telegram |
| `psn_gift_card_alert_bot/bot.py` | Optional local long-running process (pairing, `/status`) |
| `psn_gift_card_alert_bot/config/products.yaml` | Product list; `enabled: true/false` |
| `state/state.json` | Persisted observations, chat id, last report date (committed) |
| `tests/test_core.py` | 71 tests for state, transitions, dedup, extractors |
| `requirements.txt` | Runtime dependencies (canonical) |
| `requirements-dev.txt` | Test dependencies (`pytest`) |

---

## Setup

### 1. Create the Telegram bot (once)

1. Open Telegram, talk to `@BotFather`, send `/newbot`, follow the prompts.
2. Copy the **bot token**.
3. Open your new bot and send `/start` — this links the bot to your private
   chat. The runner picks it up on its next cycle (it reads the pending
   `/start` via `getUpdates` and stores only the chat id in
   `state/state.json`).

> **Never paste the token into a chat, an issue, or a commit.** It is read
> only from the environment variable `TELEGRAM_BOT_TOKEN`, and every log line
> and error message is scrubbed through `core.scrub()` before it is written.

### 2. Add the GitHub secret (once)

1. Open the repository on GitHub → **Settings** → **Secrets and variables** →
   **Actions**.
2. Click **New repository secret**.
3. Name: `TELEGRAM_BOT_TOKEN`
4. Value: the token from BotFather (type it in the web form; it is not stored
   in the repository and never appears in logs).
5. Click **Add secret**.

The workflow reads it as `${{ secrets.TELEGRAM_BOT_TOKEN }}` and passes it to
the process via the environment. It is never echoed, never written to
`state/state.json`, and never printed.

### 3. Enable scheduled runs

After the workflow file is on the default branch, GitHub enables the
`schedule` trigger automatically. You can also start a run manually:

**Actions → PSN Gift Card Monitor → Run workflow**

---

## Schedule (and why it is not exact)

```yaml
schedule:
  - cron: '7,37 * * * *'   # every 30 minutes
  - cron: '30 3 * * *'     # 09:00 Asia/Kolkata = 03:30 UTC
```

- Cron runs in **UTC**. Asia/Kolkata is UTC+05:30 with **no daylight saving
  time**, so 09:00 IST is exactly 03:30 UTC.
- **GitHub scheduled runs are not on time.** GitHub queues scheduled
  workflows on busy minutes and routinely delays them by 5–30 minutes,
  occasionally much longer. Treat the interval as "roughly every 30 minutes",
  not a guarantee.
- To reduce queuing, the 30-minute ticks use `:07` and `:37` instead of the
  heavily loaded `:00` and `:30`.
- Because a tick may be delayed or skipped, the runner checks **on every
  cycle** whether the daily report is still due for today. Result: exactly
  one report per day, even if the 09:00 tick runs at 09:20 or is missed
  entirely (the next cycle sends it).

---

## Running locally (optional)

Local runs are only needed to pair the bot or to debug a selector. Normal
monitoring happens on GitHub Actions.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Put the token in a local `.env` (this file is gitignored and must never be
committed):

```
TELEGRAM_BOT_TOKEN=123456:ABC-your-token
```

Then:

```powershell
# One cycle, no notifications, no state write (safe to test anywhere)
python github_runner.py --dry-run

# One real cycle (sends alerts, writes state/state.json)
python github_runner.py

# Force the daily report right now
python github_runner.py --report

# Optional: long-running local bot with /start, /status, /report, /help
python -m psn_gift_card_alert_bot.bot
```

**Do not run `bot.py` and the scheduled workflow at the same time.** Two
Telegram pollers conflict (HTTP 409) and the two processes would race on the
state file.

Exit codes for `github_runner.py`: `0` cycle done (individual product
failures are warnings), `1` fatal config/state error, `2` every enabled
product failed (likely blocking or a page change).

---

## Configuration

Edit `psn_gift_card_alert_bot/config/products.yaml`:

| Field | Meaning |
| --- | --- |
| `id` | Stable unique id (state key — do not change it later) |
| `retailer` / `denomination` | Labels used in alerts and reports |
| `url` | Exact product/variant URL |
| `availability_selector` | CSS selector for the stock text of *that* variant |
| `price_selector` | CSS selector for the price |
| `discount_selector` / `cashback_selector` | Optional offer text selectors |
| `in_stock_patterns` / `out_of_stock_patterns` | Phrases matched against the selected text |
| `enabled` | `true` only after the URL and selectors are verified |

Safety rules that are enforced by the code, not by convention:

- An observation that cannot be read is recorded as `UNKNOWN` and **never**
  counts as out of stock, and never triggers an alert by itself.
- The first successful observation is a **baseline**: it records state and
  raises nothing, so the first run cannot burst false alerts.
- `RESTOCK` fires only when the previous *known* state was `OUT_OF_STOCK`.
  A baseline discovery (`UNKNOWN → IN_STOCK`) is not reported as a restock.
- Duplicate alerts are suppressed while the state stays the same.

---

## Retailer coverage (verified 2026-11)

| Retailer | Status | Notes |
| --- | --- | --- |
| **Rooter** (`shop.rooter.gg`) | ✅ **Enabled, verified** | Server-rendered Next.js page; `productData.baseVariations` carries all five variants with per-variant `outOfStock`, `sellPrice`, `proSellPrice` and cashback. `robots.txt` allows crawling. 5 entries enabled. |
| **Amazon India** | ❌ Disabled | Product/search pages return HTTP **503** to non-browser clients (bot protection). Needs the official **Creators API** (PA-API 5 is deprecated) plus Associates credentials. Do not attempt to bypass the blocking. |
| **Games The Shop** | ❌ Disabled | Product data is fetched client-side from `/api/`, which `robots.txt` explicitly `Disallow`s. Respect it; needs an official feed/API. |
| **Paytm** | ❌ Disabled | Search returns a generic marketing page; no server-rendered listings. |
| **Snapdeal** | ❌ Disabled | Search returns unrelated results; no genuine PSN listings found. |
| **Flipkart / Croma / Vijay Sales** | ⬜ Unverified | Left in the config as documented placeholders. |

### What still needs attention

These are the entries that must stay disabled until someone verifies a real
URL and selectors (open the page, DevTools → right-click → *Copy selector*,
test against the live page):

1. **Amazon India** — blocked (503). Only an official Creators API adapter
   can enable this.
2. **Games The Shop** — needs an official data source; its `/api/` path is
   disallowed by robots.txt.
3. **Paytm** — needs a server-rendered or official listing URL.
4. **Snapdeal / Flipkart / Croma / Vijay Sales** — candidate URLs and
   denomination-scoped selectors have not been verified at all.
5. For any page that lists several denominations at once, the selector must
   be scoped to the exact denomination — otherwise "out of stock" for one
   variant can be read as stock for another.

No working product URLs or selectors were invented. Only the five Rooter
entries are enabled because they were checked against the live page.

---

## Terms, robots and rate limits

- One request per product per cycle, with a configurable
  `REQUEST_DELAY_SECONDS` (default 2s) between requests — about 48 cycles/day.
- No CAPTCHA solving, no authentication bypass, no anti-bot evasion. If a
  retailer blocks the bot or returns 429/403, the correct response is to slow
  down or disable that entry, not to work around it.
- Unknown/blocked results are recorded as `UNKNOWN` and reported as such.

---

## Tests

```powershell
pip install -r requirements-dev.txt
python -m pytest
```

Covers state persistence, stock transitions (including
`OUT → UNKNOWN → IN`), price drops, duplicate suppression, Rooter and generic
HTML extractors, safety rules (unknown never counts as in/out of stock),
report scheduling, money parsing and token scrubbing.

---

## Troubleshooting

- **No alerts** — confirm the secret exists, send `/start` to the bot, check
  the Actions log for `Linked Telegram chat id ending ...xxxx`.
- **`UNKNOWN` status** — the page did not contain a recognized phrase or the
  selector changed. This is safer than a false positive; inspect the selector.
- **Exit code 2** — every enabled product failed: the retailer is likely
  blocking the runner or the page changed.
- **State push warnings** — the run still completed; the next run recomputes
  state from the repository.
- **Missed/delayed runs** — expected GitHub behaviour for scheduled
  workflows; see the schedule section above.

## Before going live with more retailers

Test each new entry manually against the live page, confirm at least one
known in-stock and one known out-of-stock state, then set `enabled: true`.
This is an alert tool only — it never logs in, adds to cart, or purchases.
