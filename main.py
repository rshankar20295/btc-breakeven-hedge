"""
Delta Exchange - BTC Options Short Strangle Strategy (v3.1 - Breakeven Hedge Edition)
========================================================================================
This is a NEW, SEPARATE version. It does NOT modify or replace main.py (v1) or the
v2 SL/Target/Trailing-SL script - deploy this as its own repo/service.

Sells 1 Call + 1 Put on BTC options, strike selected by nearest-match delta (any
delta, including deep ITM values like 0.7 / -0.7). Entry within a user-defined
IST time window. NO stop loss, trailing stop loss, or target exit exists in this
version - positions are intended to run to natural expiry/settlement on the
exchange.

WHAT CHANGED IN v3.1 (bug fixes on top of v3):
  1. TRADING STATUS PRE-CHECK: Before placing ANY order, both the Call and Put
     legs' trading_status is checked. If either is not "operational" (e.g.
     market_disrupted_cancel_only_mode), the entry attempt is skipped entirely -
     no orders are placed, no rollback needed. This fixes a real incident where
     the bot kept placing the Call leg, having the Put leg rejected due to a
     disrupted market, rolling back, and immediately retrying every poll cycle.
  2. ENTRY RETRY CAP + COOLDOWN: Failed entry attempts are now capped per day
     (MAX_ENTRY_ATTEMPTS_PER_WINDOW) with a cooldown between attempts
     (ENTRY_RETRY_COOLDOWN_SECONDS), instead of retrying every single poll
     cycle (~every 10 seconds) for the entire entry window.
  3. LOT SIZE VALIDATION: Bot now refuses to start if CALL_LOTS != PUT_LOTS,
     since mismatched quantities would silently corrupt the breakeven
     calculation (which assumes equal quantities on both legs).
  4. ZERO/NEGATIVE TIME VALUE WARNING: If computed time value is <= 0 at entry
     (common for deep ITM strikes near expiry), breakeven levels cannot be
     computed and hedge protection is effectively disabled for that trade.
     The bot now prints a LOUD, REPEATING warning every monitoring cycle in
     this case, instead of silently running unprotected.
  5. ORPHAN POSITION STARTUP CHECK: On startup, if local state says no trade
     is active, but the exchange reports open BTC option positions anyway
     (e.g. due to a crash/restart between leg fills), the bot prints a loud
     warning listing the orphaned position(s) and REFUSES to start a new
     entry cycle until resolved manually. This prevents accidental duplicate
     exposure.

EVERYTHING ELSE FROM v3 UNCHANGED:
  - Breakeven computed from TIME VALUE (real max profit), not raw premium.
  - One-time-only hedge per side (Call hedge on upside breach, Put hedge on
    downside breach), using ATM strike (or next strike closer to spot if ATM
    collides with the original sold strike).
  - Original short legs are NEVER closed by the hedge logic.
  - Hedge order failure => immediate full-position force-close.
  - No SL / Trailing SL / Target / time-based force-exit. Settlement is
    detected via a fixed daily time check (17:30 IST), not bot-initiated.
  - Forward-fallback expiry search (up to MAX_EXPIRY_SEARCH_DAYS).

IMPORTANT WARNINGS (READ CAREFULLY):
  - This is a NAKED SHORT OPTIONS strategy. The hedge reduces further loss
    RATE on a breached side but does not cap losses to zero, and does not
    protect against gap moves between polling cycles or exchange-side
    liquidation triggered independently of your bot.
  - Deep ITM strikes (e.g. delta 0.7) are more prone to thin liquidity /
    disrupted market states, especially on testnet, as observed in real
    usage. Even with the fixes above, a deep ITM entry may simply fail to
    find a tradable market more often than OTM/ATM strikes.
  - Test on TESTNET first (set DELTA_BASE_URL env var to testnet URL).
  - All configuration is via environment variables (set in Railway dashboard).
  - Railway's default networking may assign a DIFFERENT outbound IP on every
    redeploy/restart unless a static IP feature is enabled on your plan.
    Re-check the IP banner after every redeploy and update Delta's whitelist.
  - Settlement detection relies on a FIXED time assumption (17:30 IST) as
    explicitly confirmed by the user, not a documented exchange guarantee.
    Cross-check final P&L against your actual Delta Exchange fills/positions
    history, especially during initial testing.
"""

