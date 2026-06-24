"""Trade PnL and summary stats (shared by bot + server)."""

from __future__ import annotations

from typing import Any


def trade_result(side: str, winning_side: str | None) -> str:
    if winning_side is None:
        return "pending"
    s = side.lower().strip()
    return "win" if s == winning_side else "loss"


def trade_cost_usd(trade: dict[str, Any]) -> float | None:
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


def trade_pnl_usd(trade: dict[str, Any], winner: str | None = None) -> float | None:
    result = trade.get("result")
    if result is None and winner is not None:
        result = trade_result(str(trade.get("side") or ""), winner)
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
