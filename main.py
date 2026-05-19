"""Bi-weekly delta-neutral strategy runner.

Usage:
    export UPSTOX_ACCESS_TOKEN=eyJh...
    python main.py                  # live loop, candle-aligned 5-min ticks
    python main.py --once           # single tick
    python main.py --status         # print active trade summary

Designed to be safely killed and restarted -- on restart, if state/active_trade.json
exists, the bot resumes monitoring. When the trade is closed the file is renamed
into state/closed_trades/<trade_id>_<reason>_<ts>.json automatically.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback

from state_manager import StateManager
from strategy import DeltaNeutralStrategy
from upstox_client import UpstoxClient
from utils.logger import get_logger
from utils import time_utils as tu


def load_config(path: str = "config.json") -> dict:
    with open(path, "r") as f:
        return json.load(f)


def cmd_status(cfg, log):
    sm = StateManager(cfg, log)
    state = sm.load()
    if not state:
        print("No active trade.")
        return
    print(json.dumps(state, indent=2, default=str))


def _sleep_until(target, log) -> None:
    """Sleep in chunks until `target` (timezone-aware datetime). Robust to clock
    skew and to the process being suspended (e.g. laptop sleep)."""
    while True:
        now = tu.now_ist()
        secs = (target - now).total_seconds()
        if secs <= 0:
            return
        # Cap chunk so a long sleep is interruptible and self-correcting
        time.sleep(min(secs, 30))


def cmd_run(cfg, log, once: bool):
    client = UpstoxClient(cfg, log)
    sm = StateManager(cfg, log)
    strat = DeltaNeutralStrategy(cfg, client, sm, log)

    if once:
        strat.run_once()
        return

    interval_min = int(cfg.get("monitor_interval_minutes", 5))
    win_start = tu.parse_hhmm(cfg.get("monitor_window_start", "09:20"))
    win_end   = tu.parse_hhmm(cfg.get("monitor_window_end",   "15:25"))
    buffer_s  = int(cfg.get("tick_buffer_seconds", 3))

    log.info(
        "Bot started. Candle-aligned ticks every %d min, window %s-%s IST (skips 9:15 & 15:30).",
        interval_min,
        cfg.get("monitor_window_start", "09:20"),
        cfg.get("monitor_window_end",   "15:25"),
    )

    while True:
        try:
            now = tu.now_ist()
            # If we're already inside the window AND aligned within ~5s of a
            # candle close, run immediately.
            if tu.is_in_monitor_window(now) and (now.minute % interval_min == 0) and now.second <= 10:
                strat.run_once()
            # Schedule next aligned wake-up
            nxt = tu.next_candle_tick(now, interval_min, win_start, win_end, buffer_s)
            log.info("Next tick at %s IST", nxt.strftime("%Y-%m-%d %H:%M:%S"))
            _sleep_until(nxt, log)
            strat.run_once()
        except KeyboardInterrupt:
            log.info("Bot interrupted by user. State preserved.")
            break
        except Exception as e:
            log.error("Tick failed: %s\n%s", e, traceback.format_exc())
            # Avoid hot-looping on persistent errors
            time.sleep(15)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--once", action="store_true", help="Run a single tick and exit")
    ap.add_argument("--status", action="store_true", help="Print active trade and exit")
    args = ap.parse_args()

    cfg = load_config(args.config)
    log = get_logger("delta_neutral", cfg.get("log_file", "logs/strategy.log"))
    log.info("Now (IST): %s", tu.now_ist().isoformat())

    if args.status:
        cmd_status(cfg, log)
        return
    cmd_run(cfg, log, once=args.once)


if __name__ == "__main__":
    sys.exit(main() or 0)
