"""Bi-weekly delta neutral NIFTY strategy.

Entry (Wed 15:00-15:25 IST, INDIA VIX < 14):
  1. On the next-to-next-week Tue expiry, find the 0.20 delta CE & PE strikes
     restricted to 100-spaced strikes (skip 50-strikes; less liquid).
  2. PREMIUM BALANCE: anchor to the side with the lower premium. Walk the
     other (richer) side further OTM (still on 100-strikes, with delta floor)
     until premiums are roughly matched.
  3. Buy CE/PE hedges 200pts away from the *adjusted* shorts (also 100-strike).
  4. Net credit gate: must be >= 3.5% of capital deployed.
  5. Place all four legs as LIMIT orders at the bid-ask mid; if a leg is
     unfilled within 60s, cancel and re-price; after `limit_max_attempts`
     fall back to MARKET so execution is guaranteed.

Exit:
  - Target  = +1.0% of deployed capital  -> close all (MARKET)
  - HARD SL = -1.0% of deployed capital  -> close all (MARKET)  [first priority]
  - Time    = Friday 15:00 before expiry -> close all (MARKET)

Adjustments (continuously checked when no exit gate fired):
  A) Premium-double / delta imbalance: if one short delta > 2x the other,
     square off that side + hedge with MARKET, redeploy at 0.20 delta + 200pt
     hedge using LIMIT-with-retry.
  B) Delta cap 0.35: same flow.
  C) Premium decay >= 50%: same flow.
"""
from __future__ import annotations

