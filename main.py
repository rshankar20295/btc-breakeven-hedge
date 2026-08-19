"""
Delta Exchange - BTC Options Short Strangle Strategy (v3 - Breakeven Hedge Edition)
=====================================================================================
This is a NEW, SEPARATE version. It does NOT modify or replace main.py (v1) or the
v2 SL/Target/Trailing-SL script - deploy this as its own repo/service.

Sells 1 Call + 1 Put on BTC options, strike selected by nearest-match delta (any
delta, including deep ITM values like 0.7 / -0.7). Entry within a user-defined
IST time window. NO stop loss, trailing stop loss, or target exit exists in this
version - positions are intended to run to natural expiry/settlement on the
exchange.

WHAT CHANGED FROM v2 (SL/Target/Trailing-SL version):
  - REMOVED: Fixed SL, Trailing SL, Target, and time-based force-exit. None of
    that logic exists in this file.
  - ADDED: Breakeven-based ONE-TIME hedge per side:
      * Breakeven levels are computed using TIME VALUE (real max profit
        potential), NOT raw premium collected - this matters especially for
        ITM strikes where premium looks large but time value (true max
        profit) is much smaller.
      * If underlying spot price closes ABOVE the upside breakeven, the bot
        BUYS (goes long) a NEW, ADDITIONAL Call option at the ATM strike
        (or the next strike closer to spot if ATM happens to collide with
        the already-sold Call's strike, to avoid netting/closing the
        existing short). This is a hedge added ON TOP of the existing short
        Call - the original short Call is NEVER closed by this logic.
      * Same logic mirrored for the Put side on a downside breach.
      * Each side hedges AT MOST ONCE per trade - it will never re-trigger
        even if price crosses back and forth across the breakeven multiple
        times.
      * If the hedge buy order fails for ANY reason (insufficient balance,
        API error, no liquidity, etc.), the bot treats this as an
        emergency: it immediately market-closes the ENTIRE position (all
        legs, including any hedge already active on the other side) to
        avoid running a naked, un-hedged, un-stopped short position.
  - Settlement is NOT bot-initiated. The bot does not place any closing
    order at expiry. It simply detects that the fixed daily settlement time
    (17:30 IST) has passed on the trade's expiry date, logs a final summary,
    and resets state to search for the next entry.

IMPORTANT WARNINGS (READ CAREFULLY):
  - This is a NAKED SHORT OPTIONS strategy with NO stop loss whatsoever on
    the original short legs. The hedge added on breakeven breach REDUCES
    the rate of further loss on that side, but it does NOT cap losses to
    zero, and does not protect against gap moves between polling cycles.
  - Delta Exchange's own risk engine may liquidate a naked short position
    for margin shortfall BEFORE your breakeven is reached or before your
    bot's next poll cycle runs. This risk is materially higher for ITM
    delta selections (e.g., 0.7 / -0.7) versus OTM deltas.
  - Test on TESTNET first (set DELTA_BASE_URL env var to testnet URL).
  - All configuration is via environment variables (set in Railway dashboard).
  - Railway's default networking may assign a DIFFERENT outbound IP on every
    redeploy/restart unless a static IP feature is enabled on your plan.
    Re-check the IP banner after every redeploy and update Delta's whitelist.
  - Settlement detection in this script relies on a FIXED time check
    (17:30 IST) rather than an exchange "settled" event/webhook. If Delta's
    actual settlement/position-clearing timing ever differs from this
    assumption, the final P&L summary logged by the bot may not perfectly
    match what actually happened on the exchange. Always cross-check final
    P&L against your Delta Exchange fills/positions history.
"""

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests

# ======================================================================
# CONFIG - LOADED FROM ENVIRONMENT VARIABLES (set these in Railway)
# ======================================================================

BASE_URL = os.environ.get("DELTA_BASE_URL", "https://cdn-ind.testnet.deltaex.org")
API_KEY = os.environ.get("DELTA_API_KEY")
API_SECRET = os.environ.get("DELTA_API_SECRET")

if not API_KEY or not API_SECRET:
    raise EnvironmentError(
        "DELTA_API_KEY and DELTA_API_SECRET must be set as environment variables."
    )

UNDERLYING_ASSET = os.environ.get("UNDERLYING_ASSET", "BTC")

CALL_TARGET_DELTA = float(os.environ.get("CALL_TARGET_DELTA", "0.40"))
PUT_TARGET_DELTA = float(os.environ.get("PUT_TARGET_DELTA", "-0.40"))

EXPIRY_N_DAYS = int(os.environ.get("EXPIRY_N_DAYS", "7"))
MAX_EXPIRY_SEARCH_DAYS = int(os.environ.get("MAX_EXPIRY_SEARCH_DAYS", "15"))

CALL_LOTS = int(os.environ.get("CALL_LOTS", "1"))
PUT_LOTS = int(os.environ.get("PUT_LOTS", "1"))

# Entry window: 7:00 PM - 7:15 PM IST (confirmed)
ENTRY_WINDOW_START = os.environ.get("ENTRY_WINDOW_START", "19:00")
ENTRY_WINDOW_END = os.environ.get("ENTRY_WINDOW_END", "19:15")

# Fixed daily settlement time assumption for options - NOT configurable,
# hardcoded per confirmed requirement (5:30 PM IST).
SETTLEMENT_TIME_IST = "17:30"

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "10"))
WAITING_LOG_INTERVAL_SECONDS = int(os.environ.get("WAITING_LOG_INTERVAL_SECONDS", "300"))

STATE_FILE = os.environ.get("STATE_FILE_PATH", "state.json")

# ======================================================================
# IST TIMEZONE HELPER
# ======================================================================
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist():
    return datetime.now(IST)


