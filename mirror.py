"""
Portfolio Mirror → Alpaca

Fetches holdings from a portfolio tracker API, resolves FIGIs to tickers via OpenFIGI,
filters to US-listed equities/ETFs only (skips options, foreign stocks),
then rebalances the Alpaca paper account to match those weights.

Supports long and short positions. Options are skipped.
Run on a schedule (every 15–30 min during market hours).
"""

import json
import os
import time
import datetime
import urllib.request
import urllib.error
import urllib.parse
import math
from pathlib import Path
import requests as _requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / "secrets.env")

# ── Config ────────────────────────────────────────────────────────────────────

SESSION_FILE       = Path(__file__).parent / "browser_state.json"
PORTFOLIO_SLUG     = os.environ["PORTFOLIO_SLUG"]
STATE_FILE         = Path(__file__).parent / "last_state.json"
FIGI_CACHE_FILE    = Path(__file__).parent / "figi_cache.json"

ALPACA_API_KEY    = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
ALPACA_BASE_URL   = "https://paper-api.alpaca.markets"

TRACKER_API_KEY   = os.environ["TRACKER_API_KEY"]
TRACKER_BASE_URL  = os.environ["TRACKER_BASE_URL"]

REBALANCE_THRESHOLD     = 0.5   # min pct-point change before rebalancing
MIN_TRADE_DOLLARS       = 10.0
MARGIN_BUFFER_THRESHOLD = 0.40  # close worst shorts if maintenance_margin/equity exceeds this

# ── HTTP helpers ──────────────────────────────────────────────────────────────

def http_get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

def http_post(url, payload, headers=None):
    data = json.dumps(payload).encode()
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

# ── Citrindex client ──────────────────────────────────────────────────────────

def load_session_cookie():
    """Read the session cookie from the saved Playwright browser state."""
    if not SESSION_FILE.exists():
        raise RuntimeError(f"Session file not found: {SESSION_FILE}\nRun login_and_explore.py to authenticate.")
    with open(SESSION_FILE) as f:
        state = json.load(f)
    for cookie in state.get("cookies", []):
        if cookie["name"] == "__Secure-better-auth.session_token":
            expires_ts = cookie.get("expires", 0)
            if expires_ts and expires_ts < time.time():
                raise RuntimeError("Session has expired. Re-run the login script.")
            return cookie["value"]
    raise RuntimeError("Session cookie not found in browser state file.")

def fetch_holdings(date_str=None):
    """Fetch portfolio holdings for a given date (defaults to today)."""
    if date_str is None:
        date_str = datetime.date.today().isoformat()
    raw_cookie = load_session_cookie()
    cookie_value = urllib.parse.unquote(raw_cookie)
    url = f"{TRACKER_BASE_URL}/api/portfolio/{PORTFOLIO_SLUG}/holdings/{date_str}"
    print(f"Fetching holdings for {date_str}...")
    r = _requests.get(
        url,
        headers={
            "x-api-key": TRACKER_API_KEY,
            "referer": f"{TRACKER_BASE_URL}/dashboard/holdings",
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/145.0.7632.6 Safari/537.36",
        },
        cookies={"__Secure-better-auth.session_token": cookie_value},
    )
    if not r.ok:
        raise RuntimeError(f"Holdings fetch failed: {r.status_code} {r.text[:200]}")
    return r.json()

def compute_raw_weights(holdings):
    """
    Compute portfolio-level weight for every FIGI.
    Returns: (long_weights, short_weights) where each is {figi: abs_weight_pct}.
    Short weights are stored as positive numbers representing the magnitude of the short.
    """
    long_weights = {}
    short_weights = {}

    # Direct (non-basket) securities
    for figi, pct in holdings["weightsOfPortfolio"]["security"].items():
        if pct > 0:
            long_weights[figi] = long_weights.get(figi, 0) + pct
        elif pct < 0:
            short_weights[figi] = short_weights.get(figi, 0) + abs(pct)

    # Securities inside baskets
    for basket_name, basket_data in holdings["weightsOfBaskets"].items():
        basket_pct = basket_data["weightInPortfolio"]
        if basket_pct == 0:
            continue
        for figi, weight_in_basket in basket_data["security"].items():
            if weight_in_basket == 0:
                continue
            portfolio_pct = basket_pct * (weight_in_basket / 100.0)
            if portfolio_pct > 0:
                long_weights[figi] = long_weights.get(figi, 0) + portfolio_pct
            else:
                short_weights[figi] = short_weights.get(figi, 0) + abs(portfolio_pct)

    return long_weights, short_weights

