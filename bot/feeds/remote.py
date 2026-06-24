"""Consume live feed snapshots from the dashboard server (avoids duplicate WS connections)."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlparse

import httpx

try:
    import websockets
except ImportError:
    websockets = None  # type: ignore


def _http_to_ws_url(http_url: str) -> str:
    p = urlparse(http_url.strip())
    scheme = "wss" if p.scheme == "https" else "ws"
    host = p.netloc or p.path
    return f"{scheme}://{host}/ws"


class RemoteFeedClient:
    """Mirror FeedAggregator surface using dashboard /api/snapshot + /ws."""

    def __init__(self, server_url: str = "http://127.0.0.1:8788") -> None:
        self._http_url = server_url.rstrip("/")
        self._ws_url = _http_to_ws_url(self._http_url)
        self._snap: dict[str, Any] = {}
        self._stop = asyncio.Event()
        self._ws_task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None

    def snapshot(self) -> dict[str, Any]:
        return dict(self._snap) if self._snap else {"feeds": {}, "version": 0}

    def reset_epoch(self) -> None:
        return None

    def snap_slug_feed_beats(
        self,
        asset: str,
        slug: str,
        epoch_start_ts: float,
        *,
        interval: str | None = None,
        allow_live: bool = False,
        live_snap: bool = False,
    ) -> dict[str, float | None]:
        del asset, slug, epoch_start_ts, interval, allow_live, live_snap
        return {}

    async def snap_slug_feed_beats_async(
        self,
        asset: str,
        slug: str,
        epoch_start_ts: float,
        *,
        interval: str | None = None,
        allow_live: bool = False,
        live_snap: bool = False,
    ) -> dict[str, float | None]:
        if live_snap:
            allow_live = True
        body = {
            "asset": asset,
            "slug": slug,
            "epoch_start": epoch_start_ts,
            "allow_live": allow_live,
        }
        if interval:
            body["interval"] = interval
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(f"{self._http_url}/api/feeds/snap-beats", json=body)
                r.raise_for_status()
                data = r.json()
                fb = data.get("feed_beats")
                return fb if isinstance(fb, dict) else {}
        except Exception as e:
            print(f"  [FEEDS] snap-beats failed ({self._http_url}): {e}")
            return {}

    async def start(self, assets: list[str]) -> None:
        del assets  # server owns feed asset list
        self._stop.clear()
        await self._fetch_once()
        self._ws_task = asyncio.create_task(self._ws_loop())
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._stop.set()
        for task in (self._ws_task, self._poll_task):
            if task:
                task.cancel()
        await asyncio.gather(
            *(t for t in (self._ws_task, self._poll_task) if t),
            return_exceptions=True,
        )
        self._ws_task = None
        self._poll_task = None

    async def _fetch_once(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self._http_url}/api/snapshot")
                r.raise_for_status()
                self._snap = r.json()
        except Exception as e:
            if not self._snap:
                print(f"  [FEEDS] dashboard snapshot failed ({self._http_url}): {e}")

    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.sleep(2.0)
                await self._fetch_once()
            except asyncio.CancelledError:
                break

    async def _ws_loop(self) -> None:
        if websockets is None:
            print("  [FEEDS] websockets not installed — using HTTP poll only")
            return
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(self._ws_url, ping_interval=20, ping_timeout=20) as ws:
                    backoff = 1.0
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            self._snap = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._stop.is_set():
                    break
                print(f"  [FEEDS] dashboard WS reconnect ({e!s})")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 15.0)

    async def wait_for_prices(
        self,
        asset: str,
        feed_ids: list[str],
        *,
        timeout_sec: float = 30.0,
    ) -> bool:
        """Block until required feeds have a price for asset (or timeout)."""
        asset_u = asset.upper()
        deadline = asyncio.get_event_loop().time() + timeout_sec
        while asyncio.get_event_loop().time() < deadline:
            snap = self.snapshot()
            feeds = snap.get("feeds") or {}
            if all(
                (feeds.get(fid, {}).get("assets") or {}).get(asset_u, {}).get("price") is not None
                for fid in feed_ids
            ):
                return True
            await asyncio.sleep(0.1)
        return False

    async def capture_trade_chart(
        self,
        asset: str,
        enabled_feeds: list[str],
        *,
        slug: str | None = None,
        interval: str | None = None,
        epoch_start: int | None = None,
        epoch_end: int | None = None,
        order_ts: float | None = None,
        chart_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "asset": asset,
            "enabled_feeds": enabled_feeds,
        }
        if slug:
            body["slug"] = slug
        if interval:
            body["interval"] = interval
        if epoch_start is not None:
            body["epoch_start"] = epoch_start
        if epoch_end is not None:
            body["epoch_end"] = epoch_end
        if order_ts is not None:
            body["order_ts"] = order_ts
        if chart_id:
            body["chart_id"] = chart_id
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(f"{self._http_url}/api/feeds/capture-chart", json=body)
                r.raise_for_status()
                return r.json()
        except Exception as e:
            print(f"  [FEEDS] capture-chart failed ({self._http_url}): {e}")
            return {"series": {}, "error": str(e)}
