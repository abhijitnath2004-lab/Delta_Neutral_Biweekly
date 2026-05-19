"""Upstox API wrapper -- mixed v2 / v3 per requirement.

Endpoint -> version mapping (driven by config.upstox.endpoints):
  GET    /v2/option/chain                       option chain + greeks
  GET    /v2/market-quote/quotes                full quote (incl. depth/bid/ask)
  GET    /v2/market-quote/ltp                   compact LTP (spot, VIX)
  POST   /v3/order/place                        place order
  DELETE /v3/order/cancel                       cancel a working order
  GET    /v2/order/details                      order status + average_price
  GET    /v2/portfolio/short-term-positions     positions

Both `place_limit_with_retry` and `place_market_and_fill` track partial fills
across attempts. They keep placing follow-up orders for the remaining quantity
until the full target is filled or `max_attempts` is exhausted, then return a
weighted-average price + the final filled quantity.

Docs: https://upstox.com/developer/api-documentation/
"""
from __future__ import annotations

import os
import time as _time
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import requests


class UpstoxError(RuntimeError):
    pass


class FillResult(NamedTuple):
    """Outcome of a (potentially multi-attempt) order placement."""
    order_id: Optional[str]      # last order_id involved
    avg_price: Optional[float]   # weighted-average fill price across all fills
    filled_qty: int              # total filled quantity across all attempts