# ── OpenFIGI resolver ─────────────────────────────────────────────────────────

def load_figi_cache():
    if FIGI_CACHE_FILE.exists():
        with open(FIGI_CACHE_FILE) as f:
            return json.load(f)
    return {}

def save_figi_cache(cache):
    with open(FIGI_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)

def resolve_figis(figis):
    """
    Map FIGIs → {ticker, securityType, securityType2, exchCode} via OpenFIGI.
    Uses a local cache to avoid redundant API calls.
    Returns: dict of {figi: info_dict or None}
    """
    cache = load_figi_cache()
    to_resolve = [f for f in figis if f not in cache]

    if to_resolve:
        print(f"Resolving {len(to_resolve)} new FIGIs via OpenFIGI...")
        # OpenFIGI free tier (no API key): max 10 per request
        for i in range(0, len(to_resolve), 10):
            batch = to_resolve[i:i+10]
            payload = [{"idType": "ID_BB_GLOBAL", "idValue": figi} for figi in batch]
            try:
                r = _requests.post(
                    "https://api.openfigi.com/v3/mapping",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                r.raise_for_status()
                results = r.json()
                for figi, result in zip(batch, results):
                    data = result.get("data", [{}])
                    if data:
                        d = data[0]
                        cache[figi] = {
                            "ticker": d.get("ticker"),
                            "name": d.get("name"),
                            "securityType": d.get("securityType"),
                            "securityType2": d.get("securityType2"),
                            "exchCode": d.get("exchCode"),
                        }
                    else:
                        cache[figi] = None
                time.sleep(0.5)
            except Exception as e:
                print(f"  OpenFIGI error for batch {i//10}: {e}")
        save_figi_cache(cache)

    return cache

def is_tradeable_us_equity(info):
    """Return True if this security is a US-listed equity or ETF (no options, no foreign)."""
    if info is None:
        return False
    sec_type = info.get("securityType", "") or ""
    sec_type2 = info.get("securityType2", "") or ""
    exch = info.get("exchCode", "") or ""
    ticker = info.get("ticker", "") or ""

    # Reject options
    if "Option" in sec_type or "Option" in sec_type2:
        return False

    # Reject futures, warrants, etc.
    reject_types = {"Future", "Warrant", "Right", "Index"}
    if any(t in sec_type or t in sec_type2 for t in reject_types):
        return False

    # Must be US-listed
    if exch not in ("US", "UA", "UN", "UQ", "UP", "UR", "UT"):
        return False

    # Must have a usable ticker (no spaces — options tickers have spaces)
    if not ticker or " " in ticker:
        return False

    return True

# ── Alpaca client ─────────────────────────────────────────────────────────────

def alpaca_headers():
    return {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }

def get_account():
    return http_get(f"{ALPACA_BASE_URL}/v2/account", alpaca_headers())

def get_positions():
    positions = http_get(f"{ALPACA_BASE_URL}/v2/positions", alpaca_headers())
    return {p["symbol"]: p for p in positions}

def get_latest_price(ticker):
    """Get latest trade price for a ticker via Alpaca data API."""
    url = f"https://data.alpaca.markets/v2/stocks/{ticker}/trades/latest"
    try:
        data = http_get(url, alpaca_headers())
        return float(data["trade"]["p"])
    except Exception:
        return None

def is_tradeable_on_alpaca(ticker):
    """Check if Alpaca can trade this ticker (asset must be active and fractionable)."""
    try:
        data = http_get(f"{ALPACA_BASE_URL}/v2/assets/{ticker}", alpaca_headers())
        return data.get("status") == "active" and data.get("tradable", False)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise

def place_order(ticker, side, notional=None, qty=None):
    """Place a market order. Uses notional (dollar amount) for fractional shares."""
    payload = {
        "symbol": ticker,
        "side": side,
        "type": "market",
        "time_in_force": "day",
    }
    if notional is not None:
        payload["notional"] = round(notional, 2)
    elif qty is not None:
        payload["qty"] = qty
    else:
        raise ValueError("Must provide notional or qty")

    try:
        result = http_post(f"{ALPACA_BASE_URL}/v2/orders", payload, alpaca_headers())
        return result
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  Order failed for {ticker}: {e.code} {body}")
        return None

def close_position(ticker):
    """Close entire position in a ticker."""
    req = urllib.request.Request(
        f"{ALPACA_BASE_URL}/v2/positions/{ticker}",
        method="DELETE",
        headers=alpaca_headers(),
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  Close failed for {ticker}: {e.code} {body}")
        return None

# ── State management ──────────────────────────────────────────────────────────

def load_last_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Main sync logic ───────────────────────────────────────────────────────────

def check_market_open():
    """Return True if US market is currently open via Alpaca clock."""
    try:
        clock = http_get(f"{ALPACA_BASE_URL}/v2/clock", alpaca_headers())
        return clock.get("is_open", False)
    except Exception as e:
        print(f"Warning: could not check market status: {e}")
        return False

def check_session_expiry():
    """Auto-reauth if session is expiring within 2 days."""
    if not SESSION_FILE.exists():
        print("No session file found — running reauth...")
        import reauth
        reauth.reauth()
        return
    with open(SESSION_FILE) as f:
        state = json.load(f)
    for cookie in state.get("cookies", []):
        if cookie["name"] == "__Secure-better-auth.session_token":
            expires_ts = cookie.get("expires", 0)
            days_left = (expires_ts - time.time()) / 86400
            if days_left < 2:
                print(f"Session expires in {days_left:.1f} days — re-authenticating automatically...")
                import reauth
                reauth.reauth()
            else:
                print(f"Session valid for {days_left:.1f} more days.")

def check_and_reduce_margin(dry_run=False):
    """
    If maintenance_margin / equity exceeds MARGIN_BUFFER_THRESHOLD, close
    short positions starting with the worst unrealized loss until we're back
    under the threshold. Returns True if any action was taken.
    """
    account = get_account()
    equity = float(account["equity"])
    maintenance_margin = float(account["maintenance_margin"])
    ratio = maintenance_margin / equity if equity > 0 else 0

    if ratio <= MARGIN_BUFFER_THRESHOLD:
        return False

    print(f"\n⚠️  Margin buffer breach: maintenance_margin/equity = {ratio:.1%} (threshold {MARGIN_BUFFER_THRESHOLD:.0%})")
    print("Closing worst-performing short positions to reduce margin usage...")

    positions = get_positions()
    shorts = [
        p for p in positions.values()
        if p.get("side") == "short"
    ]

    if not shorts:
        print("  No short positions found — nothing to close.")
        return False

    shorts.sort(key=lambda p: float(p.get("unrealized_pl", 0)))

    for pos in shorts:
        ticker = pos["symbol"]
        unreal_pl = float(pos.get("unrealized_pl", 0))
        mkt_val = abs(float(pos["market_value"]))
        # Estimate margin relief: Alpaca short maintenance margin is ~30% of market value
        estimated_margin_relief = mkt_val * 0.30

        print(f"  Closing short {ticker} (unrealized P&L: ${unreal_pl:+,.2f}, ~${estimated_margin_relief:,.0f} margin relief)...")
        if not dry_run:
            close_position(ticker)
            time.sleep(0.5)

        # Re-fetch account to check if we're back under threshold
        if not dry_run:
            account = get_account()
            equity = float(account["equity"])
            maintenance_margin = float(account["maintenance_margin"])
            ratio = maintenance_margin / equity if equity > 0 else 0
            print(f"  Margin ratio now: {ratio:.1%}")
            if ratio <= MARGIN_BUFFER_THRESHOLD:
                print("  Margin buffer restored.")
                break
        else:
            # In dry run, simulate relief
            maintenance_margin -= estimated_margin_relief
            ratio = maintenance_margin / equity
            if ratio <= MARGIN_BUFFER_THRESHOLD:
                print(f"  [DRY RUN] Margin ratio would be ~{ratio:.1%} — threshold met.")
                break

    return True


def run_sync(dry_run=False):
    print(f"\n{'='*60}")
    print(f"Portfolio Mirror — {datetime.datetime.now().isoformat()}")
    print(f"{'='*60}")

    check_session_expiry()

    if not dry_run and not check_market_open():
        print("Market is closed — nothing to do.")
        return

    holdings = fetch_holdings()
    raw_long_weights, raw_short_weights = compute_raw_weights(holdings)
    print(f"Raw positions: {len(raw_long_weights)} long, {len(raw_short_weights)} short FIGIs")

    all_figis = list(set(list(raw_long_weights.keys()) + list(raw_short_weights.keys())))
    figi_info = resolve_figis(all_figis)

    target_weights = {}  # ticker → weight_pct (long)
    target_short_weights = {}  # ticker → abs_weight_pct (short)
    skipped = []
    for figi, weight in raw_long_weights.items():
        info = figi_info.get(figi)
        if not is_tradeable_us_equity(info):
            skipped.append((figi, info.get("ticker") if info else "?", info.get("securityType2") if info else "?", weight))
            continue
        ticker = info["ticker"]
        target_weights[ticker] = target_weights.get(ticker, 0) + weight

    for figi, weight in raw_short_weights.items():
        info = figi_info.get(figi)
        if not is_tradeable_us_equity(info):
            continue  # already captured in skipped via longs if applicable
        ticker = info["ticker"]
        # If a ticker appears as both long and short, net them out
        if ticker in target_weights:
            net = target_weights[ticker] - weight
            if net > 0:
                target_weights[ticker] = net
            elif net < 0:
                del target_weights[ticker]
                target_short_weights[ticker] = abs(net)
            else:
                del target_weights[ticker]
        else:
            target_short_weights[ticker] = target_short_weights.get(ticker, 0) + weight

    print(f"Skipped {len(skipped)} positions (options/foreign/unknown):")
    for figi, ticker, stype, w in sorted(skipped, key=lambda x: -x[3])[:10]:
        print(f"  {ticker:30s} {stype:20s} {w:.2f}%")

    tradeable = {}
    for ticker, weight in sorted(target_weights.items(), key=lambda x: -x[1]):
        if is_tradeable_on_alpaca(ticker):
            tradeable[ticker] = weight
        else:
            print(f"  Not on Alpaca: {ticker} ({weight:.2f}%) — skipping")

    tradeable_short = {}
    for ticker, weight in sorted(target_short_weights.items(), key=lambda x: -x[1]):
        if ticker in tradeable:
            print(f"  Short {ticker} conflicts with long target — skipping short")
            continue
        if is_tradeable_on_alpaca(ticker):
            tradeable_short[ticker] = weight
        else:
            print(f"  Not on Alpaca (short): {ticker} ({weight:.2f}%) — skipping")

    total_allocated_pct = sum(tradeable.values())
    total_short_pct = sum(tradeable_short.values())
    print(f"\nTarget: {len(tradeable)} long positions ({total_allocated_pct:.1f}%), {len(tradeable_short)} short positions ({total_short_pct:.1f}%)")
    print(f"  (Remaining {100-total_allocated_pct:.1f}% unallocated on long side)")

    account = get_account()
    portfolio_value = float(account["portfolio_value"])
    cash = float(account["cash"])
    print(f"\nAlpaca account: ${portfolio_value:,.2f} total | ${cash:,.2f} cash")

    current_positions = get_positions()
    maintenance_margin = float(account["maintenance_margin"])
    margin_ratio = maintenance_margin / float(account["equity"]) if float(account["equity"]) > 0 else 0
    print(f"Current Alpaca positions: {len(current_positions)} | Margin ratio: {margin_ratio:.1%} (threshold {MARGIN_BUFFER_THRESHOLD:.0%})")

    orders = []

    for ticker, target_pct in tradeable.items():
        target_dollars = (target_pct / 100) * portfolio_value
        current_dollars = float(current_positions.get(ticker, {}).get("market_value", 0))
        current_pct = (current_dollars / portfolio_value) * 100

        delta_pct = target_pct - current_pct
        delta_dollars = target_dollars - current_dollars

        if abs(delta_pct) >= REBALANCE_THRESHOLD and abs(delta_dollars) >= MIN_TRADE_DOLLARS:
            side = "buy" if delta_dollars > 0 else "sell"
            orders.append({
                "ticker": ticker,
                "side": side,
                "delta_pct": delta_pct,
                "delta_dollars": delta_dollars,
                "target_pct": target_pct,
                "current_pct": current_pct,
            })

    for ticker, pos in current_positions.items():
        if pos.get("side") != "long":
            continue
        if ticker not in tradeable:
            current_dollars = float(pos["market_value"])
            if current_dollars > MIN_TRADE_DOLLARS:
                orders.append({
                    "ticker": ticker,
                    "side": "sell_all",
                    "delta_pct": -(current_dollars / portfolio_value * 100),
                    "delta_dollars": -current_dollars,
                    "target_pct": 0,
                    "current_pct": current_dollars / portfolio_value * 100,
                })

    for ticker, target_pct in tradeable_short.items():
        target_dollars = (target_pct / 100) * portfolio_value
        pos = current_positions.get(ticker, {})
        pos_side = pos.get("side", "")

        if pos_side == "long":
            # target is short — close long first, open short next run
            current_dollars = float(pos["market_value"])
            orders.append({
                "ticker": ticker,
                "side": "sell_all",
                "delta_pct": -(current_dollars / portfolio_value * 100),
                "delta_dollars": -current_dollars,
                "target_pct": 0,
                "current_pct": current_dollars / portfolio_value * 100,
                "note": "closing long before shorting next run",
            })
            continue

        current_dollars = abs(float(pos.get("market_value", 0))) if pos_side == "short" else 0
        current_pct = (current_dollars / portfolio_value) * 100
        delta_pct = target_pct - current_pct
        delta_dollars = target_dollars - current_dollars

        if abs(delta_pct) >= REBALANCE_THRESHOLD and abs(delta_dollars) >= MIN_TRADE_DOLLARS:
            price = get_latest_price(ticker)
            if not price:
                print(f"  Could not get price for {ticker} — skipping short order")
                continue
            qty = math.floor(abs(delta_dollars) / price)
            if qty < 1:
                continue
            side = "sell_short" if delta_dollars > 0 else "buy_to_cover"
            orders.append({
                "ticker": ticker,
                "side": side,
                "qty": qty,
                "delta_pct": delta_pct,
                "delta_dollars": delta_dollars,
                "target_pct": target_pct,
                "current_pct": current_pct,
            })

    for ticker, pos in current_positions.items():
        if pos.get("side") != "short":
            continue
        if ticker not in tradeable_short:
            orders.append({
                "ticker": ticker,
                "side": "close_short",
                "delta_pct": abs(float(pos["market_value"])) / portfolio_value * 100,
                "delta_dollars": abs(float(pos["market_value"])),
                "target_pct": 0,
                "current_pct": abs(float(pos["market_value"])) / portfolio_value * 100,
            })

    orders.sort(key=lambda o: (0 if o["side"] in ("sell", "sell_all", "close_short", "buy_to_cover") else 1, -abs(o["delta_dollars"])))

    print(f"\nOrders to place: {len(orders)}")
    for o in orders:
        print(f"  {o['side']:8s} {o['ticker']:8s}  {o['current_pct']:+.2f}% → {o['target_pct']:.2f}%  (${o['delta_dollars']:+,.2f})")

    if dry_run:
        print("\n[DRY RUN] No orders placed.")
        return

    print("\nExecuting...")
    for o in orders:
        ticker = o["ticker"]
        if o["side"] == "sell_all":
            print(f"  Closing long {ticker}...{' (' + o['note'] + ')' if 'note' in o else ''}")
            close_position(ticker)
        elif o["side"] == "sell":
            print(f"  Selling ${abs(o['delta_dollars']):.2f} of {ticker}...")
            place_order(ticker, "sell", notional=abs(o["delta_dollars"]))
        elif o["side"] == "buy":
            print(f"  Buying ${o['delta_dollars']:.2f} of {ticker}...")
            place_order(ticker, "buy", notional=o["delta_dollars"])
        elif o["side"] == "sell_short":
            print(f"  Shorting {o['qty']} shares of {ticker} (${abs(o['delta_dollars']):.2f})...")
            place_order(ticker, "sell", qty=o["qty"])
        elif o["side"] == "buy_to_cover":
            print(f"  Covering {o['qty']} shares of {ticker} (${abs(o['delta_dollars']):.2f})...")
            place_order(ticker, "buy", qty=o["qty"])
        elif o["side"] == "close_short":
            print(f"  Closing short {ticker}...")
            close_position(ticker)
        time.sleep(0.3)

    check_and_reduce_margin(dry_run=dry_run)

    state = {
        "timestamp": datetime.datetime.now().isoformat(),
        "date": datetime.date.today().isoformat(),
        "weights": tradeable,
        "short_weights": tradeable_short,
        "portfolio_value": portfolio_value,
        "total_allocated_pct": total_allocated_pct,
        "total_short_pct": total_short_pct,
    }
    save_state(state)
    print(f"\nDone. State saved to {STATE_FILE}")


if __name__ == "__main__":
    import sys
    dry_run = "--dry-run" in sys.argv
    run_sync(dry_run=dry_run)
