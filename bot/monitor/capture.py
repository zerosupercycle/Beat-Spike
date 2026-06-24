"""Capture ±window price charts via dashboard server."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from bot.config import FEED_IDS
from bot.monitor.store import MonitorEventRecord, monitor_chart_id
from bot.pm.slug import parse_market_slug

log = logging.getLogger(__name__)


def _parse_trade(trade: dict[str, Any]) -> dict[str, Any] | None:
    slug = str(trade.get("slug") or "").strip().lower()
    parsed = parse_market_slug(slug)
    if not parsed:
        return None
    asset, interval, _epoch = parsed
    side = str(trade.get("side") or "").upper()
    if side != "BUY":
        return None
    try:
        ts = float(trade.get("timestamp") or 0)
        price = float(trade.get("price") or 0)
        size = float(trade.get("size") or 0)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    tx = str(trade.get("transactionHash") or trade.get("transaction_hash") or "").lower()
    if not tx:
        return None
    return {
        "slug": slug,
        "asset": asset,
        "interval": interval,
        "side": side,
        "timestamp": ts,
        "price": price,
        "size": size,
        "usdc_size": float(trade.get("usdcSize") or trade.get("usdc_size") or price * size),
        "outcome": str(trade.get("outcome") or ""),
        "transaction_hash": tx,
        "proxy_wallet": str(trade.get("proxyWallet") or trade.get("proxy_wallet") or "").lower(),
    }


async def capture_monitor_chart(
    *,
    server_url: str,
    parsed: dict[str, Any],
    before_sec: float,
    after_sec: float,
    chart_id: str,
) -> dict[str, Any]:
    body = {
        "asset": parsed["asset"],
        "enabled_feeds": sorted(FEED_IDS),
        "slug": parsed["slug"],
        "interval": parsed["interval"],
        "order_ts": parsed["timestamp"],
        "window_before_sec": before_sec,
        "window_after_sec": after_sec,
        "chart_id": chart_id,
        "source": "monitor",
        "snapshot_dir": "monitor",
    }
    url = f"{server_url.rstrip('/')}/api/feeds/capture-chart"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, json=body)
        r.raise_for_status()
        return r.json()


async def finalize_monitor_chart(
    *,
    server_url: str,
    chart_id: str,
) -> dict[str, Any]:
    url = f"{server_url.rstrip('/')}/api/monitor/trade-chart/{chart_id}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.json()


def build_event_record(
    parsed: dict[str, Any],
    *,
    target_label: str,
    target_url: str,
    chart_id: str,
) -> MonitorEventRecord:
    from datetime import datetime, timezone

    ts_iso = datetime.fromtimestamp(parsed["timestamp"], tz=timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    return MonitorEventRecord(
        ts=ts_iso,
        target=target_label,
        target_url=target_url,
        proxy_wallet=parsed["proxy_wallet"],
        slug=parsed["slug"],
        asset=parsed["asset"].upper(),
        interval=parsed["interval"],
        side=parsed["side"].lower(),
        price=parsed["price"],
        size=parsed["size"],
        usdc_size=parsed["usdc_size"],
        outcome=parsed["outcome"],
        transaction_hash=parsed["transaction_hash"],
        chart_id=chart_id,
    )


async def schedule_finalize(
    *,
    server_url: str,
    chart_id: str,
    finalize_at: float,
    stop: asyncio.Event,
) -> None:
    import time

    delay = max(0.0, finalize_at - time.time())
    while delay > 0 and not stop.is_set():
        wait = min(delay, 5.0)
        await asyncio.sleep(wait)
        delay = finalize_at - time.time()
    if stop.is_set():
        return
    try:
        await finalize_monitor_chart(server_url=server_url, chart_id=chart_id)
        log.info("Finalized monitor chart %s", chart_id)
    except Exception as exc:
        log.warning("Finalize monitor chart %s failed: %s", chart_id, exc)