import hashlib
import hmac
import json
import os
import sys
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

# NEW (v3.1): Enforce matching quantities - required for breakeven math to
# be valid, and explicitly confirmed as a strategy requirement.
if CALL_LOTS != PUT_LOTS:
    raise EnvironmentError(
        f"CALL_LOTS ({CALL_LOTS}) and PUT_LOTS ({PUT_LOTS}) must be EQUAL. "
        f"Mismatched quantities will silently corrupt breakeven calculations. "
        f"Fix your environment variables before restarting."
    )

# Entry window: 7:00 PM - 7:15 PM IST (confirmed)
ENTRY_WINDOW_START = os.environ.get("ENTRY_WINDOW_START", "19:00")
ENTRY_WINDOW_END = os.environ.get("ENTRY_WINDOW_END", "19:15")

# Fixed daily settlement time assumption for options - NOT configurable,
# hardcoded per confirmed requirement (5:30 PM IST).
SETTLEMENT_TIME_IST = "17:30"

# NEW (v3.1): Entry retry controls
MAX_ENTRY_ATTEMPTS_PER_WINDOW = int(os.environ.get("MAX_ENTRY_ATTEMPTS_PER_WINDOW", "5"))
ENTRY_RETRY_COOLDOWN_SECONDS = int(os.environ.get("ENTRY_RETRY_COOLDOWN_SECONDS", "60"))

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
    """Display-only approximate expiry moment for 'Time to Expiry' logs."""
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
        "User-Agent": "btc-strangle-breakeven-hedge-bot-v3.1",
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

        # NEW (v3.1): entry retry tracking
        "entry_attempts_today": 0,
        "entry_attempts_date": None,
        "last_entry_attempt_ts": None,
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
        return remaining[0]

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


def get_product_trading_status(symbol):
    """
    NEW (v3.1): Fetches current trading_status for a product (operational,
    disrupted_cancel_only, or disrupted_post_only). Used to pre-check
    market health BEFORE attempting to place an order.
    """
    path = f"/v2/products/{symbol}"
    resp = send_request("GET", path)
    if not resp or not resp.get("success"):
        print(f"[WARN] Could not fetch trading status for {symbol}: {resp}")
        return None
    result = resp.get("result") or {}
    return result.get("trading_status")


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


def get_all_open_positions_for_underlying(underlying_asset_symbol):
    """
    NEW (v3.1): Used for the orphan-position startup check. Fetches ALL
    open positions for the given underlying asset (e.g. 'BTC') in one call.
    """
    path = "/v2/positions"
    params = {"underlying_asset_symbol": underlying_asset_symbol}
    resp = send_request("GET", path, query_params=params)
    if not resp or not resp.get("success"):
        print(f"[WARN] Could not fetch open positions for {underlying_asset_symbol}: {resp}")
        return []
    result = resp.get("result")
    if result is None:
        return []
    if isinstance(result, list):
        return result
    return [result]


def get_margin_and_pnl_for_products(product_ids):
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
# STARTUP SAFETY CHECK (NEW in v3.1)
# ======================================================================

def check_for_orphaned_positions(state):
    """
    NEW (v3.1): On startup, if local state says no trade is active, but the
    exchange reports open option positions for the underlying asset anyway
    (e.g. due to a crash/restart between leg fills during a prior entry
    attempt), print a loud warning and BLOCK new entries until resolved.

    Returns True if it is safe to proceed, False if the bot should halt
    entry attempts (monitoring/logging still continues) until manually
    reviewed.
    """
    if state.get("active"):
        return True  # bot already knows about an active trade, nothing to check here

    positions = get_all_open_positions_for_underlying(UNDERLYING_ASSET)
    open_option_positions = [
        p for p in positions
        if p.get("size") and int(p.get("size", 0)) != 0
    ]

    if open_option_positions:
        print("#" * 70)
        print("#  [CRITICAL WARNING] ORPHANED POSITION(S) DETECTED ON STARTUP")
        print("#  Local state shows NO active trade, but the exchange reports")
        print("#  the following OPEN position(s) for this underlying asset:")
        for p in open_option_positions:
            print(f"#    - Symbol: {p.get('product_symbol')} | Size: {p.get('size')} | "
                  f"Entry Price: {p.get('entry_price')}")
        print("#  This likely happened due to a crash/restart during a previous")
        print("#  entry attempt, between leg fills. The bot will NOT start any")
        print("#  new entry cycle until this is resolved.")
        print("#  ACTION REQUIRED: Manually review and close/manage these")
        print("#  position(s) on Delta Exchange, then restart the bot.")
        print("#" * 70)
        return False

    return True


