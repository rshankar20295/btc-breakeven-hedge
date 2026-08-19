# BTC with Breakeven Hedge

Delta Exchange automated options strategy: sells 1 BTC Call + 1 BTC Put
(short strangle) at a configurable delta. No fixed SL/Target/Trailing SL —
positions run to natural expiry. If the underlying price breaches the
time-value-based breakeven on either side, the bot buys a one-time
additional hedge option (ATM strike) on that side only, without closing
the original short leg. If a hedge order fails, the entire position is
force-closed immediately as a safety measure.

## Setup

1. Copy `.env.example` to `.env` and fill in your Delta Exchange API
   key/secret and desired configuration.
2. Install dependencies:
   pip install -r requirements.txt
3. Run:
   python main.py

## Important

- Test on TESTNET first (`DELTA_BASE_URL` pointed to testnet).
- This is a naked short options strategy with no stop loss. Read all
  warnings in main.py before running on a live account.
- Never commit `.env` or `state.json` to version control.
