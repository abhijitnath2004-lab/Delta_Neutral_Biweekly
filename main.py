"""Bi-weekly delta-neutral strategy runner.

Usage:
    export UPSTOX_ACCESS_TOKEN=eyJh...
    python main.py                  # live loop
    python main.py --once           # single tick, useful for cron
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


def cmd_run(cfg, log, once: bool):
    client = UpstoxClient(cfg, log)
    sm = StateManager(cfg, log)
    strat = DeltaNeutralStrategy(cfg, client, sm, log)

    if once:
        strat.run_once()
        return

    interval = int(cfg.get("monitor_interval_seconds", 60))
    log.info("Bot started. Polling every %ds. Ctrl+C to stop.", interval)
    while True:
        try:
            strat.run_once()
        except KeyboardInterrupt:
            log.info("Bot interrupted by user. State preserved.")
            break
        except Exception as e:
            log.error("Tick failed: %s\n%s", e, traceback.format_exc())
        time.sleep(interval)


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