def parse_hhmm(hhmm_str):
    h, m = map(int, hhmm_str.split(":"))
    return h, m


def format_timedelta(td):
    total_seconds = int(td.total_seconds())
    if total_seconds < 0:
        return "0d 0h 0m (past due)"
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    return f"{days}d {hours}h {minutes}m"


def combine_expiry(expiry_date_obj):
    """
    Approximate expiry moment for DISPLAY purposes only (the "Time to
    Expiry" countdown in monitoring logs). The actual settlement detection
    used by the bot is governed independently by SETTLEMENT_TIME_IST.
    """
    return datetime(
        expiry_date_obj.year, expiry_date_obj.month, expiry_date_obj.day,
        21, 30, 0, tzinfo=IST
    )


def get_time_to_expiry_str(expiry_date_iso):
    expiry_date_obj = datetime.fromisoformat(expiry_date_iso).date()
    expiry_dt = combine_expiry(expiry_date_obj)
    remaining = expiry_dt - now_ist()
    return format_timedelta(remaining)


# ======================================================================
# OUTBOUND IP DETECTION (for Delta API key IP whitelisting)
# ======================================================================

def get_public_ip():
    services = [
        "https://api.ipify.org?format=json",
        "https://ifconfig.me/all.json",
    ]
    for service_url in services:
        try:
            resp = requests.get(service_url, timeout=10)
            data = resp.json()
            ip = data.get("ip") or data.get("ip_addr")
            if ip:
                return ip
        except requests.exceptions.RequestException:
            continue
        except ValueError:
            continue
    return None


def print_ip_banner():
    ip = get_public_ip()
    print("")
    print("#" * 70)
    if ip:
        print(f"#  OUTBOUND PUBLIC IP (whitelist this on Delta): {ip}")
    else:
        print("#  COULD NOT DETECT OUTBOUND PUBLIC IP - check network/connectivity")
    print("#  NOTE: Railway's default networking may assign a DIFFERENT IP")
    print("#  on every redeploy/restart unless a static IP feature is enabled")
    print("#  on your Railway plan. Re-check this banner after every redeploy.")
    print("#" * 70)
    print("")


# ======================================================================
# AUTH / REQUEST HELPERS
# ======================================================================