class UpstoxClient:
    def __init__(self, cfg: Dict[str, Any], logger):
        self.cfg = cfg
        self.log = logger
        ux = cfg["upstox"]
        self.base_v2 = ux["base_url_v2"].rstrip("/")
        self.base_v3 = ux["base_url_v3"].rstrip("/")
        self.endpoints = ux["endpoints"]

        # Token loading: prefer a file path (the user runs a daily token
        # generator that writes to e.g. /home/opc/TOKEN/upstox_token.txt) but
        # fall back to an environment variable for dev / CI environments.
        # The file's mtime is tracked so a fresh token written next morning
        # is picked up automatically without restarting the bot.
        self._token_file = ux.get("access_token_file") or None
        self._token_env  = ux.get("access_token_env")  or None
        self._token_mtime: Optional[float] = None
        self._token: str = self._load_token()

        self._sess = requests.Session()
        self._fill_attempts = int(ux.get("fill_poll_attempts", 5))
        self._fill_delay = float(ux.get("fill_poll_delay_seconds", 1.0))

    # ---------------------------------------------------------
    # Token loading + auto-refresh
    # ---------------------------------------------------------
    def _load_token(self) -> str:
        """Read the access token from the configured file (preferred) or env
        var (fallback). Updates the cached file mtime when the file is used."""
        if self._token_file:
            path = os.path.expanduser(self._token_file)
            try:
                with open(path, "r") as f:
                    token = f.read().strip()
                if token:
                    try:
                        self._token_mtime = os.path.getmtime(path)
                    except OSError:
                        self._token_mtime = None
                    self.log.info("Loaded Upstox access token from %s", path)
                    return token
                self.log.warning("Token file %s is empty; falling back to env var.", path)
            except FileNotFoundError:
                self.log.warning("Token file %s not found; falling back to env var.", path)
            except Exception as e:
                self.log.warning("Failed to read token file %s: %s", path, e)

        if self._token_env:
            token = os.environ.get(self._token_env)
            if token:
                self.log.info("Loaded Upstox access token from env var %s", self._token_env)
                return token

        raise UpstoxError(
            f"No Upstox access token found. Tried file '{self._token_file}' "
            f"and env var '{self._token_env}'."
        )

    def _maybe_refresh_token(self) -> None:
        """If the token file has been rewritten since we last read it (e.g. by
        the daily morning token generator), reload it transparently."""
        if not self._token_file:
            return
        path = os.path.expanduser(self._token_file)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return
        if self._token_mtime is None or mtime > self._token_mtime:
            self.log.info("Token file mtime changed; reloading access token.")
            try:
                self._token = self._load_token()
            except Exception as e:
                self.log.error("Token reload failed: %s (continuing with previous token)", e)

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }

    # =========================================================
    # internal: route to v2 or v3
    # =========================================================
    def _base_for(self, ep_key: str) -> str:
        v = self.endpoints[ep_key]["version"]
        return self.base_v3 if v == "v3" else self.base_v2

    def _path_for(self, ep_key: str) -> str:
        return self.endpoints[ep_key]["path"]

    def _request(self, method: str, ep_key: str, **kwargs) -> Dict[str, Any]:
        # Pick up a freshly-rotated token file if the morning generator has run.
        self._maybe_refresh_token()
        url = f"{self._base_for(ep_key)}{self._path_for(ep_key)}"
        reloaded_on_401 = False
        for attempt in range(3):
            try:
                r = self._sess.request(method, url, headers=self._auth_headers(),
                                       timeout=15, **kwargs)
                # Token expired / unauthorized -- force a reload from disk
                # and retry exactly once with the fresh token.
                if r.status_code == 401 and not reloaded_on_401:
                    self.log.warning(
                        "Upstox returned 401 on %s %s; reloading token and retrying once.",
                        method, ep_key,
                    )
                    try:
                        self._token = self._load_token()
                        self._token_mtime = None  # force re-stat next time
                    except Exception as e:
                        self.log.error("Token reload after 401 failed: %s", e)
                        raise UpstoxError(f"401 unauthorized and token reload failed: {e}")
                    reloaded_on_401 = True
                    continue
                if r.status_code >= 500:
                    raise UpstoxError(f"{r.status_code}: {r.text[:200]}")
                if r.status_code == 429:
                    _time.sleep(1.5 * (attempt + 1))
                    continue
                data = r.json()
                if r.status_code >= 400:
                    raise UpstoxError(f"{r.status_code}: {data}")
                return data
            except (requests.RequestException, UpstoxError) as e:
                self.log.warning("Upstox %s %s failed (attempt %d): %s",
                                 method, ep_key, attempt + 1, e)
                if attempt == 2:
                    raise
                _time.sleep(1.0 * (attempt + 1))
        raise UpstoxError("unreachable")

    @staticmethod
    def _round_to_tick(price: float, tick: float = 0.05) -> float:
        return round(round(price / tick) * tick, 2)

    @staticmethod
    def _extract_order_id(resp: Dict[str, Any]) -> Optional[str]:
        data = resp.get("data") or {}
        return (
            data.get("order_id")
            or (data.get("order_ids") or [None])[0]
            or resp.get("order_id")
        )

    # =========================================================
    # quotes (v2)
    # =========================================================
    def get_ltp(self, instrument_keys: List[str]) -> Dict[str, float]:
        params = {"instrument_key": ",".join(instrument_keys)}
        data = self._request("GET", "ltp", params=params)
        out: Dict[str, float] = {}
        for k, v in (data.get("data") or {}).items():
            ik = v.get("instrument_token") or v.get("instrument_key") or k
            out[ik] = float(v["last_price"])
        return out

    def get_quote(self, instrument_keys: List[str]) -> Dict[str, Dict[str, Any]]:
        params = {"instrument_key": ",".join(instrument_keys)}
        data = self._request("GET", "market_quote", params=params)
        return data.get("data") or {}

    def get_bid_ask(self, instrument_key: str) -> Tuple[Optional[float], Optional[float]]:
        """Top-of-book bid/ask via v2 quote depth. Returns (bid, ask) or (None, None)."""
        try:
            data = self.get_quote([instrument_key])
        except Exception as e:
            self.log.warning("get_bid_ask(%s) failed: %s", instrument_key, e)
            return None, None
        if not data:
            return None, None
        q = next(iter(data.values()))
        depth = q.get("depth") or {}
        buys = depth.get("buy") or []
        sells = depth.get("sell") or []
        bid = float(buys[0]["price"]) if buys and buys[0].get("price") else None
        ask = float(sells[0]["price"]) if sells and sells[0].get("price") else None
        return bid, ask

    def get_india_vix(self) -> float:
        ik = self.cfg["vix_instrument_key"]
        ltp = self.get_ltp([ik])
        return ltp[ik] if ik in ltp else float(next(iter(ltp.values())))

    def get_underlying_spot(self) -> float:
        ik = self.cfg["underlying_instrument_key"]
        ltp = self.get_ltp([ik])
        return ltp[ik] if ik in ltp else float(next(iter(ltp.values())))

    # =========================================================
    # option chain (v2)
    # =========================================================
    def get_option_chain(self, expiry_date: str) -> List[Dict[str, Any]]:
        params = {
            "instrument_key": self.cfg["underlying_instrument_key"],
            "expiry_date": expiry_date,
        }
        data = self._request("GET", "option_chain", params=params)
        return data.get("data") or []

    # =========================================================
    # orders -- low level
    # =========================================================
    def place_order(
        self,
        instrument_key: str,
        side: str,
        quantity: int,
        order_type: str = "MARKET",
        product: str = "D",
        price: float = 0.0,
        trigger_price: float = 0.0,
        tag: str = "delta_neutral",
        validity: str = "DAY",
    ) -> Dict[str, Any]:
        body = {
            "quantity": int(quantity),
            "product": product,
            "validity": validity,
            "price": float(price),
            "tag": tag,
            "instrument_token": instrument_key,
            "order_type": order_type,
            "transaction_type": side.upper(),
            "disclosed_quantity": 0,
            "trigger_price": float(trigger_price),
            "is_amo": False,
            "slice": False,
        }
        self.log.info("PLACE [v3] %s %s qty=%d %s price=%.2f",
                      side, instrument_key, quantity, order_type, price)
        return self._request("POST", "place_order", json=body)

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        params = {"order_id": order_id}
        self.log.info("CANCEL [v3] order_id=%s", order_id)
        return self._request("DELETE", "cancel_order", params=params)

    def get_order_details(self, order_id: str) -> Dict[str, Any]:
        params = {"order_id": order_id}
        data = self._request("GET", "order_details", params=params)
        return data.get("data") or {}

    # =========================================================
    # orders -- helpers
    # =========================================================
    def _wait_for_terminal(self, order_id: str, timeout_seconds: float,
                           poll_interval: float = 2.0) -> str:
        """Poll order details until terminal status (complete/rejected/cancelled)
        or timeout. Returns the last-seen status (or 'pending')."""
        deadline = _time.time() + float(timeout_seconds)
        last_status = "pending"
        while _time.time() < deadline:
            try:
                od = self.get_order_details(order_id)
                status = (od.get("status") or od.get("order_status") or "").lower()
                last_status = status or last_status
                if status in ("complete", "rejected", "cancelled"):
                    return status
            except Exception as e:
                self.log.warning("[%s] order detail poll failed: %s", order_id, e)
            _time.sleep(poll_interval)
        return last_status

    def _fetch_summary(self, order_id: str
                       ) -> Tuple[str, Optional[float], int]:
        """Fetch (status, average_price, filled_quantity) for an order.
        Tolerates field-name variation across Upstox versions."""
        try:
            od = self.get_order_details(order_id)
        except Exception as e:
            self.log.warning("[%s] summary fetch failed: %s", order_id, e)
            return ("unknown", None, 0)
        status = (od.get("status") or od.get("order_status") or "unknown").lower()
        avg = od.get("average_price")
        filled = od.get("filled_quantity")
        if filled is None:
            filled = od.get("filled_qty")
        try:
            avg_f = float(avg) if avg is not None else None
        except (TypeError, ValueError):
            avg_f = None
        try:
            qty_i = int(filled) if filled is not None else 0
        except (TypeError, ValueError):
            qty_i = 0
        return (status, avg_f, qty_i)

    # =========================================================
    # orders -- high level
    # =========================================================
    def place_market_and_fill(
        self,
        instrument_key: str,
        side: str,
        quantity: int,
        product: str = "D",
        tag: str = "delta_neutral",
        max_attempts: int = 3,
        wait_seconds: float = 10.0,
    ) -> FillResult:
        """Place a MARKET order. If the broker reports a partial fill, place
        further MARKET orders for the remainder until fully filled or
        `max_attempts` is reached."""
        target = int(quantity)
        total_filled = 0
        weighted_cost = 0.0
        last_oid: Optional[str] = None

        for attempt in range(1, max_attempts + 1):
            remaining = target - total_filled
            if remaining <= 0:
                break

            self.log.info("[%s] MARKET attempt %d/%d  side=%s qty=%d (filled %d/%d)",
                          instrument_key, attempt, max_attempts, side,
                          remaining, total_filled, target)
            try:
                resp = self.place_order(
                    instrument_key=instrument_key, side=side, quantity=remaining,
                    order_type="MARKET", price=0.0, product=product, tag=tag,
                )
            except Exception as e:
                self.log.error("[%s] MARKET place failed: %s", instrument_key, e)
                continue

            order_id = self._extract_order_id(resp)
            if not order_id:
                self.log.error("[%s] No order_id in MARKET response: %s",
                               instrument_key, resp)
                continue
            last_oid = order_id

            self._wait_for_terminal(order_id, timeout_seconds=wait_seconds,
                                    poll_interval=1.0)
            status, avg, fqty = self._fetch_summary(order_id)

            if fqty > 0:
                if avg is None:
                    self.log.warning("[%s] order %s filled %d but no avg_price",
                                     instrument_key, order_id, fqty)
                else:
                    weighted_cost += fqty * avg
                total_filled += fqty
                self.log.info("[%s] MARKET fill: order=%s qty=%d avg=%.2f (cumulative %d/%d)",
                              instrument_key, order_id, fqty, avg or 0.0,
                              total_filled, target)
                if status == "complete" and total_filled >= target:
                    break
            else:
                self.log.warning("[%s] MARKET order %s ended with status=%s, no fill.",
                                 instrument_key, order_id, status)

        if total_filled < target:
            self.log.error(
                "[%s] MARKET partially filled: %d/%d after %d attempts.",
                instrument_key, total_filled, target, max_attempts,
            )

        avg_out = (weighted_cost / total_filled) if total_filled > 0 else None
        return FillResult(last_oid, avg_out, total_filled)

    # Backward-compat alias
    place_and_fill = place_market_and_fill

    def place_limit_with_retry(
        self,
        instrument_key: str,
        side: str,
        quantity: int,
        max_attempts: int = 5,
        wait_seconds: int = 60,
        product: str = "D",
        tag: str = "delta_neutral",
        tick_size: float = 0.05,
        fallback_to_market: bool = True,
        market_fallback_attempts: int = 3,
    ) -> FillResult:
        """Open a position via LIMIT at the bid-ask mid; if not filled within
        `wait_seconds`, cancel and re-place with a refreshed mid for the
        REMAINING quantity (so partial fills are not lost). After `max_attempts`,
        fall back to MARKET for any unfilled remainder if `fallback_to_market`.

        Returns FillResult(order_id, weighted_avg_price, filled_qty).
        """
        target = int(quantity)
        total_filled = 0
        weighted_cost = 0.0
        last_oid: Optional[str] = None

        for attempt in range(1, max_attempts + 1):
            remaining = target - total_filled
            if remaining <= 0:
                break

            bid, ask = self.get_bid_ask(instrument_key)
            if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:
                self.log.warning(
                    "[%s] No usable bid/ask (bid=%s ask=%s); breaking out for MARKET fallback.",
                    instrument_key, bid, ask,
                )
                break

            limit_price = self._round_to_tick((bid + ask) / 2.0, tick_size)
            if side.upper() == "SELL":
                limit_price = max(limit_price, self._round_to_tick(bid, tick_size))
            else:
                limit_price = min(limit_price, self._round_to_tick(ask, tick_size))

            self.log.info(
                "[%s] LIMIT attempt %d/%d  side=%s qty=%d (filled %d/%d)  bid=%.2f ask=%.2f -> px=%.2f",
                instrument_key, attempt, max_attempts, side, remaining,
                total_filled, target, bid, ask, limit_price,
            )

            try:
                resp = self.place_order(
                    instrument_key=instrument_key, side=side, quantity=remaining,
                    order_type="LIMIT", product=product, price=limit_price, tag=tag,
                )
            except Exception as e:
                self.log.error("[%s] place_order(LIMIT) failed: %s", instrument_key, e)
                continue

            order_id = self._extract_order_id(resp)
            if not order_id:
                self.log.error("[%s] No order_id in LIMIT response: %s",
                               instrument_key, resp)
                continue
            last_oid = order_id

            status_at_timeout = self._wait_for_terminal(
                order_id, timeout_seconds=wait_seconds, poll_interval=2.0,
            )

            if status_at_timeout != "complete":
                # Cancel so we can re-price; partial fill (if any) is preserved
                # by the broker -- we'll capture it from the post-cancel summary.
                self.log.info("[%s] Status=%s after %ds; cancelling %s.",
                              instrument_key, status_at_timeout, wait_seconds, order_id)
                try:
                    self.cancel_order(order_id)
                except Exception as e:
                    self.log.warning("[%s] cancel_order failed: %s", instrument_key, e)
                _time.sleep(1.0)  # let cancel propagate

            # Fetch authoritative final state for THIS order
            final_status, final_avg, final_fqty = self._fetch_summary(order_id)

            if final_fqty > 0:
                if final_avg is None:
                    self.log.warning(
                        "[%s] order %s filled %d but no avg_price reported",
                        instrument_key, order_id, final_fqty,
                    )
                else:
                    weighted_cost += final_fqty * final_avg
                total_filled += final_fqty
                self.log.info(
                    "[%s] LIMIT fill: order=%s qty=%d avg=%.2f (cumulative %d/%d, status=%s)",
                    instrument_key, order_id, final_fqty, final_avg or 0.0,
                    total_filled, target, final_status,
                )

            if total_filled >= target:
                break

            if final_status == "rejected":
                self.log.warning("[%s] order %s rejected; retrying with fresh price.",
                                 instrument_key, order_id)
                continue

        # Fallback to MARKET for any remainder
        remaining = target - total_filled
        if remaining > 0 and fallback_to_market:
            self.log.warning(
                "[%s] LIMIT retries exhausted; MARKET fallback for remaining %d/%d.",
                instrument_key, remaining, target,
            )
            mres = self.place_market_and_fill(
                instrument_key=instrument_key, side=side, quantity=remaining,
                product=product, tag=tag, max_attempts=market_fallback_attempts,
            )
            if mres.order_id:
                last_oid = mres.order_id
            if mres.filled_qty > 0 and mres.avg_price is not None:
                weighted_cost += mres.filled_qty * mres.avg_price
                total_filled += mres.filled_qty

        if total_filled < target:
            self.log.error(
                "[%s] PARTIAL FILL ONLY: %d/%d after all attempts (LIMIT + MARKET fallback).",
                instrument_key, total_filled, target,
            )

        avg_out = (weighted_cost / total_filled) if total_filled > 0 else None
        return FillResult(last_oid, avg_out, total_filled)

    # =========================================================
    # positions
    # =========================================================
    def get_positions(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "positions")
        return data.get("data") or []
