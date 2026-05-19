"""Upstox API wrapper -- mixed v2 / v3 per requirement.

Endpoint -> version mapping (driven by config.upstox.endpoints):
  GET    /v2/option/chain                       option chain + greeks
  GET    /v2/market-quote/quotes                full quote (incl. depth/bid/ask)
  GET    /v2/market-quote/ltp                   compact LTP (spot, VIX)
  POST   /v3/order/place                        place order
  DELETE /v3/order/cancel                       cancel a working order
  GET    /v2/order/details                      order status + average_price
  GET    /v2/portfolio/short-term-positions     positions

Docs: https://upstox.com/developer/api-documentation/
"""
from __future__ import annotations

import os
import time as _time
from typing import Any, Dict, List, Optional, Tuple

import requests


class UpstoxError(RuntimeError):
    pass


class UpstoxClient:
    def __init__(self, cfg: Dict[str, Any], logger):
        self.cfg = cfg
        self.log = logger
        ux = cfg["upstox"]
        self.base_v2 = ux["base_url_v2"].rstrip("/")
        self.base_v3 = ux["base_url_v3"].rstrip("/")
        self.endpoints = ux["endpoints"]
        token = os.environ.get(ux["access_token_env"])
        if not token:
            raise UpstoxError(
                f"Set Upstox access token in env var {ux['access_token_env']}"
            )
        self._headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
        self._sess = requests.Session()
        self._fill_attempts = int(ux.get("fill_poll_attempts", 5))
        self._fill_delay = float(ux.get("fill_poll_delay_seconds", 1.0))

    # =========================================================
    # internal: route to v2 or v3
    # =========================================================
    def _base_for(self, ep_key: str) -> str:
        v = self.endpoints[ep_key]["version"]
        return self.base_v3 if v == "v3" else self.base_v2

    def _path_for(self, ep_key: str) -> str:
        return self.endpoints[ep_key]["path"]

    def _request(self, method: str, ep_key: str, **kwargs) -> Dict[str, Any]:
        url = f"{self._base_for(ep_key)}{self._path_for(ep_key)}"
        for attempt in range(3):
            try:
                r = self._sess.request(method, url, headers=self._headers, timeout=15, **kwargs)
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
        # Server may key the response by instrument_key or by token; just take first.
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
        side: str,                 # "BUY" or "SELL"
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
    # orders -- high level (used by strategy)
    # =========================================================
    def _wait_for_fill(self, order_id: str, timeout_seconds: float,
                       poll_interval: float = 2.0) -> Tuple[str, Optional[float]]:
        """Poll order details until terminal status or timeout.
        Returns (status, average_price_or_None). Status is one of:
            'complete', 'rejected', 'cancelled', 'pending'.
        """
        deadline = _time.time() + float(timeout_seconds)
        while _time.time() < deadline:
            try:
                od = self.get_order_details(order_id)
                status = (od.get("status") or od.get("order_status") or "").lower()
                avg = od.get("average_price")
                if status == "complete":
                    return ("complete", float(avg) if avg is not None else None)
                if status in ("rejected", "cancelled"):
                    return (status, None)
            except Exception as e:
                self.log.warning("get_order_details(%s) failed: %s", order_id, e)
            _time.sleep(poll_interval)
        return ("pending", None)

    def place_market_and_fill(self, **kwargs) -> Tuple[Optional[str], Optional[float]]:
        """Place a MARKET order (v3) and resolve the average fill price (v2)."""
        kwargs["order_type"] = "MARKET"
        kwargs["price"] = 0.0
        resp = self.place_order(**kwargs)
        order_id = self._extract_order_id(resp)
        if not order_id:
            self.log.error("place_market_and_fill: no order_id in response %s", resp)
            return None, None
        status, avg = self._wait_for_fill(
            order_id,
            timeout_seconds=self._fill_attempts * self._fill_delay * 4,
            poll_interval=self._fill_delay,
        )
        if status != "complete":
            self.log.warning("MARKET order %s did not complete in time (status=%s)",
                             order_id, status)
        return order_id, avg

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
    ) -> Tuple[Optional[str], Optional[float]]:
        """Open a position via LIMIT at the bid-ask mid; if not filled within
        `wait_seconds`, cancel and re-place with a refreshed mid.

        After `max_attempts` unfilled tries, optionally fall back to a MARKET
        order so execution is guaranteed (`fallback_to_market`).

        Returns (order_id, average_fill_price).
        """
        last_order_id = None
        for attempt in range(1, max_attempts + 1):
            bid, ask = self.get_bid_ask(instrument_key)
            if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:
                self.log.warning("[%s] No usable bid/ask (bid=%s ask=%s); breaking to MARKET fallback.",
                                 instrument_key, bid, ask)
                break

            limit_price = self._round_to_tick((bid + ask) / 2.0, tick_size)
            # For SELL, ensure limit >= bid; for BUY, ensure limit <= ask
            if side.upper() == "SELL":
                limit_price = max(limit_price, self._round_to_tick(bid, tick_size))
            else:
                limit_price = min(limit_price, self._round_to_tick(ask, tick_size))

            self.log.info(
                "[%s] LIMIT attempt %d/%d  side=%s qty=%d  bid=%.2f ask=%.2f -> px=%.2f",
                instrument_key, attempt, max_attempts, side, quantity, bid, ask, limit_price,
            )

            try:
                resp = self.place_order(
                    instrument_key=instrument_key, side=side, quantity=quantity,
                    order_type="LIMIT", product=product, price=limit_price, tag=tag,
                )
            except Exception as e:
                self.log.error("[%s] place_order(LIMIT) failed: %s", instrument_key, e)
                continue

            order_id = self._extract_order_id(resp)
            if not order_id:
                self.log.error("[%s] No order_id in response: %s", instrument_key, resp)
                continue
            last_order_id = order_id

            status, avg = self._wait_for_fill(order_id, timeout_seconds=wait_seconds,
                                              poll_interval=2.0)
            if status == "complete":
                self.log.info("[%s] LIMIT filled: order_id=%s avg=%.2f",
                              instrument_key, order_id, avg if avg else -1)
                return order_id, avg

            # Not filled: cancel and retry with a fresh mid
            self.log.info("[%s] Status=%s after %ds; cancelling %s and retrying.",
                          instrument_key, status, wait_seconds, order_id)
            try:
                self.cancel_order(order_id)
            except Exception as e:
                self.log.warning("[%s] cancel_order failed: %s", instrument_key, e)

            # Race guard: order may have completed between the wait expiring and the cancel
            try:
                final = self.get_order_details(order_id)
                fstatus = (final.get("status") or final.get("order_status") or "").lower()
                if fstatus == "complete":
                    avg = final.get("average_price")
                    self.log.info("[%s] Race: order completed during cancel; avg=%s",
                                  instrument_key, avg)
                    return order_id, float(avg) if avg is not None else None
            except Exception:
                pass

        if fallback_to_market:
            self.log.warning(
                "[%s] LIMIT retries exhausted; falling back to MARKET for %s qty=%d.",
                instrument_key, side, quantity,
            )
            return self.place_market_and_fill(
                instrument_key=instrument_key, side=side, quantity=quantity,
                product=product, tag=tag,
            )
        return last_order_id, None

    # =========================================================
    # positions
    # =========================================================
    def get_positions(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "positions")
        return data.get("data") or []
