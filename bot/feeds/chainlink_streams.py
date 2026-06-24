"""Chainlink Data Streams REST — HMAC auth and V3 benchmark decode."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from typing import Any

import aiohttp
import httpx

log = logging.getLogger(__name__)

CHAINLINK_REST_URL = "https://api.dataengine.chain.link"
CHAINLINK_PRICE_DECIMALS = 1e18


def _generate_auth_headers(
    method: str,
    path: str,
    body: bytes,
    user_id: str,
    secret: str,
) -> dict[str, str]:
    ts = str(int(time.time() * 1000))
    body_hash = hashlib.sha256(body).hexdigest()
    sig_data = f"{method} {path} {body_hash} {user_id} {ts}"
    signature = hmac.new(secret.encode(), sig_data.encode(), hashlib.sha256).hexdigest()
    return {
        "Authorization": user_id,
        "X-Authorization-Timestamp": ts,
        "X-Authorization-Signature-SHA256": signature,
    }


def _decode_v3_benchmark_price(report_hex: str) -> float | None:
    try:
        raw = bytes.fromhex(report_hex.removeprefix("0x"))
    except ValueError:
        return None

    if len(raw) < 224:
        return None

    try:
        blob_offset = int.from_bytes(raw[96:128], "big")
        blob_len = int.from_bytes(raw[blob_offset : blob_offset + 32], "big")
        blob = raw[blob_offset + 32 : blob_offset + 32 + blob_len]
    except Exception:
        return None

    if len(blob) < 224:
        return None

    bp_int = int.from_bytes(blob[192:224], "big", signed=True)
    return bp_int / CHAINLINK_PRICE_DECIMALS


def _extract_full_report(data: Any) -> str:
    if isinstance(data, dict):
        report = data.get("report")
        if isinstance(report, dict):
            full = report.get("fullReport") or report.get("full_report")
            if full:
                return str(full)
        full = data.get("fullReport") or data.get("full_report")
        if full:
            return str(full)
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            return _extract_full_report(first)
    return ""


async def _fetch_report(
    session: aiohttp.ClientSession,
    *,
    user_id: str,
    secret: str,
    path: str,
) -> Any | None:
    url = f"{CHAINLINK_REST_URL}{path}"
    headers = _generate_auth_headers("GET", path, b"", user_id, secret)
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                log.warning("Chainlink REST %s: HTTP %s", path, resp.status)
                return None
            return await resp.json()
    except Exception as e:
        log.warning("Chainlink REST %s failed: %s", path, e)
        return None


def _fetch_report_sync(
    *,
    user_id: str,
    secret: str,
    path: str,
) -> Any | None:
    url = f"{CHAINLINK_REST_URL}{path}"
    headers = _generate_auth_headers("GET", path, b"", user_id, secret)
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code != 200:
                log.warning("Chainlink REST %s: HTTP %s", path, resp.status_code)
                return None
            return resp.json()
    except Exception as e:
        log.warning("Chainlink REST %s failed: %s", path, e)
        return None


def _strike_from_report_path(
    *,
    user_id: str,
    secret: str,
    path: str,
) -> float | None:
    data = _fetch_report_sync(user_id=user_id, secret=secret, path=path)
    full_report_hex = _extract_full_report(data)
    if not full_report_hex:
        return None
    price = _decode_v3_benchmark_price(full_report_hex)
    if price is not None and price > 0:
        return float(price)
    return None


async def _strike_from_report_path_async(
    session: aiohttp.ClientSession,
    *,
    user_id: str,
    secret: str,
    path: str,
) -> float | None:
    data = await _fetch_report(session, user_id=user_id, secret=secret, path=path)
    full_report_hex = _extract_full_report(data)
    if not full_report_hex:
        return None
    price = _decode_v3_benchmark_price(full_report_hex)
    if price is not None and price > 0:
        return float(price)
    return None


async def fetch_latest_prices(
    user_id: str,
    secret: str,
    feed_ids: dict[str, str],
) -> dict[str, float]:
    """Latest benchmark prices keyed by asset (e.g. btc)."""
    if not feed_ids or not user_id or not secret:
        return {}

    result: dict[str, float] = {}
    async with aiohttp.ClientSession() as session:
        for asset, hex_id in feed_ids.items():
            path = f"/api/v1/reports/latest?feedID={hex_id}"
            data = await _fetch_report(session, user_id=user_id, secret=secret, path=path)
            full_report_hex = _extract_full_report(data)
            if not full_report_hex:
                continue
            price = _decode_v3_benchmark_price(full_report_hex)
            if price is not None and price > 0:
                result[asset.lower().strip()] = float(price)
    return result


async def fetch_strikes_at_timestamp(
    user_id: str,
    secret: str,
    feed_ids: dict[str, str],
    epoch_start_unix: int,
    *,
    lead_delay_s: float = 1.0,
) -> dict[str, float]:
    """Benchmark prices at epoch_start_unix. Keys = asset (e.g. btc)."""
    if not feed_ids or not user_id or not secret:
        return {}

    if lead_delay_s > 0:
        await asyncio.sleep(lead_delay_s)

    result: dict[str, float] = {}
    async with aiohttp.ClientSession() as session:
        for asset, hex_id in feed_ids.items():
            path = f"/api/v1/reports?feedID={hex_id}&timestamp={epoch_start_unix}"
            price = await _strike_from_report_path_async(
                session,
                user_id=user_id,
                secret=secret,
                path=path,
            )
            if price is not None:
                result[asset.lower().strip()] = price
    return result


def fetch_strikes_at_timestamp_sync(
    user_id: str,
    secret: str,
    feed_ids: dict[str, str],
    epoch_start_unix: int,
) -> dict[str, float]:
    """Sync benchmark prices at epoch_start_unix. Keys = asset (e.g. btc)."""
    if not feed_ids or not user_id or not secret:
        return {}

    result: dict[str, float] = {}
    for asset, hex_id in feed_ids.items():
        path = f"/api/v1/reports?feedID={hex_id}&timestamp={epoch_start_unix}"
        price = _strike_from_report_path(
            user_id=user_id,
            secret=secret,
            path=path,
        )
        if price is not None:
            result[asset.lower().strip()] = price
    return result
