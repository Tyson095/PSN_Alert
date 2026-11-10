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
| `tests/test_core.py` | 110 tests for state, transitions, dedup, extractors |
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

## Schedule (and how the 5-minute loop actually runs)

```yaml
workflow_dispatch:             # fired from outside, every 5 minutes
schedule:
  - cron: '7,37 * * * *'       # fallback: GitHub's own scheduler
  - cron: '30 3 * * *'         # 09:00 Asia/Kolkata = 03:30 UTC
```

### Why GitHub's own scheduler is not the main trigger

Measured from this repository's own run history:

| gap between scheduled runs | observed |
|---|---|
| 01:55 → 05:28 | 3.5 h |
| 05:28 → 11:41 | 6.0 h |
| 11:41 → 17:29 | 5.8 h |
| 17:29 → 22:19 | 4.8 h |
| 22:19 → next | nothing for 2+ h |

The `30 minutes` written in the YAML is not what happens — GitHub queues
scheduled workflows on busy minutes and delays them by hours, sometimes
skipping ticks entirely. By contrast a `workflow_dispatch` sent at 00:38:48
had its run created at **00:38:50**: two seconds.

That difference decides whether the bot is useful. The ₹2,000 and ₹4,000
cards sell out in 10–15 minutes; against a five-hour check the chance of
catching one is about 1 in 20, against a five-minute check it is effectively
certain.

### The external trigger

An outside scheduler POSTs to the dispatch endpoint every five minutes.

**Endpoint**

```
POST https://api.github.com/repos/Tyson095/PSN_Alert/actions/workflows/411251017/dispatches
```

**Headers**

```
Authorization: Bearer <PAT>
Accept: application/vnd.github+json
Content-Type: application/json
X-GitHub-Api-Version: 2022-11-28
```

**Body**

```json
{"ref":"refs/heads/main"}
```

**Success is `204 No Content`.** GitHub's own documentation says `200`; the
live endpoint returns `204`, which is what any health check must expect.

#### Step 1 — create a token (once)

1. GitHub → Settings → Developer settings → Personal access tokens →
   **Fine-grained tokens** → Generate new token
2. **Repository access → Only select repositories → PSN_Alert.** A
   fine-grained token must name the repository even though it is public, or
   every call returns `404`.
3. **Permissions → Repository permissions → Actions → Read and write.**
4. Pick an expiry you will remember. **When it expires the schedule stops
   silently** — the only sign is a `401` in the cron history.

That is the entire scope. It cannot read or write code, cannot see secrets,
and does nothing outside this one repository.

#### Step 2 — create the job (once)

