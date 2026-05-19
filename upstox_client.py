"""Upstox API wrapper -- mixed v2 / v3 per requirement.

Endpoint -> version mapping (driven by config.upstox.endpoints):
  GET  /v2/option/chain                         option chain + greeks
  GET  /v2/market-quote/quotes                  full quote
  GET  /v2/market-quote/ltp                     compact LTP (spot, VIX)
  POST /v3/order/place                          place order
  GET  /v2/order/details                        order status + average price
  GET  /v2/portfolio/short-term-positions       positions

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

    # ---------- internal: route to v2 or v3 ----------
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

    # ---------- quotes (v2) ----------
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

    def get_india_vix(self) -> float:
        ik = self.cfg["vix_instrument_key"]
        ltp = self.get_ltp([ik])
        return ltp[ik] if ik in ltp else float(next(iter(ltp.values())))

    def get_underlying_spot(self) -> float:
        ik = self.cfg["underlying_instrument_key"]
        ltp = self.get_ltp([ik])
        return ltp[ik] if ik in ltp else float(next(iter(ltp.values())))

    # ---------- option chain (v2) ----------
    def get_option_chain(self, expiry_date: str) -> List[Dict[str, Any]]:
        """expiry_date in 'YYYY-MM-DD'. Returns the raw rows array."""
        params = {
            "instrument_key": self.cfg["underlying_instrument_key"],
            "expiry_date": expiry_date,
        }
        data = self._request("GET", "option_chain", params=params)
        return data.get("data") or []

    # ---------- orders ----------
    def place_order(
        self,
        instrument_key: str,
        side: str,                # "BUY" or "SELL"
        quantity: int,
        order_type: str = "MARKET",
        product: str = "D",
        price: float = 0.0,
        tag: str = "delta_neutral",
    ) -> Dict[str, Any]:
        """Place via Upstox v3."""
        body = {
            "quantity": int(quantity),
            "product": product,
            "validity": "DAY",
            "price": float(price),
            "tag": tag,
            "instrument_token": instrument_key,
            "order_type": order_type,
            "transaction_type": side.upper(),
            "disclosed_quantity": 0,
            "trigger_price": 0.0,
            "is_amo": False,
            "slice": False,
        }
        self.log.info("PLACE [v3] %s %s qty=%d %s", side, instrument_key, quantity, order_type)
        return self._request("POST", "place_order", json=body)

    def get_order_details(self, order_id: str) -> Dict[str, Any]:
        """Order status + average_price via v2."""
        params = {"order_id": order_id}
        data = self._request("GET", "order_details", params=params)
        return data.get("data") or {}

    def get_avg_fill_price(self, order_id: str) -> Optional[float]:
        """Poll order details until status=='complete' (or attempts exhausted).
        Returns the fill `average_price`, else None."""
        for i in range(self._fill_attempts):
            try:
                od = self.get_order_details(order_id)
                status = (od.get("status") or od.get("order_status") or "").lower()
                avg = od.get("average_price")
                if status == "complete" and avg is not None:
                    return float(avg)
                if status in ("rejected", "cancelled"):
                    self.log.error("Order %s ended in %s; details=%s", order_id, status, od)
                    return None
            except Exception as e:
                self.log.warning("get_order_details(%s) attempt %d failed: %s", order_id, i + 1, e)
            _time.sleep(self._fill_delay)
        self.log.warning("Order %s not filled after %d polls.", order_id, self._fill_attempts)
        return None

    def place_and_fill(self, **kwargs) -> Tuple[str, Optional[float]]:
        """Place an order (v3) then resolve average fill price (v2).
        Returns (order_id, average_price_or_None)."""
        resp = self.place_order(**kwargs)
        data = resp.get("data") or {}
        order_id = (
            data.get("order_id")
            or (data.get("order_ids") or [None])[0]
            or resp.get("order_id")
        )
        avg = self.get_avg_fill_price(order_id) if order_id else None
        return order_id, avg

    def get_positions(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "positions")
        return data.get("data") or []
