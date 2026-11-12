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
| `state/state.json` | Persisted observations and last report date (committed; contains no chat id) |
| `tests/test_core.py` | 206 tests for state, transitions, dedup, extractors (incl. GYFTR's JSON feed and Woohoo's inventory ledger), the enable-candidate signal, report totals, message splitting, chat-id isolation |
| `requirements.txt` | Runtime dependencies (canonical) |
| `requirements-dev.txt` | Test dependencies (`pytest`) |

---

## Setup

### 1. Create the Telegram bot (once)

1. Open Telegram, talk to `@BotFather`, send `/newbot`, follow the prompts.
2. Copy the **bot token**.
3. Get your **chat id** (the id of your private chat with the bot):
   - easiest: open Telegram, talk to `@userinfobot`, and it replies with your
     id; or
   - local pairing: run `python -m psn_gift_card_alert_bot.bot` once and send
     `/start` — the bot replies with these instructions (the pairing lasts
     only for that local session).
4. Add both values as GitHub Actions secrets (step 2 below).

> **Never paste the token or the chat id into a chat, an issue, or a commit.**
> Both are read only from environment variables (`TELEGRAM_BOT_TOKEN`,
> `TELEGRAM_CHAT_ID`), and every log line and error message is scrubbed
> through `core.scrub()` before it is written. The chat id is **not** stored
> in `state/state.json` — that file holds only product observations and
> report scheduling.

### 2. Add the GitHub secrets (once)

1. Open the repository on GitHub → **Settings** → **Secrets and variables** →
   **Actions**.
2. Click **New repository secret** twice and create:

   | Name | Value |
   | --- | --- |
   | `TELEGRAM_BOT_TOKEN` | the token from BotFather (type it in the web form; it is not stored in the repository and never appears in logs) |
   | `TELEGRAM_CHAT_ID` | your private chat id (from `@userinfobot`, see step 1.3) |

The workflow reads them as `${{ secrets.TELEGRAM_BOT_TOKEN }}` and
`${{ secrets.TELEGRAM_CHAT_ID }}` and passes them to the process via the
environment. Neither is ever echoed, ever written to `state/state.json`, and
ever printed. If `TELEGRAM_CHAT_ID` is missing the cycle still runs, but
alerts and reports are skipped with a warning.

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

Put the token and (optionally) the chat id in a local `.env` (this file is
gitignored and must never be committed):

```
TELEGRAM_BOT_TOKEN=123456:ABC-your-token
TELEGRAM_CHAT_ID=123456789
```

