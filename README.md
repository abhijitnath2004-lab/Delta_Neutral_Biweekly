# Bi-Weekly Delta Neutral NIFTY Strategy (Upstox)

A state-aware bot that runs a bi-weekly delta-neutral iron-condor on NIFTY weekly
options via the Upstox API.

## Strategy summary

| | |
|---|---|
| **Underlying** | NIFTY 50 |
| **Lot size** | 65 |
| **Expiry** | Tuesday (next-to-next week) |
| **Entry day / time** | Wednesday 15:00 IST |
| **Entry guard** | INDIA VIX < 14 |
| **Position** | Sell 0.20 delta CE & PE (premium-matched); buy hedges 200 pts away |
| **Strike grid** | 100-spaced only (skip illiquid 50-strikes) |
| **Min credit** | >= 3.5% of capital deployed |
| **Target** | +1% of deployed capital → MARKET square-off |
| **Stop loss** | -1% of deployed capital → **HARD** MARKET square-off |
| **Time exit** | Friday 15:00 (or `max_holding_sessions`) → MARKET square-off |

> **Hard SL.** The 1% stop-loss is treated as a *hard* kill switch. If breached, **all** legs (both shorts and both hedges) are squared off **immediately with MARKET orders**. No adjustment, no roll, no second-guess.

### Entry strike selection
1. Pick the 0.20Δ CE and PE strikes — restricted to **100-spaced** strikes only.
2. **Premium balance:** anchor to the side with the lower premium. Walk the richer side further OTM (still on 100-strikes, with a `min_delta_floor` of 0.10) until premiums are roughly matched. Example: PE 0.20Δ ₹40, CE 0.20Δ ₹55 → CE is shifted further OTM until its premium is also near ₹40.
3. Hedges 200 points beyond the (possibly-shifted) shorts, snapped to the nearest 100-strike.
4. Net credit must be ≥ 3.5% of `(spread_width_CE + spread_width_PE) × qty` else entry is skipped.

### Order placement
| Action | Order type | Why |
|---|---|---|
| Open / re-open a leg | **LIMIT** at bid-ask mid, retried | Bid-ask is wide on the next-to-next-week expiry; market orders would slip badly. |
| Close a leg (target / SL / time exit) | **MARKET** | Execution certainty matters more than price. |
| Adjustment close (close challenged short + its hedge) | **MARKET** | Same reason. |
| Adjustment re-open (new short + new hedge) | **LIMIT** at bid-ask mid, retried | Same reason as fresh entry. |

The LIMIT helper:
1. Reads top-of-book bid/ask via `/v2/market-quote/quotes`.
2. Places a LIMIT at `(bid + ask) / 2`, rounded to the NSE 0.05 tick.
3. Polls `/v2/order/details` for up to `limit_wait_seconds` (default 60s).
4. If unfilled, cancels via `/v3/order/cancel`, refreshes bid/ask, re-prices, and re-places.
5. After `limit_max_attempts` unfilled tries (default 5), falls back to a MARKET order so execution is guaranteed.

### Adjustments (continuously checked when no exit gate fires)
1. **Premium-double / delta imbalance** — if one short delta exceeds 2× the other, MARKET-close that side + its hedge and LIMIT-redeploy at 0.20Δ + 200pt hedge.
2. **Delta cap 0.35** — same flow.
3. **Premium decay ≥ 50%** — same flow.

## Scheduling

The bot runs in a candle-aligned loop:

- Wakes up at every 5-minute boundary (`:20, :25, :30, …, :15:00, …, :25:00`) plus a `tick_buffer_seconds` of **7 seconds** so Upstox feeds have time to populate the freshly-closed candle.
- **Skips the 9:15 and 15:30 candles** by enforcing a monitor window of `09:20`–`15:25` IST.
- Outside the window or on weekends/holidays it sleeps until the next trading day's `09:20:07`.
- Entry can only happen on Wednesday between `15:00` and `15:25`.

## Upstox API versions