[cron-job.org](https://www.cron-job.org) is free, accepts arbitrary custom
headers and POST bodies, and allows up to 60 executions/hour — this needs 12.

| Field | Value |
|---|---|
| URL | `https://api.github.com/repos/Tyson095/PSN_Alert/actions/workflows/411251017/dispatches` |
| Method | `POST` |
| Schedule | `*/5 * * * *` |
| Body | `{"ref":"refs/heads/main"}` |
| Header `Authorization` | `Bearer <PAT>` |
| Header `Accept` | `application/vnd.github+json` |
| Header `Content-Type` | `application/json` |

Press **Test run** — it must return `204`. Then open the **Actions** tab and
confirm a run started.

#### Step 3 — confirm it is really on time

cron-job.org keeps two days of execution history with response codes:

| Code | Meaning |
|---|---|
| `204` every 5 minutes | healthy |
| `401` | token expired, or wrong scope |
| `404` | token cannot see the repo (repository not selected) |
| `403` | token lacks Actions: write |

The second check is the run list itself: **Actions → PSN Gift Card Monitor**
should show a dispatch roughly every five minutes.

### What a cycle costs

- **Retailers:** one request per distinct URL — 7 requests across 3 hosts, so
  **12 requests/hour per host**, with a 2-second pause between real requests.
  Entries that share a page (Rooter's five variants, StanShop's four) are
  fetched once, not once each.
- **GitHub:** the repository is public, so Actions minutes are unlimited. A
  cycle takes about 30 seconds, comfortably inside a 5-minute window.
- Nothing here bypasses CAPTCHA, authentication or anti-bot protection.

### Fallback

The `schedule` triggers stay. If the external job dies, monitoring degrades to
GitHub's slow cadence instead of stopping outright. Duplicate alerts are
impossible either way: a restock is only recorded when the previous known
status was `OUT_OF_STOCK`.

Because `report_due` is evaluated on **every** cycle, the daily report also
lands within five minutes of 09:00 IST instead of whenever GitHub's scheduler
gets around to it.

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
| `extractor` | `rooter_rsc`, `html_text` or `jsonld_offers` |
| `availability_selector` | CSS selector for the stock text of *that* variant |
| `price_selector` | CSS selector for the price |
| `discount_selector` / `cashback_selector` | Optional offer text selectors |
| `in_stock_patterns` / `out_of_stock_patterns` | Phrases matched against the selected text |
| `in_stock_classes` / `out_of_stock_classes` | CSS classes read off the matched element instead of its text |
| `enabled` | `true` only after the URL and selectors are verified |

### Choosing an extractor

| Extractor | Use when | Example |
| --- | --- | --- |
| `rooter_rsc` | The store ships a structured payload inside its HTML | Rooter |
| `html_text` | Stock and price are in the server-rendered markup | e2z Store |
| `jsonld_offers` | The store publishes schema.org JSON-LD offers | StanShop |

`in_stock_classes` / `out_of_stock_classes` exist because some stores put the
real signal in the element's class (`class="stock in-stock"`) while the visible
text varies per product ("Delivery By: 09/11/2026 - 17/11/2026"). Text
patterns alone would read that as `UNKNOWN` and silently miss a restock. When
classes are configured they take priority; the text patterns remain as a
fallback.

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
| **e2z Store** (`e2zstore.com`) | ✅ **Enabled, verified** | WooCommerce. Previously sat behind a Cloudflare managed challenge and returned 403; it now answers a plain GET with full HTML and no challenge header, so **no bypass is needed or used**. `robots.txt` says `Allow: /`. Stock lives in the CSS class on `<p class="stock ...">`, not the text. 6 entries enabled (Rs.1000–Rs.7000). |
| **StanShop** (`www.stanshop.co`) | ✅ **Enabled, verified** | Next.js App Router, no WooCommerce markup. Publishes per-denomination schema.org JSON-LD offers with a fixed availability vocabulary. `robots.txt` says `Allow: /`. 4 entries enabled (Rs.1000–Rs.4000); StanShop publishes no Rs.5000 card, so no entry exists for it. Note `store.getstan.app` returns HTTP 402 `DEPLOYMENT_DISABLED` and is dead. |
| **Amazon India** | ❌ Disabled | Product/search pages return HTTP **503** to non-browser clients (bot protection). Needs the official **Creators API** (PA-API 5 is deprecated) plus Associates credentials. Do not attempt to bypass the blocking. |
| **Games The Shop** | ❌ Disabled | Product data is fetched client-side from `/api/`, which `robots.txt` explicitly `Disallow`s. Respect it; needs an official feed/API. |
| **Paytm** | ❌ Disabled | Search returns a generic marketing page; no server-rendered listings. |

**15 entries are enabled across 3 retailers** (Rooter 5, e2z 6, StanShop 4),
all PlayStation only.

### Why Cloudflare bypass tools are not used

`cloudscraper` and similar libraries exist specifically to defeat Cloudflare's
anti-bot challenge. That is anti-bot evasion, which this project does not do —
and in the one case where it was relevant (e2z) it turned out to be
unnecessary: a plain request now succeeds. The tooling is also stale
(`cloudscraper` last released 2023-04) and only targets legacy challenge
pages, not modern managed challenges.

Where a retailer is actually blocked, the blocker is rarely a Cloudflare JS
challenge anyway: Amazon returns **503**, and Games The Shop's `/api/` is a
**robots policy**. A bypass library would solve neither legitimately.

### What still needs attention

These are the entries that must stay disabled until someone verifies a real
URL and selectors (open the page, DevTools → right-click → *Copy selector*,
test against the live page):

1. **Amazon India** — blocked (503). Only an official Creators API adapter
   can enable this.
2. **Games The Shop** — needs an official data source; its `/api/` path is
   disallowed by robots.txt.
3. **Paytm** — needs a server-rendered or official listing URL.
4. For any page that lists several denominations at once, the selector must
   be scoped to the exact denomination — otherwise "out of stock" for one
   variant can be read as stock for another.

No working product URLs or selectors were invented. The 15 enabled entries
(Rooter, e2z Store, StanShop) were each checked against the live page before
`enabled: true` was set.

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
`OUT → UNKNOWN → IN`), price drops, duplicate suppression, the three extractors
(`rooter_rsc`, `html_text`, `jsonld_offers`), class-based availability,
safety rules (unknown never counts as in/out of stock),
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
