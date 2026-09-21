"""
Overnight trade for ONE ticker on Trading 212:
  BUY  ~1 min before the US close, SELL ~1 min after the next US open.

Everything is driven by app settings (environment variables):
  T212_API_KEY / T212_API_SECRET  API credentials (demo and live keys differ)
  T212_ENV                        demo | live            (default demo)
  T212_TICKER                     e.g. AAPL_US_EQ
  TRADE_QUANTITY                  a share count (e.g. 1 or 0.5) OR "ALL".
                                  ALL = buy with all available cash in the
                                  account, sell the whole position (compounding).
  BUY_TIME_UK / SELL_TIME_UK      HH:MM in UK time, given for the NORMAL 5h
                                  UK/US gap (US close 21:00 UK -> 20:59,
                                  US open 14:30 UK -> 14:31)
  ADJUST_FOR_US_UK_DST_GAP        true (default) | false. The US and UK change
                                  clocks on different dates, so for ~3 weeks a
                                  year the gap is 4h. When true, both times
                                  shift 1h earlier during those weeks.
  DRY_RUN                         true (default) | false
  EXTENDED_HOURS_TRADING          true (default) | false. Lets orders fill in the
                                  pre-market/after-market session, not just the
                                  regular one - needed since BUY/SELL are timed
                                  right at the close/open edge, where regular-
                                  hours-only orders can miss the session and sit
                                  pending until the next one instead of filling.
  NO_BUY_DATES                    optional, comma-separated YYYY-MM-DD (US date)
                                  for early-close days, e.g. 2026-11-27,2026-12-24
  TRADE_SCHEDULE                  NCRONTAB in UTC, default "0 * 13-20 * * 1-5"

Only used when TRADE_QUANTITY=ALL (T212's API has no price/quote endpoint, so
the share count is worked out from a Yahoo Finance price):
  PRICE_SYMBOL                    Yahoo symbol; default = T212_TICKER up to the
                                  first "_" (AAPL_US_EQ -> AAPL)
  INSTRUMENT_CURRENCY             currency the stock is quoted in (default USD)
  CASH_BUFFER_PCT                 % of available cash left uninvested to absorb
                                  price moves, the 0.15% FX fee and spread
                                  (default 1.0). Too low -> orders can be rejected.
  MAX_TRADE_VALUE                 optional cap on the buy, in account currency
  QTY_DECIMALS                    fractional-share precision (default 4)
"""
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import azure.functions as func
import holidays
import requests

app = func.FunctionApp()
log = logging.getLogger("t212")

UK = ZoneInfo("Europe/London")
NY = ZoneInfo("America/New_York")
NYSE_HOLIDAYS = holidays.NYSE()
NORMAL_GAP = timedelta(hours=5)  # UK is normally 5h ahead of New York
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{}"


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() not in ("false", "0", "no")


def _hhmm(name: str):
    return datetime.strptime(os.environ[name].strip(), "%H:%M").time()


def load_config() -> dict:
    env = os.getenv("T212_ENV", "demo").strip().lower()
    if env not in ("demo", "live"):
        raise ValueError("T212_ENV must be 'demo' or 'live'")

    ticker = os.environ["T212_TICKER"].strip()
    raw_qty = os.environ["TRADE_QUANTITY"].strip()
    use_all = raw_qty.upper() == "ALL"
    qty = None if use_all else float(raw_qty)
    if not use_all and qty <= 0:
        raise ValueError("TRADE_QUANTITY must be a positive number or ALL")

    max_value = os.getenv("MAX_TRADE_VALUE", "").strip()
    return {
        "env": env,
        "base": f"https://{env}.trading212.com/api/v0",
        "key": os.environ["T212_API_KEY"],
        "secret": os.environ["T212_API_SECRET"],
        "ticker": ticker,
        "use_all": use_all,
        "qty": qty,
        "price_symbol": os.getenv("PRICE_SYMBOL", "").strip() or ticker.split("_")[0],
        "instrument_ccy": os.getenv("INSTRUMENT_CURRENCY", "USD").strip().upper(),
        "buffer_pct": float(os.getenv("CASH_BUFFER_PCT", "1.0") or 1.0),
        "max_value": float(max_value) if max_value else None,
        "decimals": int(os.getenv("QTY_DECIMALS", "4") or 4),
        "buy": _hhmm("BUY_TIME_UK"),
        "sell": _hhmm("SELL_TIME_UK"),
        "adjust": _flag("ADJUST_FOR_US_UK_DST_GAP", "true"),
        "dry_run": _flag("DRY_RUN", "true"),
        "extended_hours": _flag("EXTENDED_HOURS_TRADING", "true"),
        "no_buy": {d.strip() for d in os.getenv("NO_BUY_DATES", "").split(",") if d.strip()},
    }


def effective_uk_time(now_uk: datetime, ref, adjust: bool) -> datetime:
    """Configured UK time for today, shifted if the UK/US clock gap isn't 5h."""
    target = datetime.combine(now_uk.date(), ref, tzinfo=UK)
    if adjust:
        gap = now_uk.utcoffset() - now_uk.astimezone(NY).utcoffset()
        target -= NORMAL_GAP - gap  # 0 normally, 1h in the DST-mismatch weeks
    return target


