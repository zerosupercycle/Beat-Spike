from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import Any, Callable

from bot.config import ChainlinkConfig
from bot.constants import ASSETS
from bot.feeds.base import AssetTick, PriceFeed
from bot.feeds.chainlink_streams import CHAINLINK_REST_URL, fetch_latest_prices

try:
    import websockets
except ImportError:
    websockets = None  # type: ignore

RTDS_URL = "wss://ws-live-data.polymarket.com"
PING_INTERVAL_SEC = 5.0
IDLE_RECONNECT_SEC = 120.0
STALE_DATA_SEC = 90.0

ASSET_TO_SYMBOL = {
    "btc": "btc/usd",
    "eth": "eth/usd",
    "sol": "sol/usd",
    "xrp": "xrp/usd",
}
SYMBOL_TO_ASSET = {v: k.upper() for k, v in ASSET_TO_SYMBOL.items()}
TRACKED_SYMBOLS = frozenset(ASSET_TO_SYMBOL.values())

# Polymarket docs show type "*" only, but RTDS also needs an "update" sub to stream live
# Chainlink ticks in practice. Per-symbol "update" filters misroute to crypto_prices (Binance).
CHAINLINK_SUBSCRIPTIONS = [
    {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""},
    {"topic": "crypto_prices_chainlink", "type": "update", "filters": ""},
]


