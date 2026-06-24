from __future__ import annotations

import asyncio
import json
from typing import Any

from bot.feeds.base import PriceFeed

try:
    import websockets
except ImportError:
    websockets = None  # type: ignore

_BASE_TO_SYMBOL = {"BTC": "btcusdt", "ETH": "ethusdt", "SOL": "solusdt", "XRP": "xrpusdt"}
_SYMBOL_TO_BASE = {v: k for k, v in _BASE_TO_SYMBOL.items()}


def _stream_url() -> str:
    streams = "/".join(f"{sym}@miniTicker" for sym in _BASE_TO_SYMBOL.values())
    return f"wss://stream.binance.com:9443/stream?streams={streams}"


class BinanceFeed(PriceFeed):
    id = "binance"
    label = "Binance"
    endpoint = _stream_url()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._last_quote_vol: dict[str, float] = {}

    async def run(self, stop: asyncio.Event) -> None:
        if websockets is None:
            self._set_health("error", summary="websockets package not installed", last_error="ImportError")
            return

        url = self.endpoint
        backoff = 1.0

        while not stop.is_set():
            self._set_health("connecting", summary="Binance: connecting combined miniTicker")
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    self._set_health(
                        "connected",
                        summary="Binance USDT miniTicker (BTC, ETH, SOL, XRP)",
                        connected=True,
                    )
                    backoff = 1.0
                    while not stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                        except asyncio.TimeoutError:
                            continue
                        outer = json.loads(raw)
                        data = outer.get("data") if isinstance(outer, dict) else None
                        if not isinstance(data, dict):
                            continue
                        sym = str(data.get("s") or "").lower()
                        base = _SYMBOL_TO_BASE.get(sym)
                        if not base:
                            continue
                        close = data.get("c")
                        if close is None:
                            continue
                        try:
                            price = float(close)
                            quote_q = float(data.get("q") or 0.0)
                        except (TypeError, ValueError):
                            continue
                        prev_q = self._last_quote_vol.get(base)
                        vol_delta = 0.0
                        if prev_q is not None and quote_q >= prev_q:
                            vol_delta = quote_q - prev_q
                        self._last_quote_vol[base] = quote_q
                        self.handle_price(base, price, volume_delta=vol_delta)
                        self._assets[base]["quote_volume"] = quote_q
            except Exception as e:
                if stop.is_set():
                    break
                self._set_health(
                    "reconnecting",
                    summary=f"Binance disconnected — {e!s}",
                    last_error=str(e),
                    connected=False,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 30.0)