import uuid
from datetime import datetime
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
        self.min_delta_floor = float(cfg.get("min_delta_floor", 0.10))
        self.hedge_dist = int(cfg["hedge_distance_points"])
        self.imbalance_ratio = float(cfg["delta_imbalance_ratio"])
        self.decay_pct = float(cfg["premium_decay_pct"]) / 100.0
        self.premium_balance_tol = float(cfg.get("premium_balance_tolerance_pct", 5.0)) / 100.0

        self.min_credit_pct = float(cfg["min_net_credit_pct"]) / 100.0
        self.target_pct = float(cfg["target_profit_pct"]) / 100.0
        self.sl_pct = float(cfg["stop_loss_pct"]) / 100.0
        self.vix_threshold = float(cfg["vix_threshold"])

        self.strike_step = int(cfg.get("strike_step", 100))
        self.tick_size = float(cfg.get("tick_size", 0.05))
        self.product = cfg["product"]

        self.lim_attempts = int(cfg.get("limit_max_attempts", 5))
        self.lim_wait = int(cfg.get("limit_wait_seconds", 60))
        self.lim_fallback = bool(cfg.get("limit_fallback_to_market", True))
        self.mkt_attempts = int(cfg.get("market_max_attempts", 3))
        self.mkt_wait = float(cfg.get("market_wait_seconds", 10))

        # Defensive: before placing any closing order, verify the broker still
        # shows our expected position. If the leg has been closed/reduced
        # externally (e.g. user manual intervention), skip the close so we
        # don't accidentally open a brand-new opposite position.
        self.verify_position_before_close = bool(
            cfg.get("verify_position_before_close", True)
        )

    # =========================================================
    # PUBLIC ENTRY POINT
    # =========================================================
    def run_once(self) -> None:
        """Single tick of the bot. Only acts inside the safe monitor window
        (skips 9:15 and 15:30 candles)."""
        now = tu.now_ist()
        if not tu.is_in_monitor_window(now):
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
        if not tu.is_within(now, self.cfg["entry_time"], self.cfg["monitor_window_end"]):
            self.log.info("Entry skipped: outside safe entry window %s-%s.",
                          self.cfg["entry_time"], self.cfg["monitor_window_end"])
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

        # ---- 1+2: pick balanced shorts (100-strike filter, premium-matched) ----
        ce_short, pe_short = self._pick_balanced_shorts(chain)
        if not ce_short or not pe_short:
            self.log.error("Could not locate balanced short legs.")
            return

        # ---- 3: hedges 200pt from the (possibly-shifted) shorts ----
        ce_hedge = self._pick_hedge(chain, "CE", ce_short["strike"] + self.hedge_dist)
        pe_hedge = self._pick_hedge(chain, "PE", pe_short["strike"] - self.hedge_dist)
        if not ce_hedge or not pe_hedge:
            self.log.error("Could not locate hedge legs 200pt away.")
            return

        # ---- 4: capital + credit gate (uses LTPs at decision time) ----
        spread_width_ce = abs(ce_hedge["strike"] - ce_short["strike"])
        spread_width_pe = abs(pe_short["strike"] - pe_hedge["strike"])
        capital_deployed = (spread_width_ce + spread_width_pe) * self.qty

        net_credit_per_unit = (ce_short["ltp"] - ce_hedge["ltp"]
                               + pe_short["ltp"] - pe_hedge["ltp"])
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

        self.log.info(
            "Strikes: CE_short=%d (Δ%.3f, ₹%.2f) hedge=%d  |  PE_short=%d (Δ%.3f, ₹%.2f) hedge=%d",
            ce_short["strike"], ce_short["delta"], ce_short["ltp"], ce_hedge["strike"],
            pe_short["strike"], pe_short["delta"], pe_short["ltp"], pe_hedge["strike"],
        )

        # ---- 5: place orders -- LIMIT-with-retry. Hedges first, then shorts ----
        # Per-trade tag goes on every order placed for this trade. Lets you
        # filter the bot's orders in the Upstox order book and pin every order
        # back to a specific trade. Upstox tag field caps at 20 chars.
        trade_id = f"DN_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        tag = (f"DN_{now.strftime('%y%m%d_%H%M')}_{uuid.uuid4().hex[:3]}")[:20]
        legs = self._open_iron_condor(ce_short, ce_hedge, pe_short, pe_hedge, tag=tag)

        # Realized credit using ACTUAL fills
        actual_credit_per_unit = (
            legs["ce_short"]["entry_price"] - legs["ce_hedge"]["entry_price"]
            + legs["pe_short"]["entry_price"] - legs["pe_hedge"]["entry_price"]
        )
        actual_credit_total = actual_credit_per_unit * self.qty

        state = {
            "trade_id": trade_id,
            "tag": tag,
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
            "entry_credit_per_unit_quoted": net_credit_per_unit,
            "entry_credit_per_unit_filled": actual_credit_per_unit,
            "entry_credit_total_filled": actual_credit_total,
            "legs": legs,
            "status": "OPEN",
            "close_reason": None,
            "history": [],
        }
        self.state_mgr.save(state)
        self.state_mgr.append_history(
            state, "TRADE_OPENED",
            credit_per_unit_quoted=net_credit_per_unit,
            credit_per_unit_filled=actual_credit_per_unit,
            capital_deployed=capital_deployed,
        )
        self.log.info("Trade opened: %s (filled credit/unit=%.2f)",
                      state["trade_id"], actual_credit_per_unit)
        self._log_monitor_snapshot(state, pnl=0.0)

    # =========================================================
    # MONITORING
    # =========================================================
    def _monitor(self, state: Dict[str, Any], now: datetime) -> None:
        chain = self.client.get_option_chain(state["expiry_date"])
        chain_index = self._index_chain(chain)

        legs = state["legs"]
        for leg in legs.values():
            row = chain_index.get(leg["instrument_key"])
            if not row:
                continue
            leg["current_price"] = row["ltp"]
            leg["current_delta"] = row["delta"]

        pnl = self._compute_pnl(state)
        self._log_monitor_snapshot(state, pnl)

        # Exit gates -- priority order. SL is HARD: no rolls, no adjustments.
        if pnl >= state["target_pnl"]:
            self._close_all(state, "TARGET_HIT")
            return
        if pnl <= -state["stop_loss_pnl"]:
            self.log.warning("HARD STOP-LOSS HIT: pnl=%.2f <= -%.2f. Squaring off ALL legs.",
                             pnl, state["stop_loss_pnl"])
            self._close_all(state, "STOP_LOSS")
            return
        if self._time_exit_due(state, now):
            self._close_all(state, "TIME_EXIT")
            return

        self._check_adjustments(state, chain)
        self.state_mgr.save(state)

    def _time_exit_due(self, state: Dict[str, Any], now: datetime) -> bool:
        entry_dt = datetime.fromisoformat(state["entry_dt"])
        if entry_dt.tzinfo is None:
            entry_dt = tu.IST.localize(entry_dt)

        expiry = datetime.strptime(state["expiry_date"], "%Y-%m-%d").date()
        exit_friday = tu.friday_before_expiry(expiry)

        if now.date() == exit_friday and tu.at_or_after(now, self.cfg["exit_time"]):
            return True
        if now.date() > exit_friday:
            return True
        sessions = tu.trading_sessions_between(entry_dt.date(), now.date())
        if sessions >= int(self.cfg["max_holding_sessions"]):
            return True
        return False

    # =========================================================
    # ADJUSTMENTS
    # =========================================================
    def _check_adjustments(self, state, chain) -> None:
        legs = state["legs"]
        ce_s, pe_s = legs["ce_short"], legs["pe_short"]
        ce_d = abs(ce_s.get("current_delta", ce_s["entry_delta"]))
        pe_d = abs(pe_s.get("current_delta", pe_s["entry_delta"]))

        # B) hard delta cap
        if ce_d >= self.max_delta:
            self.log.warning("ADJ-B: CE Δ %.3f >= cap %.2f. Re-deploying CE.", ce_d, self.max_delta)
            self._reset_side(state, "CE", chain); return
        if pe_d >= self.max_delta:
            self.log.warning("ADJ-B: PE Δ %.3f >= cap %.2f. Re-deploying PE.", pe_d, self.max_delta)
            self._reset_side(state, "PE", chain); return

        # A) imbalance: one side > 2x other
        if ce_d > self.imbalance_ratio * max(pe_d, 1e-6):
            self.log.warning("ADJ-A: CE Δ %.3f > %.1fx PE Δ %.3f. Re-deploying CE.",
                             ce_d, self.imbalance_ratio, pe_d)
            self._reset_side(state, "CE", chain); return
        if pe_d > self.imbalance_ratio * max(ce_d, 1e-6):
            self.log.warning("ADJ-A: PE Δ %.3f > %.1fx CE Δ %.3f. Re-deploying PE.",
                             pe_d, self.imbalance_ratio, ce_d)
            self._reset_side(state, "PE", chain); return

        # C) premium decay >= 50%
        if self._decayed(ce_s):
            self.log.info("ADJ-C: CE premium decayed >=50%%. Rolling CE.")
            self._reset_side(state, "CE", chain); return
        if self._decayed(pe_s):
            self.log.info("ADJ-C: PE premium decayed >=50%%. Rolling PE.")
            self._reset_side(state, "PE", chain); return

    def _decayed(self, leg: Dict[str, Any]) -> bool:
        entry = float(leg["entry_price"])
        cur = float(leg.get("current_price", entry))
        if entry <= 0:
            return False
        return cur <= entry * (1.0 - self.decay_pct)

    def _reset_side(self, state, side: str, chain) -> None:
        """F: First close existing strikes with MARKET orders, then place
        the new sell + hedge using LIMIT-with-retry. All orders carry the
        trade's tag so the new redeploys are traceable to the same trade."""
        legs = state["legs"]
        tag = state.get("tag", "delta_neutral")
        short_key = "ce_short" if side == "CE" else "pe_short"
        hedge_key = "ce_hedge" if side == "CE" else "pe_hedge"

        # 1) Close existing -- MARKET (fast, certain). Position-existence check
        # is built into _close_leg so user-side manual closures are safe.
        self._close_leg(legs[short_key], tag)
        self._close_leg(legs[hedge_key], tag)

        # 2) Pick fresh 0.20Δ short on 100-strikes
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

        # 3) Open new -- LIMIT-with-retry. Hedge first, then short.
        h_res = self._place_limit_open(new_hedge["instrument_key"], "BUY", tag)
        s_res = self._place_limit_open(new_short["instrument_key"], "SELL", tag)

        legs[short_key] = self._make_leg(new_short, "SELL", side, fill=s_res)
        legs[hedge_key] = self._make_leg(new_hedge, "BUY",  side, fill=h_res)
        self.state_mgr.append_history(
            state, "SIDE_REDEPLOYED", side=side,
            new_short_strike=new_short["strike"], new_hedge_strike=new_hedge["strike"],
            short_filled_qty=s_res.filled_qty, hedge_filled_qty=h_res.filled_qty,
        )

    # =========================================================
    # ORDER HELPERS
    # =========================================================
    def _open_iron_condor(self, ce_s, ce_h, pe_s, pe_h, tag: str) -> Dict[str, Any]:
        # Hedges first (long protection establishes margin), then shorts.
        ch_res = self._place_limit_open(ce_h["instrument_key"], "BUY", tag)
        ph_res = self._place_limit_open(pe_h["instrument_key"], "BUY", tag)
        cs_res = self._place_limit_open(ce_s["instrument_key"], "SELL", tag)
        ps_res = self._place_limit_open(pe_s["instrument_key"], "SELL", tag)
        return {
            "ce_short": self._make_leg(ce_s, "SELL", "CE", fill=cs_res),
            "ce_hedge": self._make_leg(ce_h, "BUY",  "CE", fill=ch_res),
            "pe_short": self._make_leg(pe_s, "SELL", "PE", fill=ps_res),
            "pe_hedge": self._make_leg(pe_h, "BUY",  "PE", fill=ph_res),
        }

    def _place_limit_open(self, instrument_key: str, side: str, tag: str):
        return self.client.place_limit_with_retry(
            instrument_key=instrument_key, side=side, quantity=self.qty,
            max_attempts=self.lim_attempts, wait_seconds=self.lim_wait,
            product=self.product, tick_size=self.tick_size,
            fallback_to_market=self.lim_fallback,
            market_fallback_attempts=self.mkt_attempts,
            tag=tag,
        )

    def _close_all(self, state, reason: str) -> None:
        """Close ONLY the legs the bot itself opened (those listed in
        state['legs']). This intentionally never queries broker positions for
        a 'close all' sweep -- any manual trades you have running on other
        instruments (or even on the same instrument as additional contracts)
        are NOT touched by this method."""
        self.log.info("Closing trade %s reason=%s", state["trade_id"], reason)
        tag = state.get("tag", "delta_neutral")
        for leg in state["legs"].values():
            self._close_leg(leg, tag)
        # Final snapshot so the terminal shows the closing P&L line as well.
        self._log_monitor_snapshot(state, pnl=self._compute_pnl(state))
        self.state_mgr.close_trade(state, reason)

    def _close_leg(self, leg: Dict[str, Any], tag: str) -> None:
        """Close a leg with a MARKET order (closes are always MARKET).
        Skips with a clear log line if the broker no longer shows our expected
        position (e.g. user manually closed it from the Upstox app). Tracks
        partial fills the same way opens do."""
        if self.verify_position_before_close and not self._verify_position_exists(leg):
            leg["close_status"] = "SKIPPED_POSITION_NOT_FOUND"
            leg["close_status_at"] = datetime.utcnow().isoformat() + "Z"
            return

        opp = "BUY" if leg["side"] == "SELL" else "SELL"
        try:
            res = self.client.place_market_and_fill(
                instrument_key=leg["instrument_key"], side=opp, quantity=leg["qty"],
                product=self.product, max_attempts=self.mkt_attempts,
                wait_seconds=self.mkt_wait, tag=tag,
            )
            leg["exit_order_id"] = res.order_id
            leg["exit_filled_qty"] = res.filled_qty
            if res.avg_price is not None:
                leg["exit_price"] = res.avg_price
            leg["close_status"] = (
                "CLOSED" if res.filled_qty >= leg["qty"] else "PARTIALLY_CLOSED"
            )
            if res.filled_qty < leg["qty"]:
                self.log.error("Leg %s only partially closed: %d/%d. Manual reconciliation needed.",
                               leg["instrument_key"], res.filled_qty, leg["qty"])
        except Exception as e:
            self.log.error("Failed to close leg %s: %s", leg["instrument_key"], e)
            leg["close_status"] = "ERROR"

    def _verify_position_exists(self, leg: Dict[str, Any]) -> bool:
        """Confirm the broker still shows the bot's expected position before
        closing. Returns True (allow close) if positions API is unreachable;
        returns False if the leg's specific instrument shows less than the
        bot's expected size (i.e. it was closed/reduced externally).

        Note: positions are netted at the broker. If you are also TRADING the
        same instrument manually (adding to the bot's short, say), the netted
        size is MORE negative than expected, so the check still passes and the
        bot's BUY-to-close just unwinds its own qty -- your manual position
        stays intact.
        """
        try:
            positions = self.client.get_positions()
        except Exception as e:
            self.log.warning(
                "Position verification failed (%s). Proceeding with close as a fallback.", e,
            )
            return True

        ik = leg["instrument_key"]
        expected_side = leg["side"]              # "SELL" or "BUY"
        expected_qty = int(leg["qty"])

        for p in positions:
            pkey = p.get("instrument_token") or p.get("instrument_key")
            if pkey != ik:
                continue
            qty_signed = int(p.get("quantity") or 0)
            if expected_side == "SELL":
                ok = qty_signed <= -expected_qty
            else:
                ok = qty_signed >= expected_qty
            if ok:
                return True
            self.log.error(
                "POSITION DRIFT on %s: expected %s %d (signed %d), broker shows %d. "
                "Skipping bot close to avoid opening a new opposite position. "
                "Reconcile manually.",
                ik, expected_side, expected_qty,
                -expected_qty if expected_side == "SELL" else expected_qty, qty_signed,
            )
            return False

        self.log.error(
            "POSITION NOT FOUND at broker: %s (expected %s %d). "
            "Likely closed externally. Skipping bot close.",
            ik, expected_side, expected_qty,
        )
        return False

    def _make_leg(self, picked, side, opt_type, fill=None) -> Dict[str, Any]:
        """Build a leg dict using the FillResult (preferred) or fall back to LTP.
        `qty` reflects the ACTUAL filled quantity, which may be less than the
        intended `self.qty` if all retries failed to fully fill."""
        if fill is not None and fill.avg_price is not None and fill.filled_qty > 0:
            entry = float(fill.avg_price)
            qty = int(fill.filled_qty)
        else:
            entry = float(picked["ltp"])
            qty = self.qty
        intended = self.qty
        return {
            "instrument_key":  picked["instrument_key"],
            "tradingsymbol":   picked.get("tradingsymbol", ""),
            "strike":          picked["strike"],
            "option_type":     opt_type,
            "side":            side,
            "qty":             qty,                # actual filled
            "intended_qty":    intended,           # what we asked for
            "qty_shortfall":   max(0, intended - qty),
            "entry_price":     entry,
            "entry_delta":     picked["delta"],
            "current_price":   entry,
            "current_delta":   picked["delta"],
            "entry_order_id":  fill.order_id if fill else None,
        }

    # =========================================================
    # CHAIN PARSING + STRIKE SELECTION
    # =========================================================
    def _normalize_row(self, row: Dict[str, Any], opt_type: str) -> Optional[Dict[str, Any]]:
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

    def _liquid_candidates(self, chain, opt_type: str) -> List[Dict[str, Any]]:
        """Normalized rows on 100-spaced strikes only (skip 50-strikes, less liquid)."""
        out = []
        for row in chain:
            norm = self._normalize_row(row, opt_type)
            if not norm:
                continue
            if norm["strike"] % self.strike_step != 0:
                continue
            out.append(norm)
        return out

    def _pick_by_delta(self, chain, opt_type: str, target: float
                       ) -> Optional[Dict[str, Any]]:
        cands = self._liquid_candidates(chain, opt_type)
        if not cands:
            return None
        best = min(cands, key=lambda c: abs(abs(c["delta"]) - target))
        if abs(abs(best["delta"]) - target) > self.delta_tol * 5:
            self.log.warning("Closest %s Δ on 100-strikes is %.3f (target %.2f).",
                             opt_type, best["delta"], target)
        return best

    def _pick_hedge(self, chain, opt_type: str, desired_strike: int
                    ) -> Optional[Dict[str, Any]]:
        cands = self._liquid_candidates(chain, opt_type)
        if not cands:
            return None
        return min(cands, key=lambda c: abs(c["strike"] - desired_strike))

    def _pick_by_premium(self, chain, opt_type: str, target_premium: float,
                         ref_strike: int, direction: str
                         ) -> Optional[Dict[str, Any]]:
        """Find 100-strike of opt_type with LTP closest to target_premium.
        `direction='up'`  -> only strikes > ref_strike (further OTM for CE).
        `direction='down'`-> only strikes < ref_strike (further OTM for PE).
        Enforces a minimum delta floor so we don't pick lottery-ticket OTMs.
        """
        cands = []
        for c in self._liquid_candidates(chain, opt_type):
            if direction == "up" and c["strike"] <= ref_strike:
                continue
            if direction == "down" and c["strike"] >= ref_strike:
                continue
            if abs(c["delta"]) < self.min_delta_floor:
                continue
            cands.append(c)
        if not cands:
            return None
        return min(cands, key=lambda c: abs(c["ltp"] - target_premium))

    def _pick_balanced_shorts(self, chain
                              ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """C+D: pick 0.20Δ CE & PE on 100-strikes; if premiums diverge, walk
        the richer side further OTM until premiums match the lower one."""
        ce = self._pick_by_delta(chain, "CE", self.target_delta)
        pe = self._pick_by_delta(chain, "PE", self.target_delta)
        if not ce or not pe:
            return None, None

        ce_p, pe_p = ce["ltp"], pe["ltp"]
        self.log.info("0.20Δ candidates  CE %d Δ%.3f ₹%.2f  |  PE %d Δ%.3f ₹%.2f",
                      ce["strike"], ce["delta"], ce_p,
                      pe["strike"], pe["delta"], pe_p)

        anchor = min(ce_p, pe_p)
        if anchor <= 0:
            self.log.warning("Anchor premium <= 0; skipping premium balancing.")
            return ce, pe

        if abs(ce_p - pe_p) / anchor <= self.premium_balance_tol:
            return ce, pe   # already symmetric

        if ce_p > pe_p:
            new_ce = self._pick_by_premium(chain, "CE", target_premium=pe_p,
                                           ref_strike=ce["strike"], direction="up")
            if new_ce:
                self.log.info("Premium balance: CE shifted %d -> %d (₹%.2f -> ₹%.2f, target %.2f).",
                              ce["strike"], new_ce["strike"], ce_p, new_ce["ltp"], pe_p)
                ce = new_ce
        else:
            new_pe = self._pick_by_premium(chain, "PE", target_premium=ce_p,
                                           ref_strike=pe["strike"], direction="down")
            if new_pe:
                self.log.info("Premium balance: PE shifted %d -> %d (₹%.2f -> ₹%.2f, target %.2f).",
                              pe["strike"], new_pe["strike"], pe_p, new_pe["ltp"], ce_p)
                pe = new_pe

        return ce, pe

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

    @staticmethod
    def _leg_pnl(leg: Dict[str, Any]) -> float:
        entry = float(leg["entry_price"])
        cur = float(leg.get("current_price", entry))
        if leg["side"] == "SELL":
            return (entry - cur) * leg["qty"]
        return (cur - entry) * leg["qty"]

    def _log_monitor_snapshot(self, state: Dict[str, Any], pnl: float) -> None:
        """Compact multi-line snapshot printed each tick.

        Shows running P&L vs target / stop-loss and one line per leg with the
        strike, current delta (with entry delta in parentheses for the sold
        legs since their delta drift drives every adjustment rule), premium
        move (entry -> current), and per-leg P&L.
        """
        legs = state["legs"]
        ce_s, pe_s = legs["ce_short"], legs["pe_short"]
        ce_h, pe_h = legs["ce_hedge"], legs["pe_hedge"]
        target = state["target_pnl"]
        sl = state["stop_loss_pnl"]

        self.log.info("-" * 78)
        self.log.info(
            "Monitor %s  PnL=%+10.2f   target=+%-9.2f  SL=-%-9.2f  deployed=%.0f",
            state["trade_id"], pnl, target, sl, state["capital_deployed"],
        )

        def _fmt_short(name: str, leg: Dict[str, Any]) -> str:
            cur_d = leg.get("current_delta", leg["entry_delta"])
            cur_p = leg.get("current_price", leg["entry_price"])
            return (
                f"  {name} {leg['option_type']} {leg['strike']:>6}"
                f"  delta={cur_d:+.3f} (entry {leg['entry_delta']:+.3f})"
                f"  prem={leg['entry_price']:>6.2f} -> {cur_p:>6.2f}"
                f"  legPnL={self._leg_pnl(leg):+9.2f}"
            )

        def _fmt_hedge(name: str, leg: Dict[str, Any]) -> str:
            cur_d = leg.get("current_delta", leg["entry_delta"])
            cur_p = leg.get("current_price", leg["entry_price"])
            return (
                f"  {name} {leg['option_type']} {leg['strike']:>6}"
                f"  delta={cur_d:+.3f}                   "
                f"  prem={leg['entry_price']:>6.2f} -> {cur_p:>6.2f}"
                f"  legPnL={self._leg_pnl(leg):+9.2f}"
            )

        # Sell legs first (their deltas drive adjustments)
        self.log.info(_fmt_short("SELL", ce_s))
        self.log.info(_fmt_short("SELL", pe_s))
        self.log.info(_fmt_hedge("BUY ", ce_h))
        self.log.info(_fmt_hedge("BUY ", pe_h))
