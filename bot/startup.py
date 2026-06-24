"""Session startup logging."""

from __future__ import annotations

import asyncio

from bot.config import Settings
from bot.pm.time_sync import measure_server_time_offset_sync, print_server_time_sync_line


async def run_startup_session(cfg: Settings) -> float:
    paper = cfg.bot.mode == "paper" or not cfg.execution.enabled
    clob_url = (cfg.api.clob_url or "").rstrip("/") or "https://clob.polymarket.com"
    strat = cfg.strategy

    print()
    print(f"  Beat Spike | mode={cfg.bot.mode} | execution.enabled={cfg.execution.enabled}")
    print(
        f"  Detection: {strat.price_feed} | threshold={strat.threshold_mode} | "
        f"lookback={strat.lookback_seconds:.0f}s"
    )
    if cfg.risk.enabled:
        print(
            f"  Risk: max_deploy=${cfg.risk.max_daily_deployed_usd} "
            f"max_dd={cfg.risk.max_daily_drawdown_pct}% "
            f"max_trades/day={cfg.risk.max_trades_per_day}"
        )
    print(
        f"  Entry: {cfg.entry.entry_moment_seconds}s after open | "
        f"End: {cfg.entry.end_moment_seconds}s before close"
    )
    print(
        f"  Order: {cfg.trading.order.style} | "
        f"limit={cfg.trading.order.limit_price} | "
        f"cancel={strat.time_limit_cancel_seconds}s"
    )
    if cfg.bot.fast_order_presign_enabled and cfg.trading.order.limit_price is not None:
        if paper or not cfg.execution.enabled:
            print("  Fast orders: presign UP+DOWN at slug start (paper/sim)")
        else:
            print(
                f"  Fast orders: presign UP+DOWN {cfg.trading.order.limit_order_type} "
                f"@ {cfg.trading.order.limit_price} at slug start"
            )
    if paper:
        print("  CLOB: paper/simulate (no live orders)")

    offset = 0.0
    if cfg.bot.use_server_time_sync:
        r = await asyncio.to_thread(
            measure_server_time_offset_sync,
            clob_url,
            n_samples=cfg.bot.server_time_sync_samples,
            n_keep=cfg.bot.server_time_sync_keep,
        )
        offset = r.offset_sec
        print_server_time_sync_line(r)

    assets = cfg.markets.active_assets()
    intervals = [str(i).lower() for i in cfg.markets.intervals]
    print(f"  Markets: assets={assets} intervals={intervals}")
    for a in assets:
        p = strat.asset_params(a)
        print(
            f"    {a.upper()}: |Δ|≥ {p.format_threshold_usd()} "
            f"vs {p.lookback_seconds:.0f}s ago"
            f" · beat cross: spike direction → UP/DOWN"
            + (f" sustain≥{p.sustain_seconds:.0f}s" if p.sustain_seconds > 0 else "")
        )
    return offset
