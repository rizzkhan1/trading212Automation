# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single Azure Function (Python, timer-triggered) that runs an overnight "buy near the US close, sell near the next US open" trade for one ticker on Trading 212. Everything — ticker, quantity, trade times, demo/live, dry-run — is driven by app settings (environment variables) read in `load_config()`; there is no other config file and no database. The whole app is one file: [function_app.py](function_app.py).

## Commands

```bash
pip install -r requirements.txt
```

Local dry-run / manual testing (no Azure Functions host needed — `local_test.py` calls straight into `function_app.py`):

```bash
python local_test.py check   # read-only: verifies credentials, ticker, Yahoo price, ALL-quantity sizing
python local_test.py buy     # forces one buy immediately, ignoring the clock/holiday checks
python local_test.py sell    # forces one sell immediately
```

Settings come from `local.settings.json`'s `Values` block; real environment variables override it (`local_test.py`'s `load_settings()` uses `setdefault`). `DRY_RUN` (default true) is always respected, so `buy`/`sell` place no real order unless you explicitly set `DRY_RUN=false`:

```bash
DRY_RUN=false python local_test.py buy
```

`local_test.py` refuses to run a live, non-dry-run trade without typing `LIVE` at an interactive confirmation prompt — this is a real-money safety gate, never bypass or script around it.

There is no lint/test suite configured (no pytest, no linter config) — `local_test.py check` is the closest thing to a smoke test.

### Deployment

`provision.ps1` (PowerShell, run with Azure CLI logged in via `az login`) creates/updates a Flex Consumption Function App, pushes all trading config as app settings, and zip-deploys `function_app.py` + `host.json` + `requirements.txt`. It's safe to re-run to change settings or redeploy code. Going live is a two-step, explicit act: rerun with `-T212Env live -DryRun $false` and real live API keys (demo and live Trading 212 keys are different credentials).

## Architecture

- **Trigger**: `trade()` is registered with `@app.timer_trigger(schedule="%TRADE_SCHEDULE%", ...)` and fires every minute during a wide UTC window (default NCRONTAB `0 * 13-20 * * 1-5`, i.e. every hour-minute 13:00–20:59 UTC, Mon–Fri). Almost every invocation is a no-op.
- **Gating logic lives in `execute()`**: on each tick it converts "now" to UK time, computes today's effective buy/sell instants via `effective_uk_time()`, and only proceeds if the current minute exactly matches one of them *and* today is a US trading day (`is_trading_day()`, using the `holidays.NYSE()` calendar). `force="buy"|"sell"` (used only by `local_test.py`) skips both checks but still honors `DRY_RUN`.
- **DST handling**: `BUY_TIME_UK`/`SELL_TIME_UK` are specified for the *normal* 5-hour UK/US gap. `effective_uk_time()` shifts them by 1 hour during the ~3 weeks a year the UK and US are on mismatched clocks (US and UK change clocks on different dates), controlled by `ADJUST_FOR_US_UK_DST_GAP`.
- **Sizing**: either a fixed `TRADE_QUANTITY`, or `"ALL"` which triggers `all_in_quantity()` — reads available cash from T212's account summary, applies `CASH_BUFFER_PCT` and an optional `MAX_TRADE_VALUE` cap, then converts to a share count using a Yahoo Finance chart-endpoint price (`yahoo_price()`, unofficial/no API key) and an FX rate if the instrument currency differs from the account currency. This is what makes `ALL` mode compound gains/losses over time. Trading 212's own API has no quote/price endpoint, and its market order endpoint only accepts a share quantity (no value/notional-amount field), so this Yahoo lookup can't be avoided while `TRADE_QUANTITY=ALL` is supported.
- **Trading 212 API calls** all go through `_get()`/`place_market_order()` using HTTP Basic auth (API key as username, secret as password) against `https://{demo|live}.trading212.com/api/v0`. `place_market_order()` explicitly is not retried — the order endpoint isn't idempotent, so a retry could double-place a trade. Every order sends `extendedHours: EXTENDED_HOURS_TRADING` (default true) — since `BUY_TIME_UK`/`SELL_TIME_UK` sit right at the close/open edge, a regular-hours-only order can miss the session on minor timing/latency and sit `NEW`/pending until the next one instead of filling.
- **Selling** always sells at most what's currently held (`held_quantity()`, summed from `/equity/positions` by ticker), even in fixed-quantity mode, so it can never go short.
- Nothing else calls out to any other service — Yahoo Finance (price only, no auth) and Trading 212 (trading, auth via key/secret) are the only two external dependencies.

## Working in this repo

- This is unauthenticated financial trading code that moves real money once `T212_ENV=live` and `DRY_RUN=false`. Treat any change to `execute()`, `place_market_order()`, `load_config()`, or the DST/time logic in `effective_uk_time()` as high-stakes: an off-by-one-minute or sign error there means a wrong trade, not a crashed test.
- `local.settings.json` is git-ignored and holds real-looking credential placeholders — never commit real API keys there, and treat `.env` / `azurite/` (also git-ignored) the same way.
- There's no test framework; verify behavior changes with `python local_test.py check` (read-only) before ever running `buy`/`sell`, and keep `DRY_RUN=true` while iterating.
- `CASH_BUFFER_PCT` needs real headroom, not just enough to cover the FX fee: Trading 212's live price can differ from Yahoo's (delayed/free) quote by several percent depending on the ticker's volatility, and `TRADE_QUANTITY=ALL` orders are sized off that Yahoo quote. A buffer that's only as big as the nominal FX fee will intermittently get "insufficient funds" order rejections.