`TELEGRAM_CHAT_ID` is optional locally: without it, `/start` pairs the bot
for the current session only and the bot replies with instructions for
persisting it. Neither value is ever written to `state/state.json` or logs.

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
| `url` | Exact product/variant URL (for `gyftr_api`, the brand's JSON feed; for `woohoo_api`, the product page) |
| `link` | Optional human-facing page used in alerts when `url` is a data feed |
| `extractor` | `rooter_rsc`, `html_text`, `jsonld_offers`, `gyftr_api` or `woohoo_api` |
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
| `gyftr_api` | The store serves stock from its own public JSON feed | GYFTR |
| `woohoo_api` | The page lists what's offered and the store's own public ledger says what's sold out | Woohoo |

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
| **e2z Store** (`e2zstore.com`) | ✅ **Enabled, verified** | WooCommerce. Previously sat behind a Cloudflare managed challenge and returned 403; it now answers a plain GET with full HTML and no challenge header, so **no bypass is needed or used**. `robots.txt` says `Allow: /`. Stock lives in the CSS class on `<p class="stock ...">`, not the text. 6 entries enabled (Rs.1000–Rs.7000). **2026-11-12 update:** the managed challenge has returned on most product pages (403, `cf-mitigated: challenge`); those fetches fail and keep their last-known status — no bypass is attempted, and the entries recover when the site stops challenging. |
| **StanShop** (`www.stanshop.co`) | ✅ **Enabled, verified** | Next.js App Router, no WooCommerce markup. Publishes per-denomination schema.org JSON-LD offers with a fixed availability vocabulary. `robots.txt` says `Allow: /`. 4 entries enabled (Rs.1000–Rs.4000); StanShop publishes no Rs.5000 card, so no entry exists for it. Note `store.getstan.app` returns HTTP 402 `DEPLOYMENT_DISABLED` and is dead. |
| **Amazon India** | ❌ Disabled | Product/search pages return HTTP **503** to non-browser clients (bot protection). Needs the official **Creators API** (PA-API 5 is deprecated) plus Associates credentials. Do not attempt to bypass the blocking. |
| **Games The Shop** | ❌ Disabled | Product data is fetched client-side from `/api/`, which `robots.txt` explicitly `Disallow`s. Re-verified 2026-11-12: the sitemap now lists 20 `/product/<uuid>` URLs, but all of them return the same data-less ~45 KB shell and the App Router RSC flight carries router metadata only — the server-rendered source still does not exist. Respect robots; needs an official feed/API. |
| **Paytm** | ❌ Disabled | Search returns a generic marketing page; no server-rendered listings. |
| **GYFTR** (`www.gyftr.com`) | ⏳ **Disabled, data path verified** | The PlayStation brand page is live (brand id 753, published), but its HTML is an empty Next.js shell: denominations, prices and a per-product `stock_left` count come from the site's own unauthenticated JSON feed (`api.gyftr.com/gyftrapi/api/v1/brand/detail/sony-playstation`), which answers a plain GET with the `Origin`/`Referer` its own JavaScript sends. Neither host's `robots.txt` restricts it; no cookie, credential or challenge is involved. The feed works (other brands return real stock counts) but currently returns **zero products** for PlayStation — and the site's own search returns "No Brands Found" — so no denomination can be verified. 5 entries configured (Rs.1000–Rs.5000, denominations unverified) and disabled; **the runner annotates every run once the feed lists products again**, which is the cue to verify and enable. |
| **Woohoo** (`www.woohoo.in`) | ✅ **Enabled, verified** | The product page is server-rendered and embeds `window.__INITIAL_STATE` with the offered slabs (`price.denominations` = Rs.1000–5000 for `sku B2CEGVGBPS001`, `upc.id` 7864) — but no stock field at all; the default slab display and the quantity limit are configuration, not stock. Stock comes from the site's own unauthenticated ledger `GET /proxy/locale/en-in/product/inventories?searchCriteria…entityId=<upc>`, whose direction is proven by the shipped bundle: returned rows land in `outOfStockDenominations` and cart items listed there are blocked ("Item is out of stock"), the selector switches away from a listed slab, and normally-purchasable control brands return an empty list. `robots.txt` disallows only `/en-in/`; no cookie, login or checkout is involved. Checked live 2026-11-12: **all five PlayStation slabs currently read OUT_OF_STOCK** (ledger rows dated 2026-11-05…09). 5 entries enabled (Rs.1000–Rs.5000) after a dry-run confirmed real statuses; their first observation seeded baselines, so the first stock transition will alert. |

**20 entries are enabled across 4 retailers** (Rooter 5, e2z 6, StanShop 4,
Woohoo 5), all PlayStation only. GYFTR adds 5 configured entries that stay
disabled until its feed actually lists PlayStation products.

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
   disallowed by robots.txt. Re-checked 2026-11-12: the sitemap now lists
   20 product UUIDs (helpful on its own), but every product page is the
   same data-less ~45 KB shell — no price, no stock text, no JSON-LD — and
   the product RSC flight is 227 bytes of router metadata. The server-
   rendered source still does not exist.
3. **Paytm** — needs a server-rendered or official listing URL.
4. **GYFTR** — the feed and data path are verified, but the brand publishes
   no products at all right now (its own search returns "No Brands Found").
   The runner re-checks the disabled entries every cycle and will annotate
   each run with
   `gyftr-psn-1000: disabled entry now reads IN_STOCK -- verify ...`
   (or `OUT_OF_STOCK`) the moment the feed lists products again — that
   annotation is the cue to run `python github_runner.py --dry-run`,
   confirm each entry reads a real status (not `UNKNOWN`), delete any
   denomination GYFTR does not actually list, and set `enabled: true`.
   An entry for a card GYFTR never sells would sit at `UNKNOWN` forever.
5. For any page that lists several denominations at once, the selector must
   be scoped to the exact denomination — otherwise "out of stock" for one
   variant can be read as stock for another.

No working product URLs or selectors were invented. The 20 enabled entries
(Rooter, e2z Store, StanShop, Woohoo) were each checked against the live
page before `enabled: true` was set. GYFTR's feed URL was likewise checked
and works, but its entries stay disabled because the brand currently lists
no products to verify against. Woohoo's page and ledger were both checked
live on 2026-11-12 and agree with the shop's own cart logic; it was enabled
only after the dry-run confirmed all five entries read a real status.

---

## Terms, robots and rate limits

- One request per product per cycle, with a configurable
  `REQUEST_DELAY_SECONDS` (default 2s) between requests — about 48 cycles/day.
  One extra request per cycle re-checks disabled entries that still have a
  URL (GYFTR's five share a single feed URL), so the probe costs one
  request, not five. Woohoo costs **two** requests per cycle — the product
  page and the inventory ledger — both shared by all five of its entries,
  so enabling all five adds only those two requests.
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
`OUT → UNKNOWN → IN`), price drops, duplicate suppression, the five extractors
(`rooter_rsc`, `html_text`, `jsonld_offers`, `gyftr_api`, `woohoo_api`),
class-based availability,
safety rules (unknown never counts as in/out of stock), the enable-candidate
re-check (disabled entries with a URL warn the run once they read a real
status), Woohoo's ledger failure modes (missing stock field, partial reply,
fetch failure, unoffered slab, per-cycle request sharing),
report scheduling, money parsing and token scrubbing.

---

## Troubleshooting

- **No alerts** — confirm both secrets (`TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID`) exist, and that `TELEGRAM_CHAT_ID` matches your private
  chat id (see setup step 1.3).
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