class ChainlinkFeed(PriceFeed):
    id = "chainlink"
    label = "Chainlink"

    def __init__(
        self,
        *,
        on_tick: Callable[[str, AssetTick], None],
        momentum_cfg: dict[str, Any] | None = None,
        rtds_url: str = RTDS_URL,
        streams_cfg: ChainlinkConfig | None = None,
    ) -> None:
        self._rtds_url = rtds_url.rstrip("/")
        self._streams_cfg = streams_cfg or ChainlinkConfig()
        self._use_streams = self._streams_cfg.ready()
        super().__init__(on_tick=on_tick, momentum_cfg=momentum_cfg)
        self._last_data_mono = 0.0
        self._last_tick_ms: dict[str, int] = {}
        if self._use_streams:
            self.health.transport = "rest"
            self.health.endpoint = CHAINLINK_REST_URL
        else:
            self.health.transport = "websocket"
            self.health.endpoint = self._rtds_url

    @property
    def endpoint(self) -> str:
        return CHAINLINK_REST_URL if self._use_streams else self._rtds_url

    def _active_feed_ids(self) -> dict[str, str]:
        tracked = {a.lower() for a in ASSETS}
        return {
            asset.lower().strip(): feed_id
            for asset, feed_id in self._streams_cfg.feed_ids.items()
            if asset.lower().strip() in tracked and feed_id.strip()
        }

    async def run(self, stop: asyncio.Event) -> None:
        if self._use_streams:
            await self._run_streams_poll(stop)
            return
        await self._run_rtds(stop)

    async def _run_streams_poll(self, stop: asyncio.Event) -> None:
        feed_ids = self._active_feed_ids()
        if not feed_ids:
            self._set_health(
                "error",
                summary="Chainlink Data Streams: no feed_ids for tracked assets",
                last_error="missing feed_ids",
            )
            return

        interval = max(0.5, float(self._streams_cfg.latest_poll_sec))
        assets_label = ", ".join(sorted(feed_ids))
        self._set_health(
            "connecting",
            summary=f"Chainlink Data Streams: polling {assets_label} every {interval:.1f}s",
        )

        while not stop.is_set():
            try:
                prices = await fetch_latest_prices(
                    self._streams_cfg.streams_user_id,
                    self._streams_cfg.streams_secret,
                    feed_ids,
                )
                if prices:
                    self._last_data_mono = time.monotonic()
                    for asset, px in prices.items():
                        base = asset.upper()
                        if base in ASSETS and px > 0:
                            self.handle_price(base, float(px))
                    self._set_health(
                        "connected",
                        summary=f"Chainlink Data Streams: {assets_label}",
                        connected=True,
                    )
                elif self.health.state != "connected":
                    self._set_health(
                        "reconnecting",
                        summary="Chainlink Data Streams: poll returned no prices",
                        last_error="empty poll",
                        connected=False,
                    )
            except Exception as e:
                if stop.is_set():
                    break
                self._set_health(
                    "reconnecting",
                    summary=f"Chainlink Data Streams poll failed — {e!s}",
                    last_error=str(e),
                    connected=False,
                )

            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break

    async def _run_rtds(self, stop: asyncio.Event) -> None:
        if websockets is None:
            self._set_health("error", summary="websockets package not installed", last_error="ImportError")
            return

        backoff = 1.0

        while not stop.is_set():
            self._set_health("connecting", summary=f"Chainlink RTDS: connecting to {self._rtds_url}")
            try:
                async with websockets.connect(
                    self._rtds_url,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    await ws.send(json.dumps({"action": "subscribe", "subscriptions": CHAINLINK_SUBSCRIPTIONS}))
                    self._set_health(
                        "connected",
                        summary=f"Chainlink RTDS: {', '.join(sorted(TRACKED_SYMBOLS))}",
                        connected=True,
                    )
                    backoff = 1.0
                    connected_mono = time.monotonic()
                    self._last_data_mono = connected_mono

                    async def ping_loop() -> None:
                        while not stop.is_set():
                            await asyncio.sleep(PING_INTERVAL_SEC)
                            try:
                                await ws.send("PING")
                            except Exception:
                                break

                    ping_task = asyncio.create_task(ping_loop())
                    try:
                        while not stop.is_set():
                            since_data = time.monotonic() - self._last_data_mono
                            if since_data > IDLE_RECONNECT_SEC:
                                raise TimeoutError("Chainlink RTDS idle — reconnecting")
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
                            except asyncio.TimeoutError:
                                continue
                            self._ingest(raw)
                    finally:
                        ping_task.cancel()
                        await asyncio.gather(ping_task, return_exceptions=True)
            except Exception as e:
                if stop.is_set():
                    break
                self._set_health(
                    "reconnecting",
                    summary=f"Chainlink disconnected — {e!s}",
                    last_error=str(e),
                    connected=False,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 30.0)

    def _ingest(self, raw: str) -> None:
        if not raw or raw == "PONG":
            return
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        if str(msg.get("topic") or "") != "crypto_prices_chainlink":
            # Ignore crypto_prices (Binance) frames — misrouted when using per-symbol filters.
            return

        payload = msg.get("payload")
        if not isinstance(payload, dict):
            return

        msg_type = str(msg.get("type") or "")

        if msg_type == "update" and payload.get("value") is not None:
            self._push_tick(payload.get("symbol"), payload.get("timestamp"), payload.get("value"))
            return

        data = payload.get("data")
        if not isinstance(data, list):
            return

        rows = [row for row in data if isinstance(row, dict)]
        if not rows:
            return

        symbol = payload.get("symbol")
        if msg_type == "subscribe":
            row = rows[-1]
            self._push_tick(symbol or row.get("symbol"), row.get("timestamp"), row.get("value"))
            return

        for row in rows:
            self._push_tick(symbol or row.get("symbol"), row.get("timestamp"), row.get("value"))

    def _push_tick(self, symbol: Any, ts_ms: Any, value: Any) -> None:
        if not symbol or value is None:
            return
        sym = str(symbol).lower()
        if sym not in TRACKED_SYMBOLS:
            return
        base = SYMBOL_TO_ASSET.get(sym)
        if not base:
            return
        try:
            px = float(value)
        except (TypeError, ValueError):
            return
        if px <= 0:
            return

        if ts_ms is not None:
            try:
                ts_i = int(ts_ms)
            except (TypeError, ValueError):
                ts_i = None
            if ts_i is not None:
                prev = self._last_tick_ms.get(sym)
                if prev is not None and ts_i < prev:
                    return
                self._last_tick_ms[sym] = ts_i

        self._last_data_mono = time.monotonic()
        self.handle_price(base, px)

    def snapshot(self) -> dict[str, Any]:
        snap = super().snapshot()
        now = datetime.now(tz=UTC)
        stale_assets = 0
        stale_limit = STALE_DATA_SEC if not self._use_streams else max(
            STALE_DATA_SEC,
            float(self._streams_cfg.latest_poll_sec) * 3,
        )
        for row in snap.get("assets", {}).values():
            received = row.get("received_at")
            if not received:
                stale_assets += 1
                continue
            try:
                dt = datetime.fromisoformat(str(received).replace("Z", "+00:00"))
                age = (now - dt).total_seconds()
            except ValueError:
                stale_assets += 1
                continue
            if age > stale_limit:
                stale_assets += 1
        if stale_assets > 0 and self.health.state == "connected":
            source = "Data Streams" if self._use_streams else "RTDS"
            snap["health"] = dict(snap["health"])
            snap["health"]["data_stale"] = True
            snap["health"]["summary"] = (
                f"Chainlink {source} stale ({stale_assets}/{len(ASSETS)} assets quiet >{stale_limit:.0f}s)"
            )
        return snap

    def price_history(self, base: str, lookback_sec: float) -> list[tuple[float, float]]:
        """Return (monotonic, price) samples from momentum buffer."""
        buf = self._buffers._buffers.get(base.upper())  # noqa: SLF001
        if not buf:
            return []
        now = time.monotonic()
        cutoff = now - lookback_sec
        return [(t, p) for t, p in buf if t >= cutoff]
