# PlayStation Gift Card Alert Bot

A personal Telegram alert service for PSN gift-card availability, price reductions,
discount text and cashback text. It uses Python, Requests, BeautifulSoup, APScheduler
and SQLite. It does **not** use an LLM for routine checks.

## What is implemented

- Telegram `/start`, `/status`, `/report` and `/help`
- One-minute scheduler (minimum configured interval is 60 seconds)
- SQLite snapshots and event history
- Back-in-stock detection (after a baseline has been recorded)
- Price-drop detection when a reliable price selector is configured
- Change detection for discount and cashback text when selectors are configured
- Daily report at 09:00 Asia/Kolkata by default
- Per-product enable/disable and retailer-specific CSS selectors/patterns
- Errors are logged; unknown stock state is not treated as in-stock

## Important limitations

1. **Retailer-specific setup is required.** Product pages differ, and selectors can change.
   The included Rooter URL is a starting point only; its denomination variants need
   variant-specific selectors or URLs before enabling reliable per-denomination alerts.
2. Amazon PA-API 5 is deprecated. Amazon's current Creators API requires Associates
   eligibility and credentials. This starter does not pretend to integrate with Amazon
   without those credentials. Add an authorized API adapter if eligible.
3. Paytm and other retailers may require login, JavaScript, or anti-bot checks. Do not
   bypass CAPTCHAs, access controls, or site restrictions. Use official APIs/feeds or
   permitted access methods.
4. A one-minute interval is a scheduler target, not a promise that every retailer permits
   one-minute requests. Respect each site's terms, robots guidance, rate limits and API
   quotas. Increase the interval if a retailer objects or returns 429/403.
5. Cashback/discount selectors only detect visible page text. They cannot reliably
   calculate bank-specific, account-specific, wallet-specific or coupon-conditioned
   offers. Such offers need their own verified data source and rules.
6. A page may list several denominations at once. Configure selectors that scope the
   stock indicator to the exact denomination; otherwise the result may be ambiguous.
7. This is an alert tool, not an auto-purchaser. It will not log in, add to cart, or buy.

## Setup (Windows PowerShell)

1. Install Python 3.11 or newer from https://www.python.org/downloads/.
2. Extract this folder.
3. Open PowerShell in this folder and run:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   Copy-Item .env.example .env
   ```

4. In Telegram, open `@BotFather`, send `/newbot`, follow its prompts, and copy the bot
   token. Paste it into `.env` as `TELEGRAM_BOT_TOKEN=...`. Keep this token secret.
5. Start the program:

   ```powershell
   python bot.py
   ```

6. Open your new bot in Telegram and send `/start`. The first private chat to start an
   unclaimed bot becomes its notification destination. Keep the bot private.
7. Send `/status` or `/report` to test the connection.

## Setup (Linux/macOS)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env and set TELEGRAM_BOT_TOKEN
python bot.py
```

## Configure products

Edit `config/products.yaml`. For each product entry:

- `id`: unique stable identifier
- `retailer`: retailer name
- `denomination`: face value in INR
- `url`: exact product or variant URL
- `availability_selector`: CSS selector for the exact variant's stock text
- `price_selector`: CSS selector for current price
- `discount_selector`: CSS selector for discount/offer text
- `cashback_selector`: CSS selector for cashback text
- `in_stock_patterns` / `out_of_stock_patterns`: text fragments matched against the
  selected availability element (or whole page if no selector is supplied)
- `enabled`: set to `true` only after testing the page and selectors

To inspect selectors, open the product page in a browser, use Developer Tools
(F12 -> Elements), right-click the relevant element -> Copy -> Copy selector. Test it
against the current page. Prefer stable attributes such as `data-testid` over generated
CSS classes. If the retailer uses JavaScript-rendered content, this Requests-based starter
may not see it; use an approved API or a carefully scoped browser adapter.

**Do not enable the sample Rooter entries unchanged.** A shared page may contain
"Out of Stock" for one option and "Buy Now" for another, which can create false alerts.
First identify selectors that apply to the selected denomination, or configure exact
variant URLs. Then set `enabled: true`.

## Amazon India

Amazon PA-API 5 has been deprecated and replaced by Creators API. Creators API access
requires Amazon Associates enrollment, qualifying shipped sales, registration and
credentials. See:
https://affiliate-program.amazon.in/creatorsapi/docs/en-us/introduction

Do not scrape around Amazon access controls. Once eligible, implement a dedicated
Creators API adapter according to the current official docs and its request quotas.
This starter intentionally leaves Amazon disabled until that integration is authorized.

## One-minute interval and hosting

The default is 60 seconds. The scheduler runs enabled entries sequentially with a small
delay between requests. If you monitor many products, a run can take longer than a
minute; APScheduler will coalesce overlapping runs rather than stack them. API quotas,
retailer terms and anti-bot controls take priority over the requested interval.

For 24/7 operation, run on a small Linux VPS or a reliable always-on machine. On a VPS,
use `systemd` to restart the process after crashes/reboots. Keep `.env`, `data/` and
`bot.log` private; back up the SQLite database. Do not commit `.env` to Git.

## Troubleshooting

- No Telegram alerts: confirm the token, send `/start`, check `bot.log`.
- `403`/`429`: stop or slow checks for that retailer; do not attempt to evade blocks.
- `UNKNOWN`: the page did not contain a recognized stock phrase or the selector was
  incorrect. This is safer than a false positive.
- Wrong stock for a denomination: use a variant-specific URL or a selector scoped to
  that exact denomination.
- Price missing: configure `price_selector` to target the numeric price element.
- Daily report time: set `DAILY_REPORT_HOUR` and `DAILY_REPORT_MINUTE` in `.env`
  (24-hour local time, default 09:00 IST).

## Before going live

Test every configured product manually against the retailer page. Confirm at least one
known in-stock and one known out-of-stock state where possible. Leave ambiguous products
disabled until selectors are verified.