| Operation | Version | Endpoint |
|---|---|---|
| Option chain (with greeks) | v2 | `/v2/option/chain` |
| Market quote / LTP / depth | v2 | `/v2/market-quote/...` |
| **Place order** | **v3** | `/v3/order/place` |
| **Cancel order** | **v3** | `/v3/order/cancel` |
| Order details / average price | v2 | `/v2/order/details` |
| Positions | v2 | `/v2/portfolio/short-term-positions` |

After every order placement the bot polls `/v2/order/details` until the order is `complete` and uses the returned `average_price` as the leg's true entry / exit price for P&L.

## File layout

```
config.json              # All tunables (strikes, lot, SL, target, deltas, hedge, limit-order, etc.)
main.py                  # Runner / scheduler (candle-aligned 5-min ticks)
strategy.py              # Entry, exit, adjustment logic + strike selection
upstox_client.py         # Mixed v2/v3 wrapper, limit-with-retry, market-and-fill
state_manager.py         # JSON state persistence + archive on close
utils/
  time_utils.py          # IST clock, Tuesday-expiry math, candle alignment
  logger.py
state/
  active_trade.json      # Live trade. Renamed on close.
  closed_trades/         # Archive
```

## Setup & run

```bash
pip install -r requirements.txt
```

### Token (Alma Linux on Oracle Cloud)
The bot reads the Upstox access token from a **file** that your daily token-generator writes before market open. The default path is:

```
/home/opc/TOKEN/upstox_token.txt
```

Override via `config.upstox.access_token_file` if you store it elsewhere. The file is read on startup, on every API request (cheap stat call), and again automatically whenever the file's mtime changes — so when the morning generator overwrites the file, the bot picks up the new token without restarting.

If Upstox returns a `401 Unauthorized`, the bot also force-reloads the token from disk and retries the request once.

If the file is missing or empty, the client falls back to the env var named in `config.upstox.access_token_env` (default `UPSTOX_ACCESS_TOKEN`), so you can still run the bot on a dev machine with `export UPSTOX_ACCESS_TOKEN=...`.

```bash
# Production (Alma Linux): your generator writes /home/opc/TOKEN/upstox_token.txt
python main.py            # live loop (5-min candle-aligned ticks)

# Dev / one-off
export UPSTOX_ACCESS_TOKEN=<your_token>
python main.py --once     # single tick
python main.py --status   # print the active trade JSON
```

### Suggested systemd unit (Alma Linux)
```ini
# /etc/systemd/system/delta-neutral-bot.service
[Unit]
Description=Bi-Weekly Delta Neutral NIFTY Bot
After=network-online.target

[Service]
Type=simple
User=opc
WorkingDirectory=/home/opc/Delta_Neutral_Biweekly
ExecStart=/usr/bin/python3 main.py
Restart=on-failure
RestartSec=30
StandardOutput=append:/home/opc/Delta_Neutral_Biweekly/logs/stdout.log
StandardError=append:/home/opc/Delta_Neutral_Biweekly/logs/stderr.log

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now delta-neutral-bot
sudo systemctl status delta-neutral-bot
journalctl -u delta-neutral-bot -f
```

## State awareness

- On entry, the bot writes `state/active_trade.json` with full leg details (strike, instrument key, fill price, fill delta, order id) and the entry baselines (`target_pnl`, `stop_loss_pnl`, `capital_deployed`, `entry_credit_per_unit_filled`).
- Every monitor tick refreshes prices/deltas, recomputes P&L, evaluates exits then adjustments, and persists the snapshot back to the same file.
- On close the file is renamed to `state/closed_trades/<trade_id>_<reason>_<timestamp>.json`.
- If you restart the bot mid-trade, it loads the active state and resumes monitoring -- no re-entry while a trade is open.

## Order isolation (your manual trades are safe)

The bot is engineered to coexist with manual trades you have running on the same Upstox account. Two layers protect that:

### 1. Per-trade order tag
Every order the bot places carries `tag = "<short trade id>"` (e.g. `DN_260520_1500_a1b`, ≤20 chars to fit Upstox's tag field). The trade tag is generated at entry, persisted as `state.tag`, and reused for every redeploy/close belonging to that trade. You can filter the Upstox order book by this tag to see only the bot's orders for a given trade and tell them apart from your manual ones.

### 2. Closes only the bot's tracked legs -- never a "close all" sweep
`_close_all` iterates only `state["legs"].values()` -- the **four specific instrument keys** the bot has recorded for the current trade. It does not call `get_positions()` to find and close everything. Any manual trade you have on different strikes, expiries, or symbols is invisible to this loop and is **not** touched.

For the unusual case where you've manually altered one of those four exact instruments, a defensive **position-existence check** runs before each close (toggle: `verify_position_before_close`):

| Broker shows for that instrument | Bot decision |
|---|---|
| `≤ -65` (we shorted 65; broker shows our short, possibly plus your extra short) | **Closes 65** -- your extra position untouched |
| `-65` exactly | **Closes 65** -- normal happy path |
| `-30` (you manually closed part of the bot's leg) | **Skips**, marks the leg `SKIPPED_POSITION_NOT_FOUND`, logs an ERROR -- never blindly opens a new long |
| `0` or position absent (you manually closed the whole leg) | **Skips**, same as above |
| Positions API unreachable | Fails open: closes the leg using state's qty (logged as a warning) |

If the check skips a close, the leg is annotated in state with `close_status: "SKIPPED_POSITION_NOT_FOUND"` and a timestamp, the trade is still archived to `state/closed_trades/`, and you reconcile manually.

### What's recorded per leg in state JSON

```jsonc
{
  "instrument_key":  "NSE_FO|...",
  "tradingsymbol":   "NIFTY...CE",
  "strike":          24800,
  "option_type":     "CE",
  "side":            "SELL",
  "qty":             65,         // actual filled
  "intended_qty":    65,         // what we asked for
  "qty_shortfall":   0,
  "entry_price":     42.55,      // weighted-avg fill from /v2/order/details
  "entry_delta":     0.20,
  "current_price":   38.10,
  "current_delta":   0.18,
  "entry_order_id":  "230516010305011",
  // post-close:
  "exit_order_id":   "230516010305112",
  "exit_filled_qty": 65,
  "exit_price":      18.30,
  "close_status":    "CLOSED"    // or "PARTIALLY_CLOSED" / "SKIPPED_POSITION_NOT_FOUND" / "ERROR"
}
```

## Tunables of note

| Key | Default | What it controls |
|---|---|---|
| `strike_step` | 100 | Strike grid (100 = skip 50-strikes) |
| `min_delta_floor` | 0.10 | When premium-balancing, never pick a strike below this |Δ| |
| `premium_balance_tolerance_pct` | 5.0 | Skip premium-rebalancing if CE/PE LTPs are already this close |
| `limit_max_attempts` | 5 | LIMIT-with-retry attempts before falling back to MARKET |
| `limit_wait_seconds` | 60 | Wait per attempt before cancel & re-price |
| `limit_fallback_to_market` | true | If false, the helper gives up after `limit_max_attempts` |
| `tick_buffer_seconds` | 7 | Delay after each candle close before reading data |
| `monitor_window_start/end` | 09:20 / 15:25 | The reliable monitor window |

## Caveats

- **Time-exit semantics.** With a Wed entry and next-to-next-week Tue expiry, "Friday before expiry" is 8 trading sessions away (Wed,Thu,Fri,Mon,Tue,Wed,Thu,Fri). The bot honors **Friday-before-expiry 15:00** as the primary gate and uses `max_holding_sessions` (default 8) as a safety cap. Set it to `6` if you want a shorter cap.
- `capital_deployed` is approximated as `(spread_width_CE + spread_width_PE) × qty`. Replace with Upstox's margin API for broker-exact sizing.
- Holiday list lives in `utils/time_utils.py` (`NSE_HOLIDAYS_2026`). Update yearly.
- The bot relies on Upstox option-chain `option_greeks.delta`. If your access tier omits it, plug a black-scholes fallback into `strategy._normalize_row`.
- The Upstox `cancel_order` HTTP path is set to `DELETE /v3/order/cancel?order_id=...`. If your tier exposes a different shape (e.g. `/v3/order/cancel/{id}`), tweak `endpoints.cancel_order` in `config.json`.
