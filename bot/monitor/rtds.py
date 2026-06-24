"""Polymarket RTDS activity stream — detect target wallet buys."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, Awaitable, Callable

import aiohttp

log = logging.getLogger(__name__)

TradeHandler = Callable[[dict[str, Any]], Awaitable[None] | None]

PING_INTERVAL_SEC = 5.0


def _compact_filters(filters: dict[str, Any]) -> str:
    return json.dumps(filters, separators=(",", ":"))


def _is_rate_limited(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "429" in msg or "too many requests" in msg


def _next_backoff(
    exc: BaseException,
    *,
    current: float,
    reconnect_min_sec: float,
    reconnect_max_sec: float,
    rate_limit_backoff_sec: float,
    consecutive_rate_limits: int,
) -> float:
    if _is_rate_limited(exc):
        floor = rate_limit_backoff_sec * min(consecutive_rate_limits, 4)
        return min(max(current, floor, reconnect_min_sec), reconnect_max_sec)
    return min(max(reconnect_min_sec, current * 1.5), reconnect_max_sec)


async def run_rtds_activity(
    *,
    rtds_url: str,
    on_trade: TradeHandler,
    stop: asyncio.Event,
    reconnect_min_sec: float = 5.0,
    reconnect_max_sec: float = 120.0,
    rate_limit_backoff_sec: float = 90.0,
) -> None:
    """Subscribe to RTDS activity/orders_matched and invoke on_trade for each message."""
    backoff = reconnect_min_sec
    consecutive_rate_limits = 0
    while not stop.is_set():
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    rtds_url,
                    heartbeat=None,
                    timeout=aiohttp.ClientWSTimeout(ws_close=30.0),
                ) as ws:
                    sub = {
                        "action": "subscribe",
                        "subscriptions": [
                            {
                                "topic": "activity",
                                "type": "orders_matched",
                            }
                        ],
                    }
                    await ws.send_str(json.dumps(sub))
                    log.info("RTDS connected — subscribed to activity/orders_matched")
                    backoff = reconnect_min_sec
                    consecutive_rate_limits = 0

                    async def ping_loop() -> None:
                        while not stop.is_set():
                            await asyncio.sleep(PING_INTERVAL_SEC)
                            if ws.closed:
                                break
                            try:
                                await ws.send_str("PING")
                            except Exception:
                                break

                    ping_task = asyncio.create_task(ping_loop())
                    try:
                        async for msg in ws:
                            if stop.is_set():
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                raw = msg.data
                                if raw == "PONG":
                                    continue
                                try:
                                    data = json.loads(raw)
                                except json.JSONDecodeError:
                                    continue
                                payload = _extract_trade_payload(data)
                                if payload:
                                    result = on_trade(payload)
                                    if asyncio.iscoroutine(result):
                                        await result
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                break
                    finally:
                        ping_task.cancel()
                        await asyncio.gather(ping_task, return_exceptions=True)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            if _is_rate_limited(exc):
                consecutive_rate_limits += 1
            else:
                consecutive_rate_limits = 0
            backoff = _next_backoff(
                exc,
                current=backoff,
                reconnect_min_sec=reconnect_min_sec,
                reconnect_max_sec=reconnect_max_sec,
                rate_limit_backoff_sec=rate_limit_backoff_sec,
                consecutive_rate_limits=consecutive_rate_limits,
            )
            jitter = random.uniform(0.0, min(10.0, backoff * 0.1))
            wait = backoff + jitter
            if _is_rate_limited(exc):
                log.warning(
                    "RTDS rate-limited (429) — retry in %.0fs (Data API poll still active)",
                    wait,
                )
            else:
                log.warning("RTDS disconnected: %s — retry in %.0fs", exc, wait)
            await asyncio.sleep(wait)


def _extract_trade_payload(data: dict[str, Any]) -> dict[str, Any] | None:
    if data.get("topic") != "activity":
        return None
    payload = data.get("payload")
    if isinstance(payload, dict):
        return payload
    return None
