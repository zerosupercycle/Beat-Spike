"""Daily risk limits — pause bot when breached."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from bot.trade_stats import trade_cost_usd, trade_pnl_usd, trade_result


@dataclass
class RiskState:
    paused: bool
    reason: str
    daily_deployed_usd: float
    daily_pnl_usd: float
    daily_trades: int


def _parse_ts_day(ts: str) -> date | None:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone(UTC).date()
    except ValueError:
        return None


def evaluate_daily_risk(
    trades_path: Path,
    *,
    max_daily_deployed_usd: float | None,
    max_daily_drawdown_pct: float | None,
    starting_bankroll_usd: float,
    max_trades_per_day: int | None,
    slug_winners: dict[str, str | None] | None = None,
) -> RiskState:
    if not trades_path.is_file():
        return RiskState(False, "", 0.0, 0.0, 0)

    today = datetime.now(tz=UTC).date()
    deployed = 0.0
    daily_pnl = 0.0
    count = 0
    winners = slug_winners or {}

    for line in trades_path.read_text(encoding="utf-8").strip().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        day = _parse_ts_day(str(row.get("ts") or ""))
        if day != today:
            continue
        count += 1
        cost = trade_cost_usd(row)
        if cost is not None:
            deployed += cost
        slug = str(row.get("slug") or "")
        winner = winners.get(slug)
        pnl = trade_pnl_usd(row, winner)
        if pnl is not None:
            daily_pnl += pnl

    if max_trades_per_day is not None and count >= max_trades_per_day:
        return RiskState(
            True,
            f"daily trades {count} >= limit {max_trades_per_day}",
            deployed,
            daily_pnl,
            count,
        )

    if max_daily_deployed_usd is not None and deployed >= max_daily_deployed_usd:
        return RiskState(
            True,
            f"daily deployed ${deployed:.2f} >= limit ${max_daily_deployed_usd:.2f}",
            deployed,
            daily_pnl,
            count,
        )

    if max_daily_drawdown_pct is not None and daily_pnl < 0:
        bankroll = max(1.0, float(starting_bankroll_usd))
        dd_pct = -daily_pnl / bankroll * 100.0
        if dd_pct >= max_daily_drawdown_pct:
            return RiskState(
                True,
                f"daily drawdown {dd_pct:.2f}% >= {max_daily_drawdown_pct:.2f}%",
                deployed,
                daily_pnl,
                count,
            )

    return RiskState(False, "", deployed, daily_pnl, count)
