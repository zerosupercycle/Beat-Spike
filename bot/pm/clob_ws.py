from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

from bot.constants import WS_MSG_BOOK

MsgCallback = Callable[[dict[str, Any]], Awaitable[None]]

_WS_CONNECT_TIMEOUT_SEC = 20.0
_MARKET_WS_SUFFIX = "/ws/market"


def normalize_market_ws_url(ws_url: str) -> str:
    """Polymarket market channel requires the /ws/market path (base host 404s)."""
    base = ws_url.rstrip("/")
    if base.endswith(_MARKET_WS_SUFFIX):
        return base
    return f"{base}{_MARKET_WS_SUFFIX}"


class ClobWebSocket:
    def __init__(self, ws_url: str, *, on_book: MsgCallback | None = None) -> None:
        self._ws_url = normalize_market_ws_url(ws_url)
        self._on_book = on_book
        self._running = False
        self._ws: Any = None
        self._task: asyncio.Task | None = None

    async def connect(self, asset_ids: list[str]) -> None:
        self._running = True
        self._asset_ids = list(asset_ids)
        await self._connect()
        self._task = asyncio.create_task(self._listen())

    async def disconnect(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._ws:
            await self._ws.close()

    async def _connect(self) -> None:
        self._ws = await asyncio.wait_for(
            websockets.connect(self._ws_url, ping_interval=20, ping_timeout=20),
            timeout=_WS_CONNECT_TIMEOUT_SEC,
        )
        sub = {
            "assets_ids": self._asset_ids,
            "type": "market",
            "custom_feature_enabled": True,
        }
        await self._ws.send(json.dumps(sub))

    async def _listen(self) -> None:
        while self._running:
            try:
                raw = await self._ws.recv()
                msg = json.loads(raw)
                if isinstance(msg, list):
                    for item in msg:
                        await self._dispatch(item)
                else:
                    await self._dispatch(msg)
            except asyncio.CancelledError:
                break
            except Exception:
                if self._running:
                    print("  ws_reconnecting (CLOB orderbook)", flush=True)
                    await asyncio.sleep(1.0)
                    try:
                        await self._connect()
                        print("  ws_reconnected (CLOB orderbook)", flush=True)
                    except Exception:
                        await asyncio.sleep(2.0)

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        if msg.get("event_type") == WS_MSG_BOOK or msg.get("type") == WS_MSG_BOOK:
            if self._on_book:
                await self._on_book(msg)
