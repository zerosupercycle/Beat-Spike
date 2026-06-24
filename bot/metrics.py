"""Extended performance metrics from trade history."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from bot.trade_stats import compute_stats, trade_pnl_usd, trade_result


def compute_performance_metrics(
    trades: list[dict[str, Any]],
    slug_winners: dict[str, str | None],
) -> dict[str, Any]:
    enriched = []
    for t in trades:
        row = dict(t)
        slug = str(t.get("slug") or "")
        winner = slug_winners.get(slug)
        row["result"] = trade_result(str(t.get("side") or ""), winner)
        enriched.append(row)

    base = compute_stats(enriched)
    pnls: list[float] = []
    for t in enriched:
        pnl = trade_pnl_usd(t)
        if pnl is not None:
            pnls.append(pnl)

    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 1e-9 else None

    sharpe = None
    if len(pnls) >= 2:
        mean = sum(pnls) / len(pnls)
        var = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
        std = math.sqrt(var) if var > 0 else 0.0
        if std > 1e-9:
            sharpe = mean / std * math.sqrt(len(pnls))

    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    return {
        **base,
        "profit_factor": round(profit_factor, 3) if profit_factor is not None else None,
        "sharpe_approx": round(sharpe, 3) if sharpe is not None else None,
        "max_drawdown_usd": round(max_dd, 4),
    }


def load_metrics_from_file(
    trades_path: Path,
    slug_winners: dict[str, str | None],
) -> dict[str, Any]:
    if not trades_path.is_file():
        return compute_performance_metrics([], {})
    trades: list[dict[str, Any]] = []
    for line in trades_path.read_text(encoding="utf-8").strip().splitlines():
        try:
            trades.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return compute_performance_metrics(trades, slug_winners)