def is_trading_day(d) -> bool:
    return d.weekday() < 5 and d not in NYSE_HOLIDAYS


# ------------------------------------------------------------ Trading 212 --
def _get(s: requests.Session, c: dict, path: str):
    r = s.get(f"{c['base']}{path}", timeout=30)
    if not r.ok:
        raise RuntimeError(f"T212 GET {path} {r.status_code}: {r.text}")
    return r.json()


def held_quantity(s: requests.Session, c: dict) -> float:
    return sum(float(p["quantity"]) for p in _get(s, c, "/equity/positions")
               if p["instrument"]["ticker"] == c["ticker"])


def place_market_order(s: requests.Session, c: dict, qty: float) -> None:
    """qty > 0 buys, qty < 0 sells. Endpoint isn't idempotent: never retry."""
    side = "BUY" if qty > 0 else "SELL"
    if c["dry_run"]:
        log.info("[DRY RUN] %s %s x %s (%s)", side, c["ticker"], abs(qty), c["env"])
        return
    r = s.post(f"{c['base']}/equity/orders/market",
               json={"ticker": c["ticker"], "quantity": qty,
                     "extendedHours": c["extended_hours"]}, timeout=30)
    if not r.ok:
        raise RuntimeError(f"T212 order {r.status_code}: {r.text}")
    log.info("%s %s x %s placed (%s): %s", side, c["ticker"], abs(qty), c["env"], r.text)


# ------------------------------------------------------ "ALL" (compounding) --
def yahoo_price(symbol: str) -> float:
    """Latest price from Yahoo's unofficial chart endpoint. Swap this function
    if you'd rather use a paid/real-time source (Finnhub, Polygon, ...)."""
    r = requests.get(YAHOO_CHART.format(quote(symbol, safe="=")),
                     params={"interval": "1m", "range": "1d"},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    r.raise_for_status()
    return float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])


def all_in_quantity(s: requests.Session, c: dict) -> float:
    """Shares affordable with the account's available cash (after buffer)."""
    summary = _get(s, c, "/equity/account/summary")
    acct_ccy = summary["currency"].upper()
    cash = float(summary["cash"]["availableToTrade"])

    budget = cash * (1 - c["buffer_pct"] / 100)
    if c["max_value"] is not None:
        budget = min(budget, c["max_value"])

    price = yahoo_price(c["price_symbol"])  # in the instrument's currency
    fx = 1.0
    if c["instrument_ccy"] != acct_ccy:
        fx = yahoo_price(f"{c['instrument_ccy']}{acct_ccy}=X")
    price_acct = price * fx

    scale = 10 ** c["decimals"]
    qty = math.floor(budget / price_acct * scale) / scale
    log.info("ALL: cash=%.2f %s budget=%.2f price=%.4f %s fx=%.5f -> qty=%s",
             cash, acct_ccy, budget, price, c["instrument_ccy"], fx, qty)
    return qty


def execute(force: str | None = None) -> None:
    """One scheduler tick. Trades only if this minute is the buy/sell minute on
    a US trading day. force='buy'|'sell' skips the clock and holiday checks and
    is only used by local_test.py; DRY_RUN is still respected."""
    c = load_config()
    now_utc = datetime.now(timezone.utc)
    today_ny = now_utc.astimezone(NY).date()

    if force:
        action = force.lower()
        if action not in ("buy", "sell"):
            raise ValueError("force must be 'buy' or 'sell'")
    else:
        now_uk = now_utc.astimezone(UK).replace(second=0, microsecond=0)
        buy_at = effective_uk_time(now_uk, c["buy"], c["adjust"])
        sell_at = effective_uk_time(now_uk, c["sell"], c["adjust"])
        if now_uk == buy_at:
            action = "buy"
        elif now_uk == sell_at:
            action = "sell"
        else:
            return
        if not is_trading_day(today_ny):
            log.info("US market closed on %s, skipping", today_ny)
            return

    s = requests.Session()
    s.auth = (c["key"], c["secret"])  # HTTP Basic: key as user, secret as password

    if action == "buy":
        if not force and today_ny.isoformat() in c["no_buy"]:
            log.info("%s is in NO_BUY_DATES, skipping buy", today_ny)
            return
        qty = all_in_quantity(s, c) if c["use_all"] else c["qty"]
        if qty <= 0:
            log.warning("Computed buy quantity is %s, skipping", qty)
            return
        place_market_order(s, c, qty)
    else:
        held = held_quantity(s, c)
        qty = held if c["use_all"] else min(held, c["qty"])
        if qty <= 0:
            log.info("Nothing to sell for %s", c["ticker"])
            return
        place_market_order(s, c, -qty)


# Runs every minute (UTC window covers every possible US open/close); execute()
# returns immediately unless this minute is the buy or sell minute.
@app.timer_trigger(schedule="%TRADE_SCHEDULE%", arg_name="timer",
                   run_on_startup=False, use_monitor=False)
def trade(timer: func.TimerRequest) -> None:
    execute()