def generate_signature(secret, message):
    message = bytes(message, "utf-8")
    secret = bytes(secret, "utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def send_request(method, path, query_params=None, body=None):
    timestamp = str(int(time.time()))
    query_string = ""
    if query_params:
        query_string = "?" + "&".join(f"{k}={v}" for k, v in query_params.items())

    payload = json.dumps(body) if body else ""

    signature_data = method + timestamp + path + query_string + payload
    signature = generate_signature(API_SECRET, signature_data)

    headers = {
        "api-key": API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "User-Agent": "btc-strangle-breakeven-hedge-bot-v3",
        "Content-Type": "application/json",
    }

    url = BASE_URL + path
    try:
        resp = requests.request(
            method,
            url,
            params=query_params,
            data=payload if body else None,
            headers=headers,
            timeout=(5, 30),
        )
        return resp.json()
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Request failed: {method} {path} - {e}")
        return None
    except ValueError:
        print(f"[ERROR] Non-JSON response: {method} {path}")
        return None


# ======================================================================
# STATE PERSISTENCE
# ======================================================================

def default_state():
    return {
        "active": False,
        "last_entry_date": None,
        "expiry_date_iso": None,
        "expiry_date_str": None,
        "target_expiry_date_str": None,
        "spot_price_at_entry": None,

        "call_product_id": None,
        "call_symbol": None,
        "call_strike": None,
        "call_delta": None,
        "call_entry_price": None,
        "call_lots": None,
        "call_contract_value": None,
        "call_premium": None,
        "call_intrinsic": None,
        "call_time_value": None,
        "call_last_mark": None,

        "put_product_id": None,
        "put_symbol": None,
        "put_strike": None,
        "put_delta": None,
        "put_entry_price": None,
        "put_lots": None,
        "put_contract_value": None,
        "put_premium": None,
        "put_intrinsic": None,
        "put_time_value": None,
        "put_last_mark": None,

        "total_premium_received": None,
        "total_time_value": None,

        "upside_breakeven": None,
        "downside_breakeven": None,
        "max_profit_potential": None,
        "margin_utilized_at_entry": None,

        "call_hedge_triggered": False,
        "call_hedge_product_id": None,
        "call_hedge_symbol": None,
        "call_hedge_strike": None,
        "call_hedge_entry_price": None,
        "call_hedge_lots": None,
        "call_hedge_last_mark": None,

        "put_hedge_triggered": False,
        "put_hedge_product_id": None,
        "put_hedge_symbol": None,
        "put_hedge_strike": None,
        "put_hedge_entry_price": None,
        "put_hedge_lots": None,
        "put_hedge_last_mark": None,

        "trade_force_closed": False,
        "trade_force_close_reason": None,
    }


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                loaded = json.load(f)
                merged = default_state()
                merged.update(loaded)
                return merged
        except (json.JSONDecodeError, IOError):
            print("[WARN] Could not read state file, starting fresh.")
    return default_state()


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ======================================================================
# MARKET DATA HELPERS
# ======================================================================

def get_option_chain(expiry_date_ddmmyyyy):
    path = "/v2/tickers"
    params = {
        "contract_types": "call_options,put_options",
        "underlying_asset_symbols": UNDERLYING_ASSET,
        "expiry_date": expiry_date_ddmmyyyy,
    }
    resp = send_request("GET", path, query_params=params)
    if not resp or not resp.get("success"):
        print(f"[ERROR] Failed to fetch option chain for {expiry_date_ddmmyyyy}: {resp}")
        return []
    return resp.get("result", [])


def find_available_expiry(n_days, max_search_days):
    """
    Forward-fallback expiry search (unchanged from v2). Computes target
    expiry as today + n_days. If no chain listed, searches forward day by
    day up to max_search_days, and uses the nearest available expiry with
    a non-empty chain. The N-day target is always recalculated fresh at
    each new entry attempt, so re-entries after a long fallback (e.g. 15
    days) will again target N days from THAT point in time.
    """
    today = now_ist().date()
    target_date = today + timedelta(days=n_days)
    target_date_str = target_date.strftime("%d-%m-%Y")

    candidate_date = target_date
    while (candidate_date - today).days <= max_search_days:
        candidate_str = candidate_date.strftime("%d-%m-%Y")
        chain = get_option_chain(candidate_str)
        if chain:
            if candidate_date != target_date:
                print(f"[INFO] Target expiry {target_date_str} not available. "
                      f"Falling back to nearest available expiry: {candidate_str}")
            return candidate_date, candidate_str, chain, target_date_str
        candidate_date += timedelta(days=1)

    print(f"[WARN] No option chain found for target expiry {target_date_str} or any "
          f"expiry within {max_search_days} days from today. Skipping this window.")
    return None, None, [], target_date_str


def select_strike_by_delta(chain, contract_type, target_delta):
    candidates = []
    for ticker in chain:
        if ticker.get("contract_type") != contract_type:
            continue
        greeks = ticker.get("greeks") or {}
        delta_str = greeks.get("delta")
        if delta_str is None:
            continue
        try:
            delta_val = float(delta_str)
        except ValueError:
            continue
        candidates.append((abs(delta_val - target_delta), ticker))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def select_hedge_strike(chain, contract_type, spot_price, original_sold_strike):
    """
    Selects the ATM strike (nearest to current spot) for the hedge leg.
    If that ATM strike is the SAME as the strike already sold (which would
    net/close the existing short instead of adding a hedge), falls back to
    the NEXT strike closer to spot among the remaining candidates.
    Returns the ticker dict, or None if no suitable strike is found.
    """
    candidates = [t for t in chain if t.get("contract_type") == contract_type]
    if not candidates:
        return None

    def strike_distance(ticker):
        try:
            return abs(float(ticker.get("strike_price")) - spot_price)
        except (TypeError, ValueError):
            return float("inf")

    candidates_sorted = sorted(candidates, key=strike_distance)
    atm_ticker = candidates_sorted[0]

    try:
        atm_strike = float(atm_ticker.get("strike_price"))
        sold_strike = float(original_sold_strike) if original_sold_strike is not None else None
    except (TypeError, ValueError):
        atm_strike, sold_strike = None, None

    if sold_strike is not None and atm_strike is not None and atm_strike == sold_strike:
        remaining = candidates_sorted[1:]
        if not remaining:
            return None
        return remaining[0]  # next strike closest to spot after excluding the collision

    return atm_ticker


def get_mark_price(product_id, symbol):
    path = f"/v2/tickers/{symbol}"
    resp = send_request("GET", path)
    if not resp or not resp.get("success"):
        print(f"[ERROR] Failed to fetch ticker for {symbol}: {resp}")
        return None
    result = resp.get("result") or {}
    try:
        return float(result.get("mark_price"))
    except (TypeError, ValueError):
        return None


def get_underlying_spot_price():
    """
    Fetches the underlying index/spot price used for breakeven breach
    comparisons. Uses Delta's index symbol convention (.DE prefix). BTC is
    a special case (.DEXBTUSD) per Delta's symbology documentation.
    """
    if UNDERLYING_ASSET.upper() == "BTC":
        index_symbol = ".DEXBTUSD"
    else:
        index_symbol = f".DE{UNDERLYING_ASSET.upper()}USD"

    path = f"/v2/tickers/{index_symbol}"
    resp = send_request("GET", path)
    if not resp or not resp.get("success"):
        print(f"[ERROR] Failed to fetch underlying spot price for {index_symbol}: {resp}")
        return None
    result = resp.get("result") or {}
    try:
        price = result.get("spot_price") or result.get("mark_price")
        return float(price)
    except (TypeError, ValueError):
        return None


def get_contract_value(symbol):
    path = f"/v2/products/{symbol}"
    resp = send_request("GET", path)
    if not resp or not resp.get("success"):
        print(f"[ERROR] Failed to fetch product details for {symbol}: {resp}")
        return None
    result = resp.get("result") or {}
    try:
        return float(result.get("contract_value"))
    except (TypeError, ValueError):
        return None


def get_position(product_id):
    path = "/v2/positions"
    params = {"product_id": product_id}
    resp = send_request("GET", path, query_params=params)
    if not resp or not resp.get("success"):
        return None
    return resp.get("result")


def get_live_position_size(product_id):
    pos = get_position(product_id)
    if pos is None:
        return 0
    try:
        return int(pos.get("size", 0))
    except (TypeError, ValueError):
        return 0


def get_margin_and_pnl_for_products(product_ids):
    """
    Fetches margin and realized_pnl for a list of product_ids using
    /v2/positions/margined. Returns a dict keyed by product_id with
    {"margin": float, "realized_pnl": float or None}.
    NOTE: Once a position is fully settled/closed by the exchange, it may
    no longer appear in this endpoint's response. Callers should treat a
    missing product_id in the result as "unavailable" and fall back to
    last-known mark-price-based estimates if needed.
    """
    if not product_ids:
        return {}
    path = "/v2/positions/margined"
    params = {"product_ids": ",".join(str(pid) for pid in product_ids)}
    resp = send_request("GET", path, query_params=params)
    if not resp or not resp.get("success"):
        print(f"[WARN] Could not fetch margin/pnl data: {resp}")
        return {}
    results = resp.get("result") or []
    output = {}
    for pos in results:
        pid = pos.get("product_id")
        try:
            margin = float(pos.get("margin") or 0)
        except (TypeError, ValueError):
            margin = 0.0
        try:
            realized_pnl = float(pos.get("realized_pnl")) if pos.get("realized_pnl") is not None else None
        except (TypeError, ValueError):
            realized_pnl = None
        output[pid] = {"margin": margin, "realized_pnl": realized_pnl}
    return output


# ======================================================================
# ORDER HELPERS
# ======================================================================

def place_market_order(product_id, side, size, reduce_only=False):
    path = "/v2/orders"
    body = {
        "product_id": product_id,
        "size": size,
        "side": side,
        "order_type": "market_order",
    }
    if reduce_only:
        body["reduce_only"] = "true"

    resp = send_request("POST", path, body=body)
    if not resp or not resp.get("success"):
        print(f"[ERROR] Order placement failed: {body} -> {resp}")
        return None
    return resp.get("result")


def wait_for_fill_and_get_entry_price(product_id, retries=6, delay=2):
    for _ in range(retries):
        pos = get_position(product_id)
        if pos and int(pos.get("size", 0)) != 0:
            return float(pos.get("entry_price")), int(pos.get("size"))
        time.sleep(delay)
    return None, None


def ensure_leg_closed(product_id, symbol, label):
    """
    Ensures a leg is closed on the exchange. Checks LIVE position first -
    if already flat, treats as success without placing a redundant order.
    Returns True if confirmed flat, False otherwise.
    """
    live_size = get_live_position_size(product_id)

    if live_size == 0:
        print(f"[INFO] {label} ({symbol}) is already flat on the exchange. No close order needed.")
        return True

    side = "buy" if live_size < 0 else "sell"
    result = place_market_order(product_id, side, abs(live_size), reduce_only=True)
    if result is None:
        print(f"[ERROR] Close order for {label} ({symbol}) was rejected by the exchange.")
        return False

    for _ in range(5):
        time.sleep(2)
        if get_live_position_size(product_id) == 0:
            return True

    return False


# ======================================================================
# BREAKEVEN CALCULATION
# ======================================================================

def calculate_breakeven_levels(call_strike, put_strike, total_time_value,
                                call_contract_value, call_size):
    """
    Breakeven based on REAL MAX PROFIT (time value), not raw premium, per
    confirmed requirement. Assumes call_size and put_size are equal (per
    confirmed requirement that hedge/entry quantity must be identical on
    both sides).

    Upside Breakeven  = Call Strike + (Total Time Value / total underlying units)
    Downside Breakeven = Put Strike - (Total Time Value / total underlying units)
    """
    try:
        total_units = call_contract_value * abs(call_size)
        if total_units == 0:
            return None, None
        time_value_per_unit = total_time_value / total_units
        upside = float(call_strike) + time_value_per_unit
        downside = float(put_strike) - time_value_per_unit
        return upside, downside
    except (TypeError, ValueError, ZeroDivisionError):
        return None, None


# ======================================================================
# STRATEGY LOGIC - ENTRY
# ======================================================================

def attempt_entry(state):
    print(f"[INFO] Attempting entry. Target: today + {EXPIRY_N_DAYS} day(s). "
          f"Fallback search window: up to {MAX_EXPIRY_SEARCH_DAYS} days from today.")

    expiry_date_obj, expiry_str, chain, target_date_str = find_available_expiry(
        EXPIRY_N_DAYS, MAX_EXPIRY_SEARCH_DAYS
    )

    if not chain:
        return state

    call_ticker = select_strike_by_delta(chain, "call_options", CALL_TARGET_DELTA)
    put_ticker = select_strike_by_delta(chain, "put_options", PUT_TARGET_DELTA)

    if not call_ticker or not put_ticker:
        print("[WARN] Could not find matching call/put strikes by delta. Skipping this window.")
        return state

    call_product_id = call_ticker["product_id"]
    call_symbol = call_ticker["symbol"]
    call_strike = call_ticker.get("strike_price")
    call_delta = call_ticker.get("greeks", {}).get("delta")

    put_product_id = put_ticker["product_id"]
    put_symbol = put_ticker["symbol"]
    put_strike = put_ticker.get("strike_price")
    put_delta = put_ticker.get("greeks", {}).get("delta")

    spot_price_at_entry = None
    try:
        spot_price_at_entry = float(call_ticker.get("spot_price"))
    except (TypeError, ValueError):
        try:
            spot_price_at_entry = float(put_ticker.get("spot_price"))
        except (TypeError, ValueError):
            spot_price_at_entry = None

    call_contract_value = get_contract_value(call_symbol)
    put_contract_value = get_contract_value(put_symbol)

    if call_contract_value is None or put_contract_value is None:
        print("[ERROR] Could not fetch contract value for one or both legs. Aborting this entry attempt.")
        return state

    print(f"[INFO] Selected expiry: {expiry_str} (target was {target_date_str})")
    print(f"[INFO] Selected CALL: {call_symbol} | Strike: {call_strike} | Delta: {call_delta} | "
          f"Qty: {CALL_LOTS} lot(s)")
    print(f"[INFO] Selected PUT:  {put_symbol} | Strike: {put_strike} | Delta: {put_delta} | "
          f"Qty: {PUT_LOTS} lot(s)")
    print(f"[INFO] Spot price at entry: {spot_price_at_entry}")

    call_order = place_market_order(call_product_id, "sell", CALL_LOTS)
    if not call_order:
        print("[ERROR] CALL leg order failed to place. No position opened. Aborting entry attempt.")
        return state

    call_entry_price, call_size = wait_for_fill_and_get_entry_price(call_product_id)
    if call_entry_price is None:
        print("[ERROR] CALL leg order was placed but fill could not be confirmed. "
              "Attempting to close any partial fill to avoid an orphaned position.")
        ensure_leg_closed(call_product_id, call_symbol, "CALL")
        return state

    put_order = place_market_order(put_product_id, "sell", PUT_LOTS)
    if not put_order:
        print("[ERROR] PUT leg order failed to place. Rolling back the already-filled CALL leg "
              "to avoid an orphaned naked position.")
        ensure_leg_closed(call_product_id, call_symbol, "CALL")
        return state

    put_entry_price, put_size = wait_for_fill_and_get_entry_price(put_product_id)
    if put_entry_price is None:
        print("[ERROR] PUT leg order was placed but fill could not be confirmed. "
              "Rolling back the CALL leg and any partial PUT fill to avoid an orphaned position.")
        ensure_leg_closed(call_product_id, call_symbol, "CALL")
        ensure_leg_closed(put_product_id, put_symbol, "PUT")
        return state

    call_premium = call_entry_price * call_contract_value * abs(call_size)
    put_premium = put_entry_price * put_contract_value * abs(put_size)
    total_premium = call_premium + put_premium

    call_strike_f = float(call_strike) if call_strike is not None else None
    put_strike_f = float(put_strike) if put_strike is not None else None

    call_intrinsic_per_unit = 0.0
    put_intrinsic_per_unit = 0.0
    if spot_price_at_entry is not None and call_strike_f is not None:
        call_intrinsic_per_unit = max(spot_price_at_entry - call_strike_f, 0.0)
    if spot_price_at_entry is not None and put_strike_f is not None:
        put_intrinsic_per_unit = max(put_strike_f - spot_price_at_entry, 0.0)

    call_intrinsic = call_intrinsic_per_unit * call_contract_value * abs(call_size)
    put_intrinsic = put_intrinsic_per_unit * put_contract_value * abs(put_size)

    call_time_value = max(call_premium - call_intrinsic, 0.0)
    put_time_value = max(put_premium - put_intrinsic, 0.0)
    total_time_value = call_time_value + put_time_value

    if total_time_value <= 0:
        print("[WARN] Computed max profit potential (time value) is zero or negative. "
              "This can happen for deep ITM strikes near expiry.")

    upside_breakeven, downside_breakeven = calculate_breakeven_levels(
        call_strike_f, put_strike_f, total_time_value, call_contract_value, call_size
    )

    margin_data = get_margin_and_pnl_for_products([call_product_id, put_product_id])
    margin_utilized_at_entry = 0.0
    for pid in (call_product_id, put_product_id):
        entry = margin_data.get(pid)
        if entry:
            margin_utilized_at_entry += entry.get("margin", 0.0)

    state.update({
        "active": True,
        "last_entry_date": now_ist().date().isoformat(),
        "expiry_date_iso": expiry_date_obj.isoformat(),
        "expiry_date_str": expiry_str,
        "target_expiry_date_str": target_date_str,
        "spot_price_at_entry": spot_price_at_entry,

        "call_product_id": call_product_id,
        "call_symbol": call_symbol,
        "call_strike": call_strike,
        "call_delta": call_delta,
        "call_entry_price": call_entry_price,
        "call_lots": call_size,
        "call_contract_value": call_contract_value,
        "call_premium": call_premium,
        "call_intrinsic": call_intrinsic,
        "call_time_value": call_time_value,
        "call_last_mark": call_entry_price,

        "put_product_id": put_product_id,
        "put_symbol": put_symbol,
        "put_strike": put_strike,
        "put_delta": put_delta,
        "put_entry_price": put_entry_price,
        "put_lots": put_size,
        "put_contract_value": put_contract_value,
        "put_premium": put_premium,
        "put_intrinsic": put_intrinsic,
        "put_time_value": put_time_value,
        "put_last_mark": put_entry_price,

        "total_premium_received": total_premium,
        "total_time_value": total_time_value,

        "upside_breakeven": upside_breakeven,
        "downside_breakeven": downside_breakeven,
        "max_profit_potential": total_time_value,
        "margin_utilized_at_entry": margin_utilized_at_entry,

        "call_hedge_triggered": False,
        "call_hedge_product_id": None,
        "call_hedge_symbol": None,
        "call_hedge_strike": None,
        "call_hedge_entry_price": None,
        "call_hedge_lots": None,
        "call_hedge_last_mark": None,

        "put_hedge_triggered": False,
        "put_hedge_product_id": None,
        "put_hedge_symbol": None,
        "put_hedge_strike": None,
        "put_hedge_entry_price": None,
        "put_hedge_lots": None,
        "put_hedge_last_mark": None,

        "trade_force_closed": False,
        "trade_force_close_reason": None,
    })
    save_state(state)

    time_to_expiry = get_time_to_expiry_str(state["expiry_date_iso"])

    print("=" * 70)
    print("[ENTRY SUMMARY]")
    print(f"Target Expiry: {target_date_str} | Actual Expiry Used: {expiry_str} | "
          f"Time to Expiry: {time_to_expiry}")
    print(f"Spot Price at Entry: {spot_price_at_entry}")
    print(f"CALL {call_symbol} | Strike: {call_strike} | Delta: {call_delta} | Qty: {call_size} lot(s)")
    print(f"     Entry Price: {call_entry_price} | Premium: ${call_premium:.4f} | "
          f"Intrinsic: ${call_intrinsic:.4f} | Time Value: ${call_time_value:.4f}")
    print(f"PUT  {put_symbol} | Strike: {put_strike} | Delta: {put_delta} | Qty: {put_size} lot(s)")
    print(f"     Entry Price: {put_entry_price} | Premium: ${put_premium:.4f} | "
          f"Intrinsic: ${put_intrinsic:.4f} | Time Value: ${put_time_value:.4f}")
    print(f"TOTAL PREMIUM RECEIVED: ${total_premium:.4f}")
    print(f"TOTAL TIME VALUE (Real Max Profit Potential, no breakout): ${total_time_value:.4f}")
    print(f"UPSIDE BREAKEVEN (Call hedge triggers above this): "
          f"{'%.4f' % upside_breakeven if upside_breakeven is not None else 'N/A'}")
    print(f"DOWNSIDE BREAKEVEN (Put hedge triggers below this): "
          f"{'%.4f' % downside_breakeven if downside_breakeven is not None else 'N/A'}")
    print(f"MARGIN UTILIZED AT ENTRY: ${margin_utilized_at_entry:.4f}")
    print("=" * 70)

    return state


# ======================================================================
# STRATEGY LOGIC - HEDGE TRIGGER
# ======================================================================

def execute_hedge_leg(state, side_label, original_strike, target_delta_unused,
                       contract_type, lots, spot_price):
    """
    Attempts to place the hedge BUY order for the given side ('CALL' or
    'PUT'). Returns (success: bool, updated_state).
    On any failure (strike selection failure, order rejection, fill not
    confirmed), returns success=False - caller is responsible for forcing
    a full position close.
    """
    chain = get_option_chain(state["expiry_date_str"])
    if not chain:
        print(f"[ERROR] Could not fetch option chain for hedge leg ({side_label}).")
        return False, state

    hedge_ticker = select_hedge_strike(chain, contract_type, spot_price, original_strike)
    if not hedge_ticker:
        print(f"[ERROR] Could not select a valid hedge strike for {side_label} "
              f"(no suitable non-colliding strike found).")
        return False, state

    hedge_product_id = hedge_ticker["product_id"]
    hedge_symbol = hedge_ticker["symbol"]
    hedge_strike = hedge_ticker.get("strike_price")

    order = place_market_order(hedge_product_id, "buy", lots)
    if not order:
        print(f"[ERROR] Hedge BUY order for {side_label} ({hedge_symbol}) failed to place "
              f"(possibly insufficient balance or API error).")
        return False, state

    entry_price, size = wait_for_fill_and_get_entry_price(hedge_product_id)
    if entry_price is None:
        print(f"[ERROR] Hedge BUY order for {side_label} ({hedge_symbol}) was placed but "
              f"fill could not be confirmed.")
        return False, state

    prefix = side_label.lower()
    state[f"{prefix}_hedge_triggered"] = True
    state[f"{prefix}_hedge_product_id"] = hedge_product_id
    state[f"{prefix}_hedge_symbol"] = hedge_symbol
    state[f"{prefix}_hedge_strike"] = hedge_strike
    state[f"{prefix}_hedge_entry_price"] = entry_price
    state[f"{prefix}_hedge_lots"] = size
    state[f"{prefix}_hedge_last_mark"] = entry_price
    save_state(state)

    print(f"[HEDGE TRIGGERED] {side_label} breakeven breached at spot {spot_price}. "
          f"Bought {side_label} hedge {hedge_symbol} (Strike: {hedge_strike}) "
          f"Qty: {size} @ {entry_price}. This hedge will NOT re-trigger for the rest of this trade.")

    return True, state


def force_close_full_position(state, reason):
    """
    Emergency full close of ALL legs (original shorts + any active hedge
    legs), triggered when a hedge order fails. Captures approximate exit
    marks before closing for the final summary, then resets state.
    """
    print(f"[FORCE CLOSE] Reason: {reason}. Closing ALL open legs immediately to avoid "
          f"a naked, un-hedged position.")

    legs = []
    if state.get("call_product_id"):
        legs.append(("call", state["call_product_id"], state["call_symbol"]))
    if state.get("put_product_id"):
        legs.append(("put", state["put_product_id"], state["put_symbol"]))
    if state.get("call_hedge_triggered") and state.get("call_hedge_product_id"):
        legs.append(("call_hedge", state["call_hedge_product_id"], state["call_hedge_symbol"]))
    if state.get("put_hedge_triggered") and state.get("put_hedge_product_id"):
        legs.append(("put_hedge", state["put_hedge_product_id"], state["put_hedge_symbol"]))

    for prefix, product_id, symbol in legs:
        mark = get_mark_price(product_id, symbol)
        if mark is not None:
            state[f"{prefix}_last_mark"] = mark
        closed = ensure_leg_closed(product_id, symbol, prefix.upper())
        if not closed:
            print(f"[ERROR] Could not confirm closure of {prefix.upper()} ({symbol}) during force close. "
                  f"MANUAL REVIEW REQUIRED IMMEDIATELY.")

    state["trade_force_closed"] = True
    state["trade_force_close_reason"] = reason

    print_final_trade_summary(state, natural_settlement=False)

    fresh = default_state()
    fresh["last_entry_date"] = state.get("last_entry_date")
    save_state(fresh)
    return fresh


def check_and_trigger_hedge(state, spot_price):
    """
    Checks both breakeven levels against current spot price and triggers
    the one-time hedge on whichever side(s) are breached. On hedge
    failure, forces a full close of the entire position.
    Returns the (possibly updated/reset) state.
    """
    upside = state.get("upside_breakeven")
    downside = state.get("downside_breakeven")

    if (not state.get("call_hedge_triggered")) and upside is not None and spot_price > upside:
        success, state = execute_hedge_leg(
            state, "CALL", state.get("call_strike"), None,
            "call_options", abs(state.get("call_lots") or CALL_LOTS), spot_price
        )
        if not success:
            return force_close_full_position(
                state, "HEDGE_ORDER_FAILED_CALL_SIDE_INSUFFICIENT_BALANCE_OR_ERROR"
            )

    if not state.get("active"):
        return state  # position may have just been force-closed above

    if (not state.get("put_hedge_triggered")) and downside is not None and spot_price < downside:
        success, state = execute_hedge_leg(
            state, "PUT", state.get("put_strike"), None,
            "put_options", abs(state.get("put_lots") or PUT_LOTS), spot_price
        )
        if not success:
            return force_close_full_position(
                state, "HEDGE_ORDER_FAILED_PUT_SIDE_INSUFFICIENT_BALANCE_OR_ERROR"
            )

    return state


# ======================================================================
# STRATEGY LOGIC - SETTLEMENT DETECTION & SUMMARY
# ======================================================================

def is_settlement_time_reached(state):
    """
    Fixed daily settlement assumption: 17:30 IST. Returns True once the
    current IST time has passed 17:30 IST on (or after) the trade's
    expiry date.
    """
    settlement_h, settlement_m = parse_hhmm(SETTLEMENT_TIME_IST)
    now = now_ist()
    expiry_date_obj = datetime.fromisoformat(state["expiry_date_iso"]).date()

    if now.date() > expiry_date_obj:
        return True
    if now.date() == expiry_date_obj and (now.hour, now.minute) >= (settlement_h, settlement_m):
        return True
    return False


def print_final_trade_summary(state, natural_settlement=True):
    """
    Prints the final P&L summary for the trade, in both dollar amount and
    percentage (percentage is measured against margin_utilized_at_entry,
    a fixed baseline captured right after entry).

    For natural settlement: attempts to use realized_pnl from
    /v2/positions/margined for each leg still tracked. If a leg's
    realized_pnl is unavailable (e.g., already cleared from the API after
    settlement), falls back to an estimate using the last known mark price
    recorded during monitoring.
    """
    product_ids = []
    for prefix in ("call", "put", "call_hedge", "put_hedge"):
        pid = state.get(f"{prefix}_product_id")
        if pid:
            product_ids.append(pid)

    pnl_data = get_margin_and_pnl_for_products(product_ids) if natural_settlement else {}

    def leg_pnl(prefix, is_short):
        pid = state.get(f"{prefix}_product_id")
        entry_price = state.get(f"{prefix}_entry_price")
        lots = state.get(f"{prefix}_lots")
        cv = state.get(f"{prefix}_contract_value") or state.get("call_contract_value") or 0

        if pid and pnl_data.get(pid) and pnl_data[pid].get("realized_pnl") is not None:
            return pnl_data[pid]["realized_pnl"], "realized_pnl (exchange-confirmed)"

        last_mark = state.get(f"{prefix}_last_mark")
        if entry_price is None or last_mark is None or lots is None:
            return None, "unavailable"

        if is_short:
            pnl = (entry_price - last_mark) * cv * abs(lots)
        else:
            pnl = (last_mark - entry_price) * cv * abs(lots)
        return pnl, "estimated (last known mark price, not exchange-confirmed)"

    call_pnl, call_src = leg_pnl("call", is_short=True)
    put_pnl, put_src = leg_pnl("put", is_short=True)

    call_hedge_pnl, call_hedge_src = (None, "not triggered")
    if state.get("call_hedge_triggered"):
        call_hedge_pnl, call_hedge_src = leg_pnl("call_hedge", is_short=False)

    put_hedge_pnl, put_hedge_src = (None, "not triggered")
    if state.get("put_hedge_triggered"):
        put_hedge_pnl, put_hedge_src = leg_pnl("put_hedge", is_short=False)

    total_pnl = 0.0
    any_unavailable = False
    for pnl in (call_pnl, put_pnl, call_hedge_pnl, put_hedge_pnl):
        if pnl is None:
            any_unavailable = True
        else:
            total_pnl += pnl

    margin_base = state.get("margin_utilized_at_entry") or 0
    pnl_percent = (total_pnl / margin_base * 100.0) if margin_base else None

    print("=" * 70)
    print(f"[FINAL TRADE SUMMARY - {'NATURAL SETTLEMENT' if natural_settlement else 'FORCE-CLOSED'}]")
    if not natural_settlement:
        print(f"Force Close Reason: {state.get('trade_force_close_reason')}")
    print(f"CALL {state.get('call_symbol')} | Strike: {state.get('call_strike')} | "
          f"P&L: {'$%.4f' % call_pnl if call_pnl is not None else 'N/A'} [{call_src}]")
    print(f"PUT  {state.get('put_symbol')} | Strike: {state.get('put_strike')} | "
          f"P&L: {'$%.4f' % put_pnl if put_pnl is not None else 'N/A'} [{put_src}]")
    print(f"CALL HEDGE Triggered: {state.get('call_hedge_triggered')} | "
          f"Symbol: {state.get('call_hedge_symbol')} | "
          f"P&L: {'$%.4f' % call_hedge_pnl if call_hedge_pnl is not None else 'N/A'} [{call_hedge_src}]")
    print(f"PUT HEDGE  Triggered: {state.get('put_hedge_triggered')} | "
          f"Symbol: {state.get('put_hedge_symbol')} | "
          f"P&L: {'$%.4f' % put_hedge_pnl if put_hedge_pnl is not None else 'N/A'} [{put_hedge_src}]")
    print(f"Max Profit Potential (if no breakout had occurred): "
          f"${state.get('max_profit_potential', 0):.4f}")
    print(f"Margin Utilized At Entry (fixed baseline for %): ${margin_base:.4f}")
    print(f"TOTAL COMBINED P&L: {'$%.4f' % total_pnl if not any_unavailable else '$%.4f (partial, some legs unavailable)' % total_pnl}")
    print(f"TOTAL P&L %: {'%.2f%%' % pnl_percent if pnl_percent is not None else 'N/A'}")
    if any_unavailable:
        print("[NOTE] One or more legs' P&L could not be exchange-confirmed via realized_pnl "
              "and used a last-known-mark-price estimate instead. Cross-check against your "
              "Delta Exchange fills/positions history for exact figures.")
    print("=" * 70)


# ======================================================================
# MONITORING LOOP
# ======================================================================

def monitor_and_check_exit(state):
    if is_settlement_time_reached(state):
        print("[INFO] Settlement time (17:30 IST) reached on expiry date. Assuming exchange has "
              "auto-settled the contract(s). No close orders will be placed.")
        print_final_trade_summary(state, natural_settlement=True)

        next_window = get_next_entry_window_start(now_ist(), state.get("last_entry_date"))
        print(f"[INFO] Trade cycle complete. Waiting for next entry window starting: "
              f"{next_window.strftime('%Y-%m-%d %H:%M IST')} "
              f"(in {format_timedelta(next_window - now_ist())})")

        fresh = default_state()
        fresh["last_entry_date"] = state.get("last_entry_date")
        save_state(fresh)
        return fresh

    spot_price = get_underlying_spot_price()
    if spot_price is None:
        print("[WARN] Could not fetch underlying spot price this cycle. Will retry next cycle.")
        return state

    call_mark = get_mark_price(state["call_product_id"], state["call_symbol"])
    put_mark = get_mark_price(state["put_product_id"], state["put_symbol"])
    if call_mark is not None:
        state["call_last_mark"] = call_mark
    if put_mark is not None:
        state["put_last_mark"] = put_mark

    if state.get("call_hedge_triggered") and state.get("call_hedge_product_id"):
        hedge_mark = get_mark_price(state["call_hedge_product_id"], state["call_hedge_symbol"])
        if hedge_mark is not None:
            state["call_hedge_last_mark"] = hedge_mark

    if state.get("put_hedge_triggered") and state.get("put_hedge_product_id"):
        hedge_mark = get_mark_price(state["put_hedge_product_id"], state["put_hedge_symbol"])
        if hedge_mark is not None:
            state["put_hedge_last_mark"] = hedge_mark

    save_state(state)

    state = check_and_trigger_hedge(state, spot_price)
    if not state.get("active"):
        return state  # force-closed during hedge check above

    time_to_expiry = get_time_to_expiry_str(state["expiry_date_iso"])
    upside = state.get("upside_breakeven")
    downside = state.get("downside_breakeven")

    print(f"[MONITOR] Spot: {spot_price} | Upside BE: "
          f"{'%.4f' % upside if upside is not None else 'N/A'} "
          f"({'BREACHED-HEDGED' if state.get('call_hedge_triggered') else 'watching'}) | "
          f"Downside BE: {'%.4f' % downside if downside is not None else 'N/A'} "
          f"({'BREACHED-HEDGED' if state.get('put_hedge_triggered') else 'watching'}) | "
          f"Time to Expiry: {time_to_expiry} | Status: HOLDING TO NATURAL EXPIRY")

    return state


def is_within_entry_window(now):
    sh, sm = parse_hhmm(ENTRY_WINDOW_START)
    eh, em = parse_hhmm(ENTRY_WINDOW_END)
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= now <= end


def get_next_entry_window_start(now, last_entry_date):
    sh, sm = parse_hhmm(ENTRY_WINDOW_START)
    candidate = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    today_iso = now.date().isoformat()

    if last_entry_date == today_iso or candidate <= now:
        candidate += timedelta(days=1)

    return candidate


# ======================================================================
# MAIN LOOP
# ======================================================================

def main():
    print_ip_banner()

    state = load_state()
    print("[INFO] Strategy started. Monitoring...")
    print(f"[INFO] Base URL: {BASE_URL}")
    print(f"[INFO] Underlying Asset: {UNDERLYING_ASSET}")
    print(f"[INFO] Call Delta: {CALL_TARGET_DELTA} | Put Delta: {PUT_TARGET_DELTA} | "
          f"Expiry N Days: {EXPIRY_N_DAYS} | Max Expiry Search Days: {MAX_EXPIRY_SEARCH_DAYS} | "
          f"Call Lots: {CALL_LOTS} | Put Lots: {PUT_LOTS}")
    print(f"[INFO] Entry Window: {ENTRY_WINDOW_START} - {ENTRY_WINDOW_END} IST | "
          f"Settlement Assumption: {SETTLEMENT_TIME_IST} IST")
    print("[INFO] No SL / Trailing SL / Target in this version. Breakeven hedge (one-time per "
          "side) is the only risk-adjustment mechanism. Positions run to natural expiry.")

    last_waiting_log_ts = 0

    while True:
        try:
            now = now_ist()

            if state.get("active"):
                state = monitor_and_check_exit(state)
            else:
                today_iso = now.date().isoformat()
                if is_within_entry_window(now) and state.get("last_entry_date") != today_iso:
                    state = attempt_entry(state)
                else:
                    current_ts = time.time()
                    if current_ts - last_waiting_log_ts >= WAITING_LOG_INTERVAL_SECONDS:
                        next_window = get_next_entry_window_start(now, state.get("last_entry_date"))
                        print(f"[WAITING] Current Time: {now.strftime('%Y-%m-%d %H:%M:%S')} IST | "
                              f"Next Entry Window: {next_window.strftime('%Y-%m-%d %H:%M')} IST | "
                              f"Time Left: {format_timedelta(next_window - now)}")
                        last_waiting_log_ts = current_ts

        except Exception as e:
            print(f"[ERROR] Unexpected exception in main loop: {e}")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
