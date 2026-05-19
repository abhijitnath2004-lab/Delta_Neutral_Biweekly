# Bi-Weekly Delta Neutral NIFTY Strategy (Upstox)

A state-aware bot that runs a bi-weekly delta-neutral iron-condor on NIFTY weekly
options via the Upstox v2 API.

## Strategy summary

| | |
|---|---|
| **Underlying** | NIFTY 50 |
| **Lot size** | 65 |
| **Expiry** | Tuesday (next-to-next week) |
| **Entry day / time** | Wednesday 15:00 IST |
| **Entry guard** | INDIA VIX < 14 |
| **Position** | Sell 0.20 delta CE & PE; buy hedges 200 pts away |
| **Min credit** | >= 3.5% of capital deployed |
| **Target / SL** | +1% / -1% of deployed capital |
| **Time exit** | Friday 15:00 (or 6 trading sessions, whichever first) |

### Adjustments
1. **Premium-double / delta imbalance** -- if one short delta exceeds 2x the other, book that side + its hedge and redeploy at 0.20 delta + 200pt hedge.
2. **Delta cap 0.35** -- if any short delta crosses 0.35, square off that side + hedge and redeploy at 0.20 delta + 200pt hedge.
3. **Premium decay >= 50%** -- if a short premium has decayed by half or more, roll the short + hedge to a fresh 0.20 delta + 200pt hedge.

## File layout

```
config.json              # Strikes, lot, SL, target, deltas, hedge distance, VIX gate, etc.
main.py                  # Runner / scheduler
strategy.py              # Entry, exit, adjustment logic
upstox_client.py         # Upstox v2 API wrapper
state_manager.py         # JSON state persistence + archive on close
utils/
  time_utils.py          # IST clock, Tuesday-expiry math, trading sessions
  logger.py
state/
  active_trade.json      # Live trade. Renamed on close.
  closed_trades/         # Archive
```

## Setup

```bash
pip install -r requirements.txt
export UPSTOX_ACCESS_TOKEN=<your_token>
```

Edit `config.json` to set your `capital`, `num_lots`, etc.

## Run

```bash
python main.py            # live loop
python main.py --once     # single tick (use from cron)
python main.py --status   # show active trade
```

## State awareness

- On entry, the bot writes `state/active_trade.json` with full leg details and entry baselines.
- Every monitor tick refreshes prices/deltas, recomputes P&L, evaluates exits and adjustments, and persists the snapshot back to the same file.
- On close (target / SL / time exit) the file is renamed to
  `state/closed_trades/<trade_id>_<reason>_<timestamp>.json`.
- If you restart the bot mid-trade, it picks up the state file automatically and resumes monitoring -- no re-entry will happen while a trade is open.

## Notes / caveats

- **Time-exit semantics.** The user spec says *"Friday 15:00 when expiry is 2 days away"* and also *"max 6 trading sessions"*. With a Wed entry and next-to-next-week Tue expiry, "Friday before expiry" is 8 trading sessions away (Wed,Thu,Fri,Mon,Tue,Wed,Thu,Fri). The bot honors the **Friday-before-expiry 15:00** gate as the primary rule and uses `max_holding_sessions` (default 8) as a safety cap. If you'd rather cap at 6 sessions, set `"max_holding_sessions": 6` in `config.json`.
- `capital_deployed` in this implementation is approximated as `(spread_width_CE + spread_width_PE) x qty`. Replace with the actual margin returned by Upstox margin API if you need broker-exact sizing.
- Holiday list lives in `utils/time_utils.py` (`NSE_HOLIDAYS_2026`). Update yearly.
- All orders are placed as `MARKET` `NRML` by default -- change in `config.json` if needed.
- The bot relies on Upstox option-chain greeks. If your access tier returns no `option_greeks.delta`, plug in your own black-scholes computation in `strategy._normalize_row`.
