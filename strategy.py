"""Bi-weekly delta neutral NIFTY strategy.

Entry:
  Wed 15:00 IST. Sell 0.20 delta CE & PE on the next-to-next-week expiry.
  Buy CE/PE hedges 200 points away. Net credit must be >= 3.5% of capital.
  Skip if INDIA VIX >= 14.

Exit:
  - Target  = +1.0% of deployed capital
  - SL      = -1.0% of deployed capital
  - Time    = Friday 15:00 IST (i.e. 2 days before Tue expiry); max 6 sessions

Adjustments (continuously checked):
  A) Premium-double / delta imbalance: if one short delta > 2x the other, book
     the challenged leg + its hedge and redeploy that side at 0.20 delta + 200pt hedge.
  B) Delta cap: if any short delta crosses 0.35, square off that side + hedge
     and redeploy at 0.20 delta + 200pt hedge.
  C) Premium decay roll: if a short premium has decayed by >= 50% from entry,
     book that leg + hedge and redeploy at 0.20 delta + 200pt hedge.
"""
from __future__ import annotations

import math
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from utils import time_utils as tu


class DeltaNeutralStrategy:
    def __init__(self, cfg: Dict[str, Any], client, state_mgr, logger):
        self.cfg = cfg
        self.client = client
        self.state_mgr = state_mgr
        self.log = logger

        self.lot_size = int(cfg["lot_size"])
        self.lots = int(cfg["num_lots"])
        self.qty = self.lot_size * self.lots
        self.capital = float(cfg["capital"])
        self.target_delta = float(cfg["target_delta"])
        self.delta_tol = float(cfg["delta_tolerance"])
        self.max_delta = float(cfg["max_delta_threshold"])
        self.hedge_dist = int(cfg["hedge_distance_points"])
        self.imbalance_ratio = float(cfg["delta_imbalance_ratio"])
        self.decay_pct = float(cfg["premium_decay_pct"]) / 100.0
        self.min_credit_pct = float(cfg["min_net_credit_pct"]) / 100.0
        self.target_pct = float(cfg["target_profit_pct"]) / 100.0
        self.sl_pct = float(cfg["stop_loss_pct"]) / 100.0
        self.vix_threshold = float(cfg["vix_threshold"])

    # =========================================================
    # PUBLIC ENTRY POINTS
    # =========================================================
    def run_once(self) -> None:
        """Single tick of the bot. Safe to call repeatedly."""
        now = tu.now_ist()
        if not tu.is_market_open(now):
            return

        state = self.state_mgr.load()
        if state and state.get("status") == "OPEN":
            self._monitor(state, now)
        else:
            self._maybe_enter(now)

    # =========================================================
    # ENTRY
    # =========================================================
    def _maybe_enter(self, now: datetime) -> None:
        if now.strftime("%A") != self.cfg["entry_day"]:
            return
        if not tu.at_or_after(now, self.cfg["entry_time"]):
            return

        vix = self.client.get_india_vix()
        self.log.info("Entry check: VIX=%.2f (threshold %.2f)", vix, self.vix_threshold)
        if vix >= self.vix_threshold:
            self.log.info("Entry skipped: VIX above threshold.")
            return

        spot = self.client.get_underlying_spot()
        expiry = tu.selected_expiry(now.date(), weeks_ahead=int(self.cfg["expiry_offset_weeks"]))
        expiry_str = expiry.strftime("%Y-%m-%d")
        self.log.info("Entry: spot=%.2f expiry=%s", spot, expiry_str)

        chain = self.client.get_option_chain(expiry_str)
        if not chain:
            self.log.error("Empty option chain for %s", expiry_str)
            return

        ce_short = self._pick_by_delta(chain, "CE", self.target_delta)
        pe_short = self._pick_by_delta(chain, "PE", self.target_delta)
        if not ce_short or not pe_short:
            self.log.error("Could not locate 0.20 delta legs.")
            return

        ce_hedge = self._pick_hedge(chain, "CE", ce_short["strike"] + self.hedge_dist)
        pe_hedge = self._pick_hedge(chain, "PE", pe_short["strike"] - self.hedge_dist)
        if not ce_hedge or not pe_hedge:
            self.log.error("Could not locate hedge legs 200pt away.")
            return

        # Capital deployed = max margin proxy: width of spread x qty (per side).
        # This is a simple, conservative approximation; replace with broker margin call if available.
        spread_width_ce = abs(ce_hedge["strike"] - ce_short["strike"])
        spread_width_pe = abs(pe_short["strike"] - pe_hedge["strike"])
        capital_deployed = (spread_width_ce + spread_width_pe) * self.qty

        net_credit_per_unit = (
            ce_short["ltp"] - ce_hedge["ltp"] + pe_short["ltp"] - pe_hedge["ltp"]
        )
        net_credit_total = net_credit_per_unit * self.qty
        credit_pct = net_credit_total / max(capital_deployed, 1.0)
        self.log.info(
            "Net credit/unit=%.2f total=%.2f (%.2f%% of deployed %.0f); min req %.2f%%",
            net_credit_per_unit, net_credit_total, credit_pct * 100, capital_deployed,
            self.min_credit_pct * 100,
        )

        if credit_pct < self.min_credit_pct:
            self.log.info("Entry skipped: net credit below %.2f%% of deployed capital.",
                          self.min_credit_pct * 100)
            return

        # ----- place orders: hedges first, then shorts (margin friendly) -----
        legs = self._open_iron_condor(ce_short, ce_hedge, pe_short, pe_hedge)

        state = {
            "trade_id": f"DN_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}",
            "entry_dt": now.isoformat(),
            "expiry_date": expiry_str,
            "spot_at_entry": spot,
            "vix_at_entry": vix,
            "capital_deployed": capital_deployed,
            "lots": self.lots,
            "lot_size": self.lot_size,
            "qty": self.qty,
            "target_pnl": capital_deployed * self.target_pct,
            "stop_loss_pnl": capital_deployed * self.sl_pct,
            "entry_credit_per_unit": net_credit_per_unit,
            "entry_credit_total": net_credit_total,
            "legs": legs,
            "status": "OPEN",
            "close_reason": None,
            "history": [],
        }
        self.state_mgr.save(state)
        self.state_mgr.append_history(state, "TRADE_OPENED",
                                      credit_per_unit=net_credit_per_unit,
                                      capital_deployed=capital_deployed)
        self.log.info("Trade opened: %s", state["trade_id"])

    # =========================================================
    # MONITORING
    # =========================================================
    def _monitor(self, state: Dict[str, Any], now: datetime) -> None:
        # Refresh quotes + greeks
        chain = self.client.get_option_chain(state["expiry_date"])
        chain_index = self._index_chain(chain)

        legs = state["legs"]
        for name, leg in legs.items():
            row = chain_index.get(leg["instrument_key"])
            if not row:
                continue
            leg["current_price"] = row["ltp"]
            leg["current_delta"] = row["delta"]

        # ---------- P&L ----------
        pnl = self._compute_pnl(state)
        self.log.info("Monitor: %s pnl=%.2f target=%.2f sl=%.2f",
                      state["trade_id"], pnl, state["target_pnl"], state["stop_loss_pnl"])

        # ---------- exit gates ----------
        if pnl >= state["target_pnl"]:
            self._close_all(state, "TARGET_HIT")
            return
        if pnl <= -state["stop_loss_pnl"]:
            self._close_all(state, "STOP_LOSS")
            return
        if self._time_exit_due(state, now):
            self._close_all(state, "TIME_EXIT")
            return

        # ---------- adjustments ----------
        self._check_adjustments(state, chain, chain_index)

        # Persist snapshot
        self.state_mgr.save(state)

    def _time_exit_due(self, state: Dict[str, Any], now: datetime) -> bool:
        """Time exit rule: 'Friday 15:00 when expiry is 2 days away',
        i.e. the Friday immediately preceding the chosen Tuesday expiry.
        Also a hard cap of `max_holding_sessions` trading sessions."""
        entry_dt = datetime.fromisoformat(state["entry_dt"])
        if entry_dt.tzinfo is None:
            entry_dt = tu.IST.localize(entry_dt)

        expiry = datetime.strptime(state["expiry_date"], "%Y-%m-%d").date()
        exit_friday = tu.friday_before_expiry(expiry)

        # Primary gate: Friday-before-expiry at exit_time
        if now.date() == exit_friday and tu.at_or_after(now, self.cfg["exit_time"]):
            return True
        # Safety: rolled past the exit Friday for any reason
        if now.date() > exit_friday:
            return True
        # Hard cap on sessions held
        sessions = tu.trading_sessions_between(entry_dt.date(), now.date())
        if sessions >= int(self.cfg["max_holding_sessions"]):
            return True
        return False

    # =========================================================
    # ADJUSTMENTS
    # =========================================================
    def _check_adjustments(self, state, chain, chain_index) -> None:
        legs = state["legs"]
        ce_s, pe_s = legs["ce_short"], legs["pe_short"]
        ce_d = abs(ce_s.get("current_delta", ce_s["entry_delta"]))
        pe_d = abs(pe_s.get("current_delta", pe_s["entry_delta"]))

        # ---- B) hard delta cap (most urgent) ----
        if ce_d >= self.max_delta:
            self.log.warning("ADJ-B: CE short delta %.3f >= cap %.2f. Re-deploying CE side.", ce_d, self.max_delta)
            self._reset_side(state, "CE", chain)
            return
        if pe_d >= self.max_delta:
            self.log.warning("ADJ-B: PE short delta %.3f >= cap %.2f. Re-deploying PE side.", pe_d, self.max_delta)
            self._reset_side(state, "PE", chain)
            return

        # ---- A) imbalance: one side > 2x other ----
        if ce_d > self.imbalance_ratio * max(pe_d, 1e-6):
            self.log.warning("ADJ-A: CE delta %.3f > %.1fx PE delta %.3f. Re-deploying CE side.",
                             ce_d, self.imbalance_ratio, pe_d)
            self._reset_side(state, "CE", chain)
            return
        if pe_d > self.imbalance_ratio * max(ce_d, 1e-6):
            self.log.warning("ADJ-A: PE delta %.3f > %.1fx CE delta %.3f. Re-deploying PE side.",
                             pe_d, self.imbalance_ratio, ce_d)
            self._reset_side(state, "PE", chain)
            return

        # ---- C) premium decay >= 50% on either short ----
        if self._decayed(ce_s):
            self.log.info("ADJ-C: CE short premium decayed >=50%%. Rolling CE side.")
            self._reset_side(state, "CE", chain)
            return
        if self._decayed(pe_s):
            self.log.info("ADJ-C: PE short premium decayed >=50%%. Rolling PE side.")
            self._reset_side(state, "PE", chain)
            return

    def _decayed(self, leg: Dict[str, Any]) -> bool:
        entry = float(leg["entry_price"])
        cur = float(leg.get("current_price", entry))
        if entry <= 0:
            return False
        return cur <= entry * (1.0 - self.decay_pct)

    def _reset_side(self, state, side: str, chain) -> None:
        """Square off the short + hedge of `side` ('CE' or 'PE') and re-open
        a fresh 0.20 delta short + 200pt hedge on the same expiry."""
        legs = state["legs"]
        short_key = "ce_short" if side == "CE" else "pe_short"
        hedge_key = "ce_hedge" if side == "CE" else "pe_hedge"

        # 1) book existing legs (reverse sides)
        self._close_leg(legs[short_key])     # buy back short
        self._close_leg(legs[hedge_key])     # sell hedge

        # 2) pick new legs
        new_short = self._pick_by_delta(chain, side, self.target_delta)
        if not new_short:
            self.log.error("Re-deploy failed: no %s leg near 0.20 delta.", side)
            return
        if side == "CE":
            new_hedge = self._pick_hedge(chain, "CE", new_short["strike"] + self.hedge_dist)
        else:
            new_hedge = self._pick_hedge(chain, "PE", new_short["strike"] - self.hedge_dist)
        if not new_hedge:
            self.log.error("Re-deploy failed: no %s hedge 200pt away.", side)
            return

        # 3) place new orders (hedge first, then short)
        self.client.place_order(new_hedge["instrument_key"], "BUY", self.qty,
                                order_type=self.cfg["order_type"], product=self.cfg["product"])
        self.client.place_order(new_short["instrument_key"], "SELL", self.qty,
                                order_type=self.cfg["order_type"], product=self.cfg["product"])

        # 4) update state (entry baseline for the new short resets)
        legs[short_key] = self._make_leg(new_short, "SELL", side)
        legs[hedge_key] = self._make_leg(new_hedge, "BUY", side)
        self.state_mgr.append_history(state, "SIDE_REDEPLOYED", side=side,
                                      new_short_strike=new_short["strike"],
                                      new_hedge_strike=new_hedge["strike"])

    # =========================================================
    # ORDER HELPERS
    # =========================================================
    def _open_iron_condor(self, ce_s, ce_h, pe_s, pe_h) -> Dict[str, Any]:
        # Hedges first (long protection establishes margin), then shorts.
        self.client.place_order(ce_h["instrument_key"], "BUY", self.qty,
                                order_type=self.cfg["order_type"], product=self.cfg["product"])
        self.client.place_order(pe_h["instrument_key"], "BUY", self.qty,
                                order_type=self.cfg["order_type"], product=self.cfg["product"])
        self.client.place_order(ce_s["instrument_key"], "SELL", self.qty,
                                order_type=self.cfg["order_type"], product=self.cfg["product"])
        self.client.place_order(pe_s["instrument_key"], "SELL", self.qty,
                                order_type=self.cfg["order_type"], product=self.cfg["product"])
        return {
            "ce_short": self._make_leg(ce_s, "SELL", "CE"),
            "ce_hedge": self._make_leg(ce_h, "BUY",  "CE"),
            "pe_short": self._make_leg(pe_s, "SELL", "PE"),
            "pe_hedge": self._make_leg(pe_h, "BUY",  "PE"),
        }

    def _close_all(self, state, reason: str) -> None:
        self.log.info("Closing trade %s reason=%s", state["trade_id"], reason)
        for leg in state["legs"].values():
            self._close_leg(leg)
        self.state_mgr.close_trade(state, reason)

    def _close_leg(self, leg: Dict[str, Any]) -> None:
        opp = "BUY" if leg["side"] == "SELL" else "SELL"
        try:
            self.client.place_order(leg["instrument_key"], opp, leg["qty"],
                                    order_type=self.cfg["order_type"], product=self.cfg["product"])
        except Exception as e:
            self.log.error("Failed to close leg %s: %s", leg["instrument_key"], e)

    def _make_leg(self, picked, side, opt_type) -> Dict[str, Any]:
        return {
            "instrument_key": picked["instrument_key"],
            "tradingsymbol":  picked.get("tradingsymbol", ""),
            "strike":         picked["strike"],
            "option_type":    opt_type,
            "side":           side,
            "qty":            self.qty,
            "entry_price":    picked["ltp"],
            "entry_delta":    picked["delta"],
            "current_price":  picked["ltp"],
            "current_delta":  picked["delta"],
        }

    # =========================================================
    # CHAIN PARSING
    # =========================================================
    def _normalize_row(self, row: Dict[str, Any], opt_type: str) -> Optional[Dict[str, Any]]:
        """Flatten Upstox option-chain row for one side (CE/PE) into a tidy dict."""
        side_key = "call_options" if opt_type == "CE" else "put_options"
        side = row.get(side_key) or {}
        md = side.get("market_data") or {}
        gk = side.get("option_greeks") or {}
        ik = side.get("instrument_key")
        if not ik:
            return None
        ltp = md.get("ltp") or md.get("close_price") or 0.0
        delta = gk.get("delta")
        if delta is None:
            return None
        return {
            "instrument_key": ik,
            "tradingsymbol":  side.get("tradingsymbol", ""),
            "strike":         int(row.get("strike_price")),
            "option_type":    opt_type,
            "ltp":            float(ltp),
            "delta":          float(delta),
        }

    def _index_chain(self, chain: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        idx: Dict[str, Dict[str, Any]] = {}
        for row in chain:
            for opt in ("CE", "PE"):
                norm = self._normalize_row(row, opt)
                if norm:
                    idx[norm["instrument_key"]] = norm
        return idx

    def _pick_by_delta(self, chain, opt_type: str, target: float) -> Optional[Dict[str, Any]]:
        candidates = []
        for row in chain:
            norm = self._normalize_row(row, opt_type)
            if not norm:
                continue
            candidates.append(norm)
        if not candidates:
            return None
        # Compare by absolute delta (puts are negative)
        best = min(candidates, key=lambda c: abs(abs(c["delta"]) - target))
        if abs(abs(best["delta"]) - target) > self.delta_tol * 5:
            self.log.warning("Closest %s delta is %.3f (target %.2f); using anyway.",
                             opt_type, best["delta"], target)
        return best

    def _pick_hedge(self, chain, opt_type: str, desired_strike: int) -> Optional[Dict[str, Any]]:
        candidates = []
        for row in chain:
            norm = self._normalize_row(row, opt_type)
            if not norm:
                continue
            candidates.append(norm)
        if not candidates:
            return None
        # Snap to closest available strike
        best = min(candidates, key=lambda c: abs(c["strike"] - desired_strike))
        return best

    # =========================================================
    # PnL
    # =========================================================
    def _compute_pnl(self, state: Dict[str, Any]) -> float:
        pnl = 0.0
        for leg in state["legs"].values():
            entry = float(leg["entry_price"])
            cur = float(leg.get("current_price", entry))
            if leg["side"] == "SELL":
                pnl += (entry - cur) * leg["qty"]
            else:
                pnl += (cur - entry) * leg["qty"]
        return pnl