# ======================================================================
# BREAKEVEN CALCULATION
# ======================================================================

def calculate_breakeven_levels(call_strike, put_strike, total_time_value,
                                call_contract_value, call_size):
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

    # --- NEW (v3.1): Trading status pre-check BEFORE placing any order ---
    call_status = get_product_trading_status(call_symbol)
    put_status = get_product_trading_status(put_symbol)

    if call_status != "operational" or put_status != "operational":
        print(f"[WARN] Market not operational for entry. "
              f"CALL ({call_symbol}) status: {call_status} | "
              f"PUT ({put_symbol}) status: {put_status}. "
              f"Skipping this attempt - no orders will be placed.")
        return state

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

    upside_breakeven, downside_breakeven = calculate_breakeven_levels(
        call_strike_f, put_strike_f, total_time_value, call_contract_value, call_size
    )

    # --- NEW (v3.1): Loud warning if hedge protection will be unavailable ---
    if total_time_value <= 0 or upside_breakeven is None or downside_breakeven is None:
        print("#" * 70)
        print("#  [CRITICAL WARNING] TIME VALUE IS ZERO/NEGATIVE OR BREAKEVEN")
        print("#  COULD NOT BE COMPUTED. HEDGE PROTECTION WILL BE UNAVAILABLE")
        print("#  FOR THIS ENTIRE TRADE. This commonly happens with deep ITM")
        print("#  strikes near expiry. The position will run FULLY NAKED AND")
        print("#  UNPROTECTED until natural expiry. Proceeding per configured")
        print("#  delta settings, but consider reviewing your delta targets.")
        print("#" * 70)

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
          f"{'%.4f' % upside_breakeven if upside_breakeven is not None else 'N/A - HEDGE DISABLED'}")
    print(f"DOWNSIDE BREAKEVEN (Put hedge triggers below this): "
          f"{'%.4f' % downside_breakeven if downside_breakeven is not None else 'N/A - HEDGE DISABLED'}")
    print(f"MARGIN UTILIZED AT ENTRY: ${margin_utilized_at_entry:.4f}")
    print("=" * 70)

    return state


# ======================================================================
# STRATEGY LOGIC - HEDGE TRIGGER
# ======================================================================

def execute_hedge_leg(state, side_label, original_strike, contract_type, lots, spot_price):
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

    # Trading status check before hedge order too
    hedge_status = get_product_trading_status(hedge_symbol)
    if hedge_status != "operational":
        print(f"[ERROR] Hedge strike {hedge_symbol} is not operational (status: {hedge_status}). "
              f"Cannot place hedge order.")
        return False, state

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
    fresh["entry_attempts_today"] = state.get("entry_attempts_today", 0)
    fresh["entry_attempts_date"] = state.get("entry_attempts_date")
    fresh["last_entry_attempt_ts"] = state.get("last_entry_attempt_ts")
    save_state(fresh)
    return fresh


