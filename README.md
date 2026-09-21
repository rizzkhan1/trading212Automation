# Trading 212 Overnight Bot

A single Azure Function that runs an automated overnight trade on [Trading 212](https://www.trading212.com/):
**buy one ticker ~1 minute before the US market close, sell it ~1 minute after the next US market open.**

The idea is to hold the position only overnight (when most of a stock's historical drift/gap tends to happen) and be back in cash during the regular trading session. With `TRADE_QUANTITY=ALL`, each cycle reinvests all available cash, so gains/losses compound.

> ⚠️ This is unattended financial trading code. Once deployed with `T212_ENV=live` and `DRY_RUN=false`, it places real orders with real money, on a schedule, with no confirmation step. Read this whole document, and the [Risks & known limitations](#risks--known-limitations) section in particular, before going live.

## How it works

- An Azure Functions **timer trigger** fires every minute inside a wide daily UTC window (`TRADE_SCHEDULE`, default covers 13:00–20:59 UTC, Mon–Fri).
- On each tick, the function converts "now" to UK time and checks whether it exactly matches the configured buy or sell instant for today. Almost every tick is a no-op; it only acts on the two ticks per day (if any) that match.
- Before buying, it also checks that today is a US trading day (weekday, not an NYSE holiday) and not in `NO_BUY_DATES`.
- **Buy**: sized either as a fixed share count (`TRADE_QUANTITY=<number>`) or, with `TRADE_QUANTITY=ALL`, as "as many shares as available cash allows" — computed from a live Yahoo Finance price quote, an account-currency FX conversion if needed, and a safety buffer (`CASH_BUFFER_PCT`).
- **Sell**: sells the entire current holding of the ticker (or up to the fixed quantity, whichever is smaller) — it will never go short.
- All of this is one file, [`function_app.py`](function_app.py); there's no database or other config store — everything is driven by environment variables (Azure "app settings").

## Requirements

- A [Trading 212](https://www.trading212.com/) account, with the [public API](https://docs.trading212.com/api) enabled (Settings → API (Beta) in the app) — separately for the **demo** (practice) and **live** (real money) environments; they use different API keys.
- Python 3.11+ (Azure Functions Python worker) for local testing.
- An Azure subscription, Azure CLI (`az`), logged in, if you want to deploy.

## Configuration reference

All configuration is environment variables / Azure app settings, read in `load_config()`.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `T212_API_KEY` / `T212_API_SECRET` | yes | — | Trading 212 API credentials (HTTP Basic auth: key as username, secret as password). Demo and live keys are different. The key needs **Portfolio**, **Account Data**, and **Orders** permission scopes. |
| `T212_ENV` | no | `demo` | `demo` or `live` — selects `https://{env}.trading212.com/api/v0` as the API base. |
| `T212_TICKER` | yes | — | Trading 212 instrument ticker, e.g. `AAPL_US_EQ` — **not** a bare stock symbol. Verify it against `/equity/metadata/instruments` (see `local_test.py check`) before deploying. |
| `TRADE_QUANTITY` | yes | — | A share count (e.g. `1` or `0.5`) or `ALL`. `ALL` buys with all available cash (minus buffer) and sells the entire position — this is what makes the strategy compound. |
| `BUY_TIME_UK` / `SELL_TIME_UK` | yes | — | `HH:MM` in UK time, specified for the *normal* 5-hour UK/US gap (US close 21:00 UK → use `20:59`; US open 14:30 UK → use `14:31`). |
| `ADJUST_FOR_US_UK_DST_GAP` | no | `true` | The UK and US change clocks on different dates, so for ~3 weeks a year the gap is 4h, not 5h. When true, both times shift 1h earlier during those weeks so the *actual* US-relative timing stays correct. |
| `DRY_RUN` | no | `true` | When true, logs the intended order instead of placing it. **Always confirm this is `false` deliberately before going live.** |
| `EXTENDED_HOURS_TRADING` | no | `true` | Passed as `extendedHours` on every order, so buys/sells can fill in the pre-market/after-market session, not just the regular one. Needed because `BUY_TIME_UK`/`SELL_TIME_UK` sit right at the close/open edge — a regular-hours-only order submitted even slightly after close (or before open) won't fill and instead sits `NEW`/pending until the next session. Extended-hours liquidity/spreads are typically worse than regular hours, so this is a real tradeoff, not a pure upgrade. |
| `NO_BUY_DATES` | no | *(empty)* | Comma-separated `YYYY-MM-DD` (US date) days to skip buying — e.g. days with an early close: `2026-11-27,2026-12-24`. |
| `TRADE_SCHEDULE` | no | `0 * 13-20 * * 1-5` | NCRONTAB, evaluated in UTC — the window the timer trigger runs in. Must cover both buy and sell times year-round including the DST-mismatch shift. |
| `PRICE_SYMBOL` | no | ticker up to first `_` | Yahoo Finance symbol used for `ALL`-mode pricing, e.g. `AAPL` for `AAPL_US_EQ`. Only used when `TRADE_QUANTITY=ALL`. |
| `INSTRUMENT_CURRENCY` | no | `USD` | Currency the instrument is quoted in — must match what Trading 212 reports for the ticker (`local_test.py check` warns if it doesn't). |
| `CASH_BUFFER_PCT` | no | `1.0` | % of available cash left uninvested. See [Risks](#risks--known-limitations) — needs more headroom than just the FX fee. |
| `MAX_TRADE_VALUE` | no | *(none)* | Optional cap on a single buy, in account currency. |
| `QTY_DECIMALS` | no | `4` | Fractional-share precision used when computing `ALL`-mode quantity. |

## Local setup & testing

```bash
pip install -r requirements.txt
```

Copy your credentials and settings into `local.settings.json`'s `Values` block (this file is git-ignored — never commit real keys). `local_test.py` loads it with `os.environ.setdefault`, so real environment variables you set on the command line always take precedence.

```bash
python local_test.py check   # read-only: verifies credentials, ticker, Yahoo price, ALL-quantity sizing
python local_test.py buy     # forces one buy immediately, ignoring the clock/holiday checks
python local_test.py sell    # forces one sell immediately
```

`check` places no orders. `buy`/`sell` still respect `DRY_RUN` (default `true`, so nothing is placed unless you explicitly override it: `DRY_RUN=false python local_test.py buy`). If `T212_ENV=live` and `DRY_RUN=false`, `local_test.py` requires you to type `LIVE` at an interactive prompt before it will place a real order — this confirmation only exists in the local script, not in the deployed Azure Function (see [Going live](#going-live)).

There's no automated test suite; `check` is the closest thing to a smoke test.

## Deployment

[`provision.ps1`](provision.ps1) (PowerShell, requires `az login` first) creates/updates a **Flex Consumption** Function App (Python, Linux), pushes all trading config as app settings, and zip-deploys the code. It's safe to re-run to change settings or redeploy.

```powershell
./provision.ps1 -AppName <unique-app-name> -Ticker AAPL_US_EQ -Quantity ALL
# add -ResourceGroup / -Location to override the defaults (rg-t212-bot / uksouth)
# add -ApiKey / -ApiSecret to avoid the interactive secret prompt (or set $env:T212_API_KEY / $env:T212_API_SECRET first)
```

By default this deploys to the **demo** environment with `DRY_RUN=true` — nothing trades until you explicitly change that.

### Going live

1. Generate a **new, live** API key in the Trading 212 app (demo and live keys are separate), with the same **Portfolio + Account Data + Orders** permissions.
2. If you set an IP restriction on the key: the deployed Function calls Trading 212 from Azure's outbound IP, not your home IP. Flex Consumption apps don't have a fixed outbound IP unless you've added VNET integration/NAT — so either leave the live key unrestricted or account for this.
3. Re-run `provision.ps1` with `-T212Env live -DryRun $true` first (same code, live account, still logging only) and check Application Insights logs over a few scheduled ticks to confirm timing and computed quantities look right.
4. Only then re-run with `-DryRun $false`. From that point on, every matching tick places a real order automatically — there's no confirmation gate once deployed.

## Architecture

- **Trigger**: `trade()` is registered with `@app.timer_trigger(schedule="%TRADE_SCHEDULE%", ...)`, firing every minute across a wide UTC window; `execute()` decides on each tick whether to actually do anything.
- **Gating**: `execute()` converts "now" to UK time, computes today's effective buy/sell instants via `effective_uk_time()`, and proceeds only if the current minute matches one of them *and* `is_trading_day()` (weekday, not an NYSE holiday) is true. `force="buy"|"sell"` (used only by `local_test.py`) skips both checks but still honors `DRY_RUN`.
- **DST handling**: `effective_uk_time()` shifts the configured times 1 hour earlier during the ~3 weeks a year the UK and US clock-change dates don't line up, so the trade stays anchored to the actual US open/close regardless of `ADJUST_FOR_US_UK_DST_GAP`.
- **Sizing**: fixed `TRADE_QUANTITY`, or `all_in_quantity()` for `ALL` — reads available cash from `/equity/account/summary`, applies `CASH_BUFFER_PCT` and an optional `MAX_TRADE_VALUE`, then converts to a share count using a Yahoo Finance chart-endpoint price (`yahoo_price()`) and an FX rate if the instrument currency differs from the account currency.
- **Trading 212 API calls** go through `_get()` / `place_market_order()` using HTTP Basic auth against `https://{demo|live}.trading212.com/api/v0`. `place_market_order()` is deliberately **not retried** — the order endpoint isn't idempotent, so a retry could double-place a trade.
- **Selling** always sells at most what's currently held (`held_quantity()`, summed from `/equity/positions`), even in fixed-quantity mode, so the strategy can never go short.
- The only two external services are Yahoo Finance (price only, unauthenticated, unofficial endpoint) and Trading 212 (trading, authenticated).

## Trading 212 API notes

A few things about Trading 212's public API that shaped this design, discovered while building and testing this project:

- **Auth** is HTTP Basic: API key as username, secret as password, base64-encoded into the `Authorization` header. There's no bearer-token or single-header scheme.
- **API keys have configurable, granular permission scopes** (Account Data, History, Orders, Portfolio, etc.), set when you generate the key. A key with only Orders permission will get `403 Forbidden` (not 401) on `/equity/positions` and `/equity/account/summary` — this looks like an auth failure but is actually a scope problem.
- **There is no quote/price endpoint.** The only way to know a live price via the API is to have already placed an order or hold a position; there's no "get current price for ticker X" call. This is why `ALL`-mode sizing has to go via Yahoo Finance instead.
- **The market order endpoint only accepts a share quantity**, not a monetary value — even though the Trading 212 app itself supports "value orders" (buy/sell by £/$ amount), that's app/UI-only and isn't exposed through the public API. Confirmed against the docs and multiple independent third-party SDKs.
- `/equity/metadata/instruments` (used to validate `T212_TICKER`) is rate-limited to roughly one call per 50 seconds.

## Risks & known limitations

- **Yahoo's price can be meaningfully stale relative to Trading 212's live execution price.** In testing, a single ticker's Yahoo quote was observed to be ~5% away from Trading 212's actual fill price within about a minute. Since `ALL`-mode sizes the order off the Yahoo quote, a `CASH_BUFFER_PCT` sized only to cover the ~0.15% FX conversion fee is **not enough** — it needs real headroom for quote staleness and volatility, or the buy order will intermittently fail with `insufficient-free-for-stocks-buy`. This repo currently uses `8%` as a result of that testing; treat it as a starting point to tune per ticker, not a guarantee.
- **Order placement is not retried by design** (the endpoint isn't idempotent) — if a buy fails (e.g. insufficient funds from the issue above), that day's buy is simply skipped; there's no automatic recovery.
- **No confirmation gate in production.** `local_test.py`'s `LIVE` typed-confirmation only protects local manual runs. The deployed, scheduled function has no equivalent — `DRY_RUN=false` on a live account means real, unattended trades.
- **Single point of failure**: one ticker, one account, one Azure Function instance. There's no monitoring/alerting built in beyond whatever you configure in Application Insights.
- **No automated tests.** Verify any change to `execute()`, `effective_uk_time()`, `load_config()`, or `place_market_order()` with `python local_test.py check` (read-only) before ever running `buy`/`sell`.

## Repository layout

```
function_app.py      # the entire application — trigger, config, trading logic
local_test.py         # local check/buy/sell runner, no Azure host needed
provision.ps1          # Azure provisioning + deployment script
host.json               # Azure Functions host config
requirements.txt        # Python dependencies
local.settings.json     # local dev settings (git-ignored — holds real credentials locally)
CLAUDE.md                # guidance for AI coding agents working in this repo
```
