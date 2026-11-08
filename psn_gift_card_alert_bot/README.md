# Package: `psn_gift_card_alert_bot`

Detection and notification logic for the PSN gift card alert bot.

- `core.py` — configuration loading, JSON state persistence, Rooter/HTML
  extractors, alert + dedup rules, Telegram sending (token always scrubbed
  from logs), report builder.
- `bot.py` — optional **local** long-running process (Telegram pairing and
  `/status`, `/report`, `/help`). Not used by GitHub Actions.
- `config/products.yaml` — product list.

GitHub Actions runs `../github_runner.py`, which imports this package's
`core` module, so both entry points share one implementation.

See the repository root `README.md` for setup, the Actions workflow, retailer
coverage, and verification status.