def check_and_trigger_hedge(state, spot_price):
    upside = state.get("upside_breakeven")
    downside = state.get("downside_breakeven")

    if (not state.get("call_hedge_triggered")) and upside is not None and spot_price > upside:
        success, state = execute_hedge_leg(
            state, "CALL", state.get("call_strike"),
            "call_options", abs(state.get("call_lots") or CALL_LOTS), spot_price
        )
        if not success:
            return force_close_full_position(
                state, "HEDGE_ORDER_FAILED_CALL_SIDE_INSUFFICIENT_BALANCE_OR_ERROR"
            )

    if not state.get("active"):
        return state

    if (not state.get("put_hedge_triggered")) and downside is not None and spot_price < downside:
        success, state = execute_hedge_leg(
            state, "PUT", state.get("put_strike"),
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
    settlement_h, settlement_m = parse_hhmm(SETTLEMENT_TIME_IST)
    now = now_ist()
    expiry_date_obj = datetime.fromisoformat(state["expiry_date_iso"]).date()

    if now.date() > expiry_date_obj:
        return True
    if now.date() == expiry_date_obj and (now.hour, now.minute) >= (settlement_h, settlement_m):
        return True
    return False


def print_final_trade_summary(state, natural_settlement=True):
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
    print(f"TOTAL COMBINED P&L: ${total_pnl:.4f}" +
          (" (partial, some legs unavailable)" if any_unavailable else ""))
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
        fresh["entry_attempts_today"] = state.get("entry_attempts_today", 0)
        fresh["entry_attempts_date"] = state.get("entry_attempts_date")
        fresh["last_entry_attempt_ts"] = state.get("last_entry_attempt_ts")
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

    # NEW (v3.1): Loud repeating warning if hedge protection is unavailable for this trade
    if state.get("upside_breakeven") is None or state.get("downside_breakeven") is None:
        print("[CRITICAL WARNING] This trade has NO hedge protection (breakeven could not be "
              "computed at entry - likely zero/negative time value). Position is running FULLY "
              "NAKED until natural expiry.")

    state = check_and_trigger_hedge(state, spot_price)
    if not state.get("active"):
        return state

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

    # NEW (v3.1): Orphan-position startup safety check
    safe_to_enter = check_for_orphaned_positions(state)

    print("[INFO] Strategy started. Monitoring...")
    print(f"[INFO] Base URL: {BASE_URL}")
    print(f"[INFO] Underlying Asset: {UNDERLYING_ASSET}")
    print(f"[INFO] Call Delta: {CALL_TARGET_DELTA} | Put Delta: {PUT_TARGET_DELTA} | "
          f"Expiry N Days: {EXPIRY_N_DAYS} | Max Expiry Search Days: {MAX_EXPIRY_SEARCH_DAYS} | "
          f"Call Lots: {CALL_LOTS} | Put Lots: {PUT_LOTS}")
    print(f"[INFO] Entry Window: {ENTRY_WINDOW_START} - {ENTRY_WINDOW_END} IST | "
          f"Settlement Assumption: {SETTLEMENT_TIME_IST} IST")
    print(f"[INFO] Max Entry Attempts/Day: {MAX_ENTRY_ATTEMPTS_PER_WINDOW} | "
          f"Retry Cooldown: {ENTRY_RETRY_COOLDOWN_SECONDS}s")
    print("[INFO] No SL / Trailing SL / Target in this version. Breakeven hedge (one-time per "
          "side) is the only risk-adjustment mechanism. Positions run to natural expiry.")

    if not safe_to_enter:
        print("[WARN] Bot will continue running (monitoring only) but WILL NOT attempt new "
              "entries until the orphaned position warning above is resolved and the bot is "
              "restarted.")

    last_waiting_log_ts = 0

    while True:
        try:
            now = now_ist()

            if state.get("active"):
                state = monitor_and_check_exit(state)
            else:
                today_iso = now.date().isoformat()

                if state.get("entry_attempts_date") != today_iso:
                    state["entry_attempts_today"] = 0
                    state["entry_attempts_date"] = today_iso
                    state["last_entry_attempt_ts"] = None
                    save_state(state)

                if (safe_to_enter and is_within_entry_window(now)
                        and state.get("last_entry_date") != today_iso):

                    if state["entry_attempts_today"] >= MAX_ENTRY_ATTEMPTS_PER_WINDOW:
                        current_ts = time.time()
                        if current_ts - last_waiting_log_ts >= WAITING_LOG_INTERVAL_SECONDS:
                            print(f"[WARN] Max entry attempts ({MAX_ENTRY_ATTEMPTS_PER_WINDOW}) "
                                  f"reached for today. Will not retry further in this window.")
                            last_waiting_log_ts = current_ts
                    else:
                        last_attempt_ts = state.get("last_entry_attempt_ts")
                        cooldown_elapsed = (
                            last_attempt_ts is None
                            or (time.time() - last_attempt_ts) >= ENTRY_RETRY_COOLDOWN_SECONDS
                        )
                        if cooldown_elapsed:
                            state["entry_attempts_today"] += 1
                            state["last_entry_attempt_ts"] = time.time()
                            save_state(state)
                            print(f"[INFO] Entry attempt {state['entry_attempts_today']}/"
                                  f"{MAX_ENTRY_ATTEMPTS_PER_WINDOW} for today.")
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
