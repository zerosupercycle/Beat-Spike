"""Resolve Polymarket up/down market outcomes for trade win-rate stats."""

from __future__ import annotations

import json
import time
from typing import Any

import aiohttp

GAMMA_URL = "https://gamma-api.polymarket.com"
_CACHE_TTL_SEC = 300.0
_cache: dict[str, tuple[float, str | None]] = {}


def _parse_json_list(raw: Any) -> list[Any]:
    if isinstance(raw, str):
        return json.loads(raw)
    return list(raw or [])


def winning_side_from_market(market: dict[str, Any]) -> str | None:
    """Return 'up' or 'down' when resolved; None if still open or unknown."""
    status = str(market.get("umaResolutionStatus") or "").lower()
    closed = bool(market.get("closed"))
    if status != "resolved" and not closed:
        return None

    outcomes = _parse_json_list(market.get("outcomes"))
    prices = _parse_json_list(market.get("outcomePrices"))
    if len(outcomes) < 2 or len(prices) < 2:
        return None

    best_i = 0
    best_p = -1.0
    for i, p in enumerate(prices):
        try:
            fv = float(p)
        except (TypeError, ValueError):
            continue
        if fv > best_p:
            best_p = fv
            best_i = i

    if best_p < 0.5:
        return None

    label = str(outcomes[best_i]).lower()
    if "up" in label or label == "yes":
        return "up"
    if "down" in label or label == "no":
        return "down"
    return label


async def fetch_winning_side(session: aiohttp.ClientSession, slug: str) -> str | None:
    slug = slug.strip().lower()
    now = time.monotonic()
    cached = _cache.get(slug)
    if cached and now - cached[0] < _CACHE_TTL_SEC:
        return cached[1]

    base = GAMMA_URL.rstrip("/")
    market: dict[str, Any] | None = None
    for url in (f"{base}/events?slug={slug}", f"{base}/markets/slug/{slug}"):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json()
        except (aiohttp.ClientError, json.JSONDecodeError):
            continue

        if isinstance(data, list) and data:
            ev = data[0]
            markets = ev.get("markets") if isinstance(ev, dict) else None
            if markets and isinstance(markets[0], dict):
                market = markets[0]
        elif isinstance(data, dict):
            market = data
        if market:
            break

    winner = winning_side_from_market(market) if market else None
    _cache[slug] = (now, winner)
    return winner


def trade_result(side: str, winning_side: str | None) -> str:
    if winning_side is None:
        return "pending"
    s = side.lower().strip()
    return "win" if s == winning_side else "loss"


def trade_cost_usd(trade: dict[str, Any]) -> float | None:
    """USDC spent to enter (size_usd or shares × entry price)."""
    size_usd = trade.get("size_usd")
    if size_usd is not None:
        try:
            cost = float(size_usd)
            return cost if cost >= 0 else None
        except (TypeError, ValueError):
            pass
    try:
        shares = float(trade.get("shares", 0))
        price = float(trade.get("price", 0))
    except (TypeError, ValueError):
        return None
    if shares <= 0 or price <= 0:
        return None
    return shares * price


def trade_pnl_usd(trade: dict[str, Any]) -> float | None:
    """Binary market PnL: win → shares − cost; loss → −cost; pending → None."""
    result = trade.get("result")
    if result not in ("win", "loss"):
        return None
    cost = trade_cost_usd(trade)
    if cost is None:
        return None
    try:
        shares = float(trade.get("shares", 0))
    except (TypeError, ValueError):
        return None
    if shares <= 0:
        return None
    if result == "win":
        return shares - cost
    return -cost


def compute_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(1 for t in trades if t.get("result") == "win")
    losses = sum(1 for t in trades if t.get("result") == "loss")
    pending = sum(1 for t in trades if t.get("result") == "pending")
    resolved = wins + losses
    win_rate = (wins / resolved) if resolved else None

    pnls = [p for t in trades if (p := trade_pnl_usd(t)) is not None]
    total_pnl = sum(pnls) if pnls else None
    win_pnls = [trade_pnl_usd(t) for t in trades if t.get("result") == "win"]
    loss_pnls = [trade_pnl_usd(t) for t in trades if t.get("result") == "loss"]
    total_won = sum(p for p in win_pnls if p is not None) if win_pnls else None
    total_lost = sum(p for p in loss_pnls if p is not None) if loss_pnls else None

    return {
        "total": len(trades),
        "wins": wins,
        "losses": losses,
        "pending": pending,
        "resolved": resolved,
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "win_rate_pct": round(win_rate * 100, 1) if win_rate is not None else None,
        "total_pnl": round(total_pnl, 2) if total_pnl is not None else None,
        "total_pnl_wins": round(total_won, 2) if total_won is not None else None,
        "total_pnl_losses": round(total_lost, 2) if total_lost is not None else None,
    }
