"""Upstox v2 API wrapper used by the strategy.

Endpoints used:
  GET  /v2/option/chain                         -> option chain with greeks
  GET  /v2/market-quote/quotes                  -> LTP / quote (incl. India VIX)
  GET  /v2/market-quote/ltp                     -> compact LTP
  POST /v2/order/place                          -> place order
  GET  /v2/portfolio/short-term-positions       -> positions

Docs: https://upstox.com/developer/api-documentation/
"""
from __future__ import annotations

import os
import time as _time
from typing import Any, Dict, List, Optional

import requests


class UpstoxError(RuntimeError):
    pass


class UpstoxClient:
    def __init__(self, cfg: Dict[str, Any], logger):
        self.cfg = cfg
        self.log = logger
        self.base_url = cfg["upstox"]["base_url"].rstrip("/")
        token = os.environ.get(cfg["upstox"]["access_token_env"])
        if not token:
            raise UpstoxError(
                f"Set Upstox access token in env var {cfg['upstox']['access_token_env']}"
            )
        self._headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
        self._sess = requests.Session()

    # ---------- core HTTP ----------
    def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
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
                self.log.warning("Upstox %s %s failed (attempt %d): %s", method, path, attempt + 1, e)
                if attempt == 2:
                    raise
                _time.sleep(1.0 * (attempt + 1))
        raise UpstoxError("unreachable")

    # ---------- quotes ----------
    def get_ltp(self, instrument_keys: List[str]) -> Dict[str, float]:
        params = {"instrument_key": ",".join(instrument_keys)}
        data = self._request("GET", "/market-quote/ltp", params=params)
        out: Dict[str, float] = {}
        for _k, v in (data.get("data") or {}).items():
            ik = v.get("instrument_token") or v.get("instrument_key") or _k
            out[ik] = float(v["last_price"])
        return out

    def get_quote(self, instrument_keys: List[str]) -> Dict[str, Dict[str, Any]]:
        params = {"instrument_key": ",".join(instrument_keys)}
        data = self._request("GET", "/market-quote/quotes", params=params)
        return data.get("data") or {}

    def get_india_vix(self) -> float:
        ik = self.cfg["vix_instrument_key"]
        ltp = self.get_ltp([ik])
        # API may key by token; fall back to first value
        if ik in ltp:
            return ltp[ik]
        return float(next(iter(ltp.values())))

    def get_underlying_spot(self) -> float:
        ik = self.cfg["underlying_instrument_key"]
        ltp = self.get_ltp([ik])
        if ik in ltp:
            return ltp[ik]
        return float(next(iter(ltp.values())))

    # ---------- option chain ----------
    def get_option_chain(self, expiry_date: str) -> List[Dict[str, Any]]:
        """expiry_date in 'YYYY-MM-DD'. Returns list of strike rows each with
        call_options + put_options dicts containing market_data and option_greeks."""
        params = {
            "instrument_key": self.cfg["underlying_instrument_key"],
            "expiry_date": expiry_date,
        }
        data = self._request("GET", "/option/chain", params=params)
        return data.get("data") or []

    # ---------- orders ----------
    def place_order(
        self,
        instrument_key: str,
        side: str,                # "BUY" or "SELL"
        quantity: int,
        order_type: str = "MARKET",
        product: str = "NRML",
        price: float = 0.0,
        tag: str = "delta_neutral",
    ) -> Dict[str, Any]:
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
        }
        self.log.info("PLACE %s %s qty=%d %s", side, instrument_key, quantity, order_type)
        return self._request("POST", "/order/place", json=body)

    def get_positions(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "/portfolio/short-term-positions")
        return data.get("data") or []
