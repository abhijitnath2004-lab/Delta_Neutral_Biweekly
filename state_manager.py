"""Persists the live trade state to JSON. On close, the active file is renamed
to an archive name so the bot starts fresh next run.

Schema (active_trade.json):
{
  "trade_id":            str,
  "entry_dt":            ISO8601,
  "expiry_date":         "YYYY-MM-DD",
  "spot_at_entry":       float,
  "vix_at_entry":        float,
  "capital_deployed":    float,
  "lots":                int,
  "lot_size":            int,
  "target_pnl":          float,
  "stop_loss_pnl":       float,
  "entry_credit":        float,        # net credit per-lot in INR (sum of premiums)
  "legs": {
      "ce_short": Leg, "ce_hedge": Leg,
      "pe_short": Leg, "pe_hedge": Leg
  },
  "status": "OPEN" | "CLOSED",
  "close_reason": str | null,
  "history": [ {ts, event, ...}, ... ]
}

Leg = {
  "instrument_key": str,
  "tradingsymbol":  str,
  "strike":         int,
  "option_type":    "CE" | "PE",
  "side":           "SELL" | "BUY",
  "qty":            int,
  "entry_price":    float,
  "entry_delta":    float
}
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from typing import Any, Dict, Optional


class StateManager:
    def __init__(self, cfg: Dict[str, Any], logger):
        self.cfg = cfg
        self.log = logger
        self.path = cfg["state_file"]
        self.archive_dir = cfg["closed_trades_dir"]
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        os.makedirs(self.archive_dir, exist_ok=True)

    # ---------- read / write ----------
    def has_active(self) -> bool:
        return os.path.exists(self.path)

    def load(self) -> Optional[Dict[str, Any]]:
        if not self.has_active():
            return None
        with open(self.path, "r") as f:
            return json.load(f)

    def save(self, state: Dict[str, Any]) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp, self.path)

    # ---------- helpers ----------
    def append_history(self, state: Dict[str, Any], event: str, **kwargs) -> None:
        state.setdefault("history", []).append(
            {"ts": datetime.utcnow().isoformat() + "Z", "event": event, **kwargs}
        )
        self.save(state)

    def close_trade(self, state: Dict[str, Any], reason: str) -> str:
        """Mark trade as closed and rename the active file into the archive dir.
        Returns archive file path."""
        state["status"] = "CLOSED"
        state["close_reason"] = reason
        state["closed_at"] = datetime.utcnow().isoformat() + "Z"
        self.append_history(state, "TRADE_CLOSED", reason=reason)

        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        trade_id = state.get("trade_id", "trade")
        archive_name = f"{trade_id}_{reason}_{ts}.json"
        archive_path = os.path.join(self.archive_dir, archive_name)

        # Persist final state then move
        self.save(state)
        shutil.move(self.path, archive_path)
        self.log.info("Trade archived to %s", archive_path)
        return archive_path
