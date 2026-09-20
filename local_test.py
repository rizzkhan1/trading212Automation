"""
Run ONE buy or sell locally, ignoring the clock and US holidays.

    pip install azure-functions requests holidays
    python local_test.py check    # read-only: credentials, ticker, price, sizing
    python local_test.py buy      # or: sell

Settings come from local.settings.json ("Values"); real environment variables
override them, e.g.  DRY_RUN=false python local_test.py buy
DRY_RUN is still respected, so with DRY_RUN=true nothing is ordered.
"""
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def load_settings(path: Path) -> None:
    for key, value in json.loads(path.read_text(encoding="utf-8"))["Values"].items():
        os.environ.setdefault(key, str(value))


def check() -> None:
    """Read-only preflight: places no orders."""
    import requests
    import function_app as fa

    c = fa.load_config()
    s = requests.Session()
    s.auth = (c["key"], c["secret"])
    print(f"env={c['env']} ticker={c['ticker']} qty={'ALL' if c['use_all'] else c['qty']}")

    held = fa.held_quantity(s, c)  # also proves the credentials work
    print(f"1. Credentials OK. Currently held {c['ticker']}: {held}")

    try:  # instrument list is rate limited (~1 call / 50s), so only once
        meta = fa._get(s, c, "/equity/metadata/instruments")
        inst = next((i for i in meta if i.get("ticker") == c["ticker"]), None)
        if inst:
            print(f"2. Ticker found: currency={inst.get('currencyCode')} "
                  f"minTradeQuantity={inst.get('minTradeQuantity')} type={inst.get('type')}")
            if inst.get("currencyCode") and inst["currencyCode"].upper() != c["instrument_ccy"]:
                print(f"   WARNING: set INSTRUMENT_CURRENCY={inst['currencyCode']}")
        else:
            print("2. WARNING: ticker not found in T212 instrument list, check T212_TICKER")
    except Exception as e:  # metadata is a nice-to-have
        print(f"2. Instrument lookup skipped: {e}")

    print(f"3. Yahoo price {c['price_symbol']}: {fa.yahoo_price(c['price_symbol'])}")
    if c["use_all"]:
        qty = fa.all_in_quantity(s, c)  # logs cash, budget, price, fx
        print(f"4. A BUY right now would order {qty} shares")
    print("Check complete: no orders were placed.")


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1].lower() not in ("buy", "sell", "check"):
        sys.exit("usage: python local_test.py check|buy|sell")
    action = sys.argv[1].lower()

    load_settings(Path(__file__).with_name("local.settings.json"))
    if action == "check":
        return check()

    env = os.environ.get("T212_ENV", "demo")
    dry = os.environ.get("DRY_RUN", "true").lower() not in ("false", "0", "no")
    print(f"env={env} dry_run={dry} ticker={os.environ.get('T212_TICKER')} "
          f"qty={os.environ.get('TRADE_QUANTITY')} action={action.upper()}")

    if env == "live" and not dry:
        if input("LIVE account and DRY_RUN=false: this places a REAL order. "
                 "Type LIVE to continue: ").strip() != "LIVE":
            sys.exit("Aborted")

    import function_app  # imported after settings are loaded
    function_app.execute(force=action)


if __name__ == "__main__":
    main()
