#!/usr/bin/env python3
"""Beat Spike dashboard API — price feeds + bot trades."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiohttp
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.config import load_config  # noqa: E402
from bot.feeds.aggregator import FeedAggregator  # noqa: E402
from bot.feeds.feed_history import parse_chart_id, slug_epoch_bounds  # noqa: E402
from bot.history_store import chart_has_series_data  # noqa: E402
from bot.metrics import load_metrics_from_file  # noqa: E402
from bot.monitor.config import load_monitor_config, monitor_paths  # noqa: E402
from trade_resolution import compute_stats, fetch_winning_side, trade_result  # noqa: E402

aggregator: FeedAggregator | None = None
dashboard_config: dict[str, Any] = {}


def _snapshots_dir() -> Path:
    cfg = load_config(ROOT / "config" / "default.yaml")
    p = Path(cfg.storage.trade_snapshots_dir)
    d = p if p.is_absolute() else ROOT / p
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_chart_id(chart_id: str) -> str | None:
    safe = chart_id.strip()
    if not safe or ".." in safe or "/" in safe or "\\" in safe:
        return None
    return safe


def _monitor_storage_paths() -> tuple[Path, Path]:
    cfg = load_monitor_config(ROOT / "config" / "monitor.yaml")
    events_path, snapshots_dir, _state = monitor_paths(cfg)
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    return events_path, snapshots_dir


def _persist_snapshot(
    chart_id: str,
    payload: dict[str, Any],
    *,
    monitor: bool = False,
) -> None:
    if not chart_has_series_data(payload):
        return
    safe = _safe_chart_id(chart_id)
    if not safe:
        return
    base = _monitor_storage_paths()[1] if monitor else _snapshots_dir()
    path = base / f"{safe}.json"
    with contextlib.suppress(OSError):
        path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


def _persist_snapshot_legacy(chart_id: str, payload: dict[str, Any]) -> None:
    _persist_snapshot(chart_id, payload, monitor=False)


def _trade_for_chart_id(chart_id: str) -> dict[str, Any] | None:
    trades_path, _ = _paths_from_config()
    if not trades_path.is_file():
        return None
    for line in reversed(trades_path.read_text(encoding="utf-8").strip().splitlines()):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(row.get("chart_id") or "") == chart_id:
            return row
    return None


def _rebuild_chart_from_history(chart_id: str) -> dict[str, Any] | None:
    """Rebuild snapshot from ring buffer when JSON file was never written."""
    if aggregator is None:
        return None
    parsed = parse_chart_id(chart_id)
    if not parsed:
        return None
    slug, order_ts = parsed
    bounds = slug_epoch_bounds(slug)
    if not bounds:
        return None
    trade = _trade_for_chart_id(chart_id) or {}
    cfg = load_config(ROOT / "config" / "default.yaml")
    asset = str(trade.get("asset") or slug.split("-")[0])
    interval = str(trade.get("interval") or "5m")
    from bot.config import FEED_IDS

    payload = aggregator.capture_trade_chart(
        asset,
        sorted(FEED_IDS),
        slug=slug,
        interval=interval,
        epoch_start=bounds[0],
        epoch_end=bounds[1],
        order_ts=order_ts,
    )
    series = payload.get("series") or {}
    if not any(isinstance(v, list) and v for v in series.values()):
        return None
    payload["rebuilt_at"] = payload.get("captured_at")
    _persist_snapshot_legacy(chart_id, payload)
    return payload


def _paths_from_config() -> tuple[Path, Path]:
    cfg = load_config(ROOT / "config" / "default.yaml")
    trades = Path(cfg.storage.trades_path)
    status = Path(cfg.storage.status_path)
    if not trades.is_absolute():
        trades = ROOT / trades
    if not status.is_absolute():
        status = ROOT / status
    return trades, status


@asynccontextmanager
async def lifespan(app: FastAPI):
    global aggregator, dashboard_config
    cfg = load_config(ROOT / "config" / "default.yaml")
    assets = cfg.markets.active_assets()
    intervals = [str(i).lower() for i in cfg.markets.intervals]
    from bot.config import FEED_IDS

    dashboard_config = {
        "assets": [a.upper() for a in assets],
        "intervals": intervals,
        "price_feed": cfg.strategy.price_feed,
        "feeds": sorted(FEED_IDS),
        "strategy": {
            "lookback_seconds": cfg.strategy.lookback_seconds,
            "sustain_seconds": cfg.strategy.sustain_seconds,
            "by_asset": {
                k: v.model_dump(exclude_none=True) for k, v in cfg.strategy.by_asset.items()
            },
        },
    }
    aggregator = FeedAggregator.from_settings(cfg)
    await aggregator.start(assets, intervals=intervals)
    yield
    if aggregator:
        await aggregator.stop()


app = FastAPI(title="Beat Spike Dashboard", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _enrich_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    out = dict(snap)
    out["config"] = dashboard_config
    return out


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config")
async def config() -> dict[str, Any]:
    return dashboard_config


@app.get("/api/snapshot")
async def snapshot() -> dict[str, Any]:
    if aggregator is None:
        return {"error": "not ready"}
    return _enrich_snapshot(aggregator.snapshot())


@app.get("/api/beat")
async def beat_for_slug(slug: str) -> dict[str, Any]:
    if aggregator is None:
        return {"error": "not ready", "slug": slug, "beat": None}
    row = await aggregator.ensure_beat_for_slug(slug)
    if row is None:
        return {"slug": slug.strip().lower(), "beat": None}
    return row


class SnapBeatsRequest(BaseModel):
    asset: str
    slug: str
    epoch_start: float
    interval: str | None = None
    allow_live: bool = False


@app.post("/api/feeds/snap-beats")
async def snap_beats(body: SnapBeatsRequest) -> dict[str, Any]:
    if aggregator is None:
        return {"error": "not ready", "feed_beats": {}}
    feed_beats = await aggregator.snap_slug_feed_beats_async(
        body.asset,
        body.slug,
        float(body.epoch_start),
        interval=body.interval,
        allow_live=body.allow_live,
    )
    return {"ok": True, "feed_beats": feed_beats}


@app.get("/api/bot/status")
async def bot_status() -> dict[str, Any]:
    _, status_path = _paths_from_config()
    if not status_path.is_file():
        return {"state": "idle", "detail": "bot not started"}
    try:
        return json.loads(status_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"state": "error", "detail": "invalid bot_status.json"}


def _enrich_trade(t: dict[str, Any], slug_winners: dict[str, str | None]) -> dict[str, Any]:
    slug = str(t.get("slug") or "")
    side = str(t.get("side") or "")
    winner = slug_winners.get(slug)
    row = dict(t)
    row["resolved_side"] = winner
    row["result"] = trade_result(side, winner)
    return row


def _monitor_target_key(handle: str) -> str:
    return str(handle or "").strip().lower().lstrip("@")


def _enrich_monitor_event(e: dict[str, Any], slug_winners: dict[str, str | None]) -> dict[str, Any]:
    slug = str(e.get("slug") or "")
    side = str(e.get("outcome") or "").lower().strip()
    winner = slug_winners.get(slug)
    row = dict(e)
    row["resolved_side"] = winner
    row["result"] = trade_result(side, winner)
    if row.get("shares") is None and row.get("size") is not None:
        row["shares"] = row["size"]
    if row.get("size_usd") is None and row.get("usdc_size") is not None:
        row["size_usd"] = row["usdc_size"]
    return row


def _monitor_usdc_size(row: dict[str, Any]) -> float:
    usdc = row.get("usdc_size")
    if usdc is not None:
        try:
            return float(usdc)
        except (TypeError, ValueError):
            pass
    try:
        return float(row.get("price", 0)) * float(row.get("size", 0))
    except (TypeError, ValueError):
        return 0.0


def _trade_size_usd(row: dict[str, Any]) -> float:
    size_usd = row.get("size_usd")
    if size_usd is not None:
        try:
            return float(size_usd)
        except (TypeError, ValueError):
            pass
    try:
        return float(row.get("shares", 0)) * float(row.get("price", 0))
    except (TypeError, ValueError):
        return 0.0


def _merge_bot_trades_by_slug(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Combine multiple fills on the same slug + side into one row."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for t in trades:
        slug = str(t.get("slug") or "").strip().lower()
        side = str(t.get("side") or "").strip().lower()
        if not slug:
            continue
        groups.setdefault((slug, side), []).append(t)

    merged: list[dict[str, Any]] = []
    for (slug, side), items in groups.items():
        items.sort(key=lambda row: str(row.get("ts") or ""))
        base = dict(items[0])
        total_shares = sum(float(row.get("shares") or 0) for row in items)
        total_size_usd = sum(_trade_size_usd(row) for row in items)
        base["shares"] = round(total_shares, 4)
        base["size_usd"] = round(total_size_usd, 2)
        base["fill_count"] = len(items)
        base["row_id"] = f"{slug}:{side}"
        if len(items) > 1:
            for row in reversed(items):
                if row.get("chart_id"):
                    base["chart_id"] = row["chart_id"]
                    break
        merged.append(base)

    merged.sort(key=lambda row: str(row.get("ts") or ""), reverse=True)
    return merged


def _merge_monitor_events_by_slug(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Combine multiple buys on the same profile + slug into one row."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for e in events:
        slug = str(e.get("slug") or "").strip().lower()
        if not slug:
            continue
        key = (_monitor_target_key(str(e.get("target") or "")), slug)
        groups.setdefault(key, []).append(e)

    merged: list[dict[str, Any]] = []
    for (target, slug), items in groups.items():
        items.sort(key=lambda row: str(row.get("ts") or ""))
        base = dict(items[0])
        total_size = sum(float(row.get("size") or 0) for row in items)
        total_usdc = sum(_monitor_usdc_size(row) for row in items)
        base["size"] = round(total_size, 4)
        base["usdc_size"] = round(total_usdc, 2)
        base["shares"] = base["size"]
        base["size_usd"] = base["usdc_size"]
        base["fill_count"] = len(items)
        base["row_id"] = f"{target}:{slug}"
        if len(items) > 1:
            for row in reversed(items):
                if row.get("chart_id"):
                    base["chart_id"] = row["chart_id"]
                    break
        merged.append(base)

    merged.sort(key=lambda row: str(row.get("ts") or ""), reverse=True)
    return merged


@app.get("/api/bot/trades")
async def bot_trades(limit: int = 100) -> dict[str, Any]:
    trades_path, _ = _paths_from_config()
    if not trades_path.is_file():
        return {"trades": [], "stats": compute_stats([])}
    lines = trades_path.read_text(encoding="utf-8").strip().splitlines()
    all_trades: list[dict[str, Any]] = []
    for line in lines:
        try:
            all_trades.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    slug_winners: dict[str, str | None] = {}
    async with aiohttp.ClientSession() as session:
        for t in all_trades:
            slug = str(t.get("slug") or "")
            if not slug or slug in slug_winners:
                continue
            slug_winners[slug] = await fetch_winning_side(session, slug)

    all_enriched = [_enrich_trade(t, slug_winners) for t in all_trades]
    all_merged = _merge_bot_trades_by_slug(all_enriched)
    display = all_merged[:limit]
    return {"trades": display, "stats": compute_stats(all_merged)}


@app.get("/api/bot/metrics")
async def bot_metrics() -> dict[str, Any]:
    trades_path, _ = _paths_from_config()
    if not trades_path.is_file():
        return {"metrics": load_metrics_from_file(trades_path, {})}
    lines = trades_path.read_text(encoding="utf-8").strip().splitlines()
    trades: list[dict[str, Any]] = []
    for line in lines:
        try:
            trades.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    slug_winners: dict[str, str | None] = {}
    async with aiohttp.ClientSession() as session:
        for t in trades:
            slug = str(t.get("slug") or "")
            if slug and slug not in slug_winners:
                slug_winners[slug] = await fetch_winning_side(session, slug)
    return {"metrics": load_metrics_from_file(trades_path, slug_winners)}


class CaptureChartRequest(BaseModel):
    asset: str
    enabled_feeds: list[str] = Field(default_factory=list)
    slug: str | None = None
    interval: str | None = None
    epoch_start: int | None = None
    epoch_end: int | None = None
    order_ts: float | None = None
    chart_id: str | None = None
    window_before_sec: float | None = None
    window_after_sec: float | None = None
    source: str | None = None
    snapshot_dir: str | None = None


@app.post("/api/feeds/capture-chart")
async def capture_chart(body: CaptureChartRequest) -> dict[str, Any]:
    if aggregator is None:
        return {"error": "not ready", "series": {}}
    payload = aggregator.capture_trade_chart(
        body.asset,
        body.enabled_feeds,
        slug=body.slug,
        interval=body.interval,
        epoch_start=body.epoch_start,
        epoch_end=body.epoch_end,
        order_ts=body.order_ts,
        window_before_sec=body.window_before_sec,
        window_after_sec=body.window_after_sec,
        source=body.source,
    )
    if body.chart_id and isinstance(payload, dict) and "error" not in payload:
        is_monitor = body.snapshot_dir == "monitor" or body.source == "monitor"
        _persist_snapshot(body.chart_id, payload, monitor=is_monitor)
    return payload


@app.get("/api/bot/trade-chart/{chart_id}")
async def bot_trade_chart(chart_id: str) -> dict[str, Any]:
    safe = _safe_chart_id(chart_id)
    if not safe:
        return {"error": "invalid chart id"}
    path = _snapshots_dir() / f"{safe}.json"
    saved: dict[str, Any] | None = None
    if path.is_file():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"error": "invalid snapshot file"}
    else:
        saved = _rebuild_chart_from_history(safe)
        if saved is None:
            return {"error": "not found"}
    if aggregator is not None and isinstance(saved, dict) and "error" not in saved:
        enriched = aggregator.enrich_trade_chart(saved)
        if enriched is not saved:
            with contextlib.suppress(OSError):
                path.write_text(json.dumps(enriched, separators=(",", ":")), encoding="utf-8")
        return enriched
    return saved


@app.get("/api/monitor/events")
async def monitor_events(limit: int = 100) -> dict[str, Any]:
    events_path, _ = _monitor_storage_paths()
    targets = _monitor_target_labels()
    empty_stats = compute_stats([])
    if not events_path.is_file():
        return {
            "events": [],
            "targets": [{**t, "stats": empty_stats} for t in targets],
            "stats": empty_stats,
            "stats_by_target": {},
        }
    active_targets = {_monitor_target_key(t["handle"]) for t in targets}
    rows: list[dict[str, Any]] = []
    for line in events_path.read_text(encoding="utf-8").strip().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if active_targets and _monitor_target_key(str(row.get("target") or "")) not in active_targets:
            continue
        rows.append(row)

    slug_winners: dict[str, str | None] = {}
    async with aiohttp.ClientSession() as session:
        for e in rows:
            slug = str(e.get("slug") or "")
            if slug and slug not in slug_winners:
                slug_winners[slug] = await fetch_winning_side(session, slug)

    enriched = [_enrich_monitor_event(e, slug_winners) for e in rows]
    merged = _merge_monitor_events_by_slug(enriched)
    display_merged = merged[:limit]

    by_target: dict[str, list[dict[str, Any]]] = {}
    for e in merged:
        key = _monitor_target_key(str(e.get("target") or ""))
        if not key:
            continue
        by_target.setdefault(key, []).append(e)
    stats_by_target = {k: compute_stats(v) for k, v in by_target.items()}

    targets_out = [
        {**t, "stats": stats_by_target.get(_monitor_target_key(t["handle"]), empty_stats)}
        for t in targets
    ]
    return {
        "events": display_merged,
        "targets": targets_out,
        "stats": compute_stats(merged),
        "stats_by_target": stats_by_target,
    }


def _monitor_target_labels() -> list[dict[str, str]]:
    cfg = load_monitor_config(ROOT / "config" / "monitor.yaml")
    out: list[dict[str, str]] = []
    for raw in cfg.targets:
        s = str(raw).strip()
        if not s:
            continue
        if s.startswith("@"):
            handle = s[1:]
        elif "polymarket.com" in s:
            handle = s.rstrip("/").split("/")[-1].lstrip("@")
        else:
            handle = s
        out.append({"handle": handle, "url": f"https://polymarket.com/@{handle.lower()}"})
    return out


@app.get("/api/monitor/trade-chart/{chart_id}")
async def monitor_trade_chart(chart_id: str) -> dict[str, Any]:
    safe = _safe_chart_id(chart_id)
    if not safe:
        return {"error": "invalid chart id"}
    _, snapshots_dir = _monitor_storage_paths()
    path = snapshots_dir / f"{safe}.json"
    if not path.is_file():
        return {"error": "not found"}
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"error": "invalid snapshot file"}
    if aggregator is not None and isinstance(saved, dict) and "error" not in saved:
        enriched = aggregator.enrich_monitor_chart(saved)
        if enriched is not saved:
            with contextlib.suppress(OSError):
                path.write_text(json.dumps(enriched, separators=(",", ":")), encoding="utf-8")
        return enriched
    return saved


@app.websocket("/ws")
async def ws_dashboard(websocket: WebSocket) -> None:
    await websocket.accept()
    if aggregator is None:
        await websocket.close(code=1013)
        return

    q = aggregator.subscribe()
    try:
        while True:
            try:
                data = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if websocket.client_state.name != "CONNECTED":
                    break
                continue
            await websocket.send_text(json.dumps(_enrich_snapshot(data)))
    except WebSocketDisconnect:
        pass
    finally:
        aggregator.unsubscribe(q)


def main() -> None:
    p = argparse.ArgumentParser(description="Beat Spike dashboard server")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8788)
    p.add_argument("--config", default=str(ROOT / "config" / "default.yaml"))
    args = p.parse_args()
    uvicorn.run("main:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
