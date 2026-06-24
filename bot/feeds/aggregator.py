from __future__ import annotations

import asyncio
import json
import math
import time
from asyncio import QueueEmpty
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bot.config import ChainlinkConfig, FeedsConfig, Settings, StrategyConfig
from bot.constants import ASSETS
from bot.detection import (
    PriceDeltaTracker,
    feed_asset_price,
    feed_quote_volume,
    feed_volume_delta,
)
from bot.feeds.base import AssetTick, utc_iso
from bot.feeds.feed_history import (
    DEFAULT_CAPTURE_WINDOW_SEC,
    FeedHistory,
    merge_series_points,
    slug_epoch_bounds,
)
from bot.feeds.beat_store import FeedBeatStore
from bot.feeds.binance import BinanceFeed
from bot.feeds.chainlink import ChainlinkFeed
from bot.feeds.chainlink_streams import fetch_strikes_at_timestamp_sync
from bot.pm.polymarket_beat import fetch_price_to_beat_sync
from bot.pm.slug import compute_epoch_slugs, parse_market_slug

DEFAULT_BEAT_REFRESH_SEC = 15.0
BEAT_LOOKUP_AFTER_SEC = 10.0
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class FeedAggregator:
    def __init__(
        self,
        cfg: FeedsConfig,
        *,
        strategy: StrategyConfig | None = None,
        status_path: Path | None = None,
        chainlink_cfg: ChainlinkConfig | None = None,
    ) -> None:
        self._cfg = cfg
        self._strategy = strategy
        self._status_path = status_path
        self._chainlink_cfg = chainlink_cfg or ChainlinkConfig()
        self._version = 0
        mom_cfg = {"window_seconds": cfg.momentum_window_seconds}
        self._chainlink = ChainlinkFeed(
            on_tick=self._on_tick,
            momentum_cfg=mom_cfg,
            streams_cfg=chainlink_cfg,
        )
        self._binance = BinanceFeed(on_tick=self._on_tick, momentum_cfg=mom_cfg)
        self._feeds = [self._chainlink, self._binance]
        self._feed_map = {f.id: f for f in self._feeds}
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._assets: list[str] = []
        self._clients: set[asyncio.Queue[dict[str, Any]]] = set()
        self._broadcast_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._history = FeedHistory()
        self._beat_store = FeedBeatStore()
        self._beat_cache: dict[str, float] = {}
        self._trackers: dict[str, PriceDeltaTracker] = {}
        self._intervals: list[str] = ["5m"]
        self._beats: dict[str, dict[str, dict[str, Any]]] = {}
        self._beats_by_slug: dict[str, dict[str, Any]] = {}
        self._last_epoch_slugs: dict[str, str] = {}
        self._beat_task: asyncio.Task | None = None
        self._beat_refresh_lock = asyncio.Lock()
        self._beat_refresh_pending = False

    def _on_tick(self, _feed_id: str, _tick: AssetTick) -> None:
        self._version += 1
        if self._clients and (self._broadcast_task is None or self._broadcast_task.done()):
            self._broadcast_task = asyncio.create_task(self._broadcast())

    async def _broadcast(self) -> None:
        await asyncio.sleep(0)
        snap = self.snapshot()
        dead: list[asyncio.Queue[dict[str, Any]]] = []
        for q in list(self._clients):
            while not q.empty():
                try:
                    q.get_nowait()
                except QueueEmpty:
                    break
            try:
                q.put_nowait(snap)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except QueueEmpty:
                    pass
                try:
                    q.put_nowait(snap)
                except asyncio.QueueFull:
                    dead.append(q)
        for q in dead:
            self._clients.discard(q)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
        self._clients.add(q)
        try:
            q.put_nowait(self.snapshot())
        except asyncio.QueueFull:
            pass
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._clients.discard(q)

    def snapshot(self) -> dict[str, Any]:
        snap = {
            "updated_at": utc_iso(),
            "version": self._version,
            "assets": list(ASSETS),
            "feeds": {f.id: f.snapshot() for f in self._feeds},
        }
        self._history.ingest(snap)
        if self._strategy and self._assets:
            snap["detection"] = self._detection_states(snap)
        if self._beats or self._beats_by_slug:
            beats, beats_by_slug = self._beats_payload(snap)
            if beats:
                snap["beats"] = beats
            if beats_by_slug:
                snap["beats_by_slug"] = beats_by_slug
        return snap

    @staticmethod
    def _merge_feed_beats(
        existing: dict[str, float | None] | None,
        new: dict[str, float | None] | None,
    ) -> dict[str, float | None]:
        merged: dict[str, float | None] = dict(existing or {})
        for fid, px in (new or {}).items():
            if px is not None and px > 0 and math.isfinite(float(px)):
                merged[fid] = float(px)
        return merged

    def _chainlink_beat_at_epoch(self, asset: str, epoch_start_ts: float) -> float | None:
        """Chainlink Data Streams benchmark at exact slug epoch (Polymarket resolution price)."""
        if not self._chainlink_cfg.ready():
            return None
        asset_l = asset.lower().strip()
        feed_id = self._chainlink_cfg.feed_ids.get(asset_l)
        if not feed_id:
            return None
        try:
            strikes = fetch_strikes_at_timestamp_sync(
                self._chainlink_cfg.streams_user_id,
                self._chainlink_cfg.streams_secret,
                {asset_l: feed_id},
                int(epoch_start_ts),
            )
        except Exception:
            return None
        px = strikes.get(asset_l)
        if px is not None and px > 0 and math.isfinite(float(px)):
            return float(px)
        return None

    def _resolve_feed_beats_for_slug(
        self,
        slug: str,
        asset: str,
        epoch_start_ts: float,
        *,
        allow_live: bool = False,
    ) -> dict[str, float | None]:
        """Each feed's price at ``epoch_start_ts`` (history first, then persisted snap)."""
        asset_u = asset.upper().strip()
        slug_key = slug.strip().lower()
        self.snapshot()
        stored = self._beat_store.get_feed_beats(slug_key)
        out: dict[str, float | None] = {}
        for feed in self._feeds:
            fid = feed.id
            if fid == "chainlink" and self._chainlink_cfg.ready():
                px = self._chainlink_beat_at_epoch(asset, epoch_start_ts)
                if px is None or px <= 0:
                    px = stored.get(fid)
                if px is not None and px > 0 and math.isfinite(float(px)):
                    out[fid] = float(px)
                else:
                    out[fid] = None
                continue
            px = self._history.price_at_time(
                asset_u,
                fid,
                float(epoch_start_ts),
                max_after_sec=BEAT_LOOKUP_AFTER_SEC,
            )
            if (px is None or px <= 0) and allow_live:
                row = feed.snapshot().get("assets", {}).get(asset_u, {})
                raw = row.get("price")
                if raw is not None:
                    try:
                        px = float(raw)
                    except (TypeError, ValueError):
                        px = None
            if px is None or px <= 0:
                px = stored.get(fid)
            if px is not None and px > 0 and math.isfinite(float(px)):
                out[fid] = float(px)
            else:
                out[fid] = None
        if any(v is not None and v > 0 for v in out.values()):
            self._beat_store.save(
                slug_key,
                epoch_start=int(epoch_start_ts),
                feed_beats=out,
            )
        return out

    def _feed_beats_at_epoch(
        self,
        asset: str,
        epoch_start_ts: float,
        *,
        slug: str | None = None,
        allow_live: bool = False,
    ) -> dict[str, float | None]:
        if slug:
            return self._resolve_feed_beats_for_slug(
                slug,
                asset,
                epoch_start_ts,
                allow_live=allow_live,
            )
        asset_u = asset.upper().strip()
        self.snapshot()
        out: dict[str, float | None] = {}
        for feed in self._feeds:
            fid = feed.id
            px = self._history.price_at_time(
                asset_u,
                fid,
                float(epoch_start_ts),
                max_after_sec=BEAT_LOOKUP_AFTER_SEC,
            )
            if (px is None or px <= 0) and allow_live:
                row = feed.snapshot().get("assets", {}).get(asset_u, {})
                raw = row.get("price")
                if raw is not None:
                    try:
                        px = float(raw)
                    except (TypeError, ValueError):
                        px = None
            if px is not None and px > 0 and math.isfinite(px):
                out[fid] = float(px)
            else:
                out[fid] = None
        return out

    @staticmethod
    def _feed_beats_from_series_at_epoch(
        series: dict[str, Any],
        asset: str,
        enabled_feeds: list[str],
        epoch_start: float,
    ) -> dict[str, float]:
        """Derive per-feed beats from captured chart series at slug open."""
        asset_u = asset.upper().strip()
        out: dict[str, float] = {}
        for fid in enabled_feeds:
            key = f"{fid.strip()}:{asset_u}"
            pts = series.get(key)
            if not isinstance(pts, list) or not pts:
                continue
            sorted_pts = sorted(
                (p for p in pts if isinstance(p, dict) and p.get("t") is not None),
                key=lambda p: float(p["t"]),
            )
            if not sorted_pts:
                continue
            px: float | None = None
            for p in sorted_pts:
                if float(p["t"]) <= float(epoch_start) + 0.001:
                    px = float(p["price"])
            if px is None:
                for p in sorted_pts:
                    if float(epoch_start) < float(p["t"]) <= float(epoch_start) + BEAT_LOOKUP_AFTER_SEC:
                        px = float(p["price"])
                        break
            if px is not None and px > 0 and math.isfinite(px):
                out[key] = px
        return out

    def _apply_feed_beats(self, asset: str, feed_beats: dict[str, float | None]) -> None:
        asset_u = asset.upper().strip()
        for feed in self._feeds:
            beat = feed_beats.get(feed.id)
            feed.set_beat(asset_u, beat if beat and beat > 0 else None)

    def _feed_beats_payload(
        self,
        asset: str,
        enabled_feeds: list[str],
        *,
        epoch_start: int | float | None = None,
        slug: str | None = None,
        series: dict[str, Any] | None = None,
    ) -> dict[str, float]:
        """Map ``binance:BTC`` -> beat for chart overlays."""
        asset_u = asset.upper().strip()
        slug_key = slug.strip().lower() if slug else None

        if series and epoch_start is not None:
            from_series = self._feed_beats_from_series_at_epoch(
                series, asset, enabled_feeds, float(epoch_start)
            )
            if from_series:
                return from_series

        if slug_key and epoch_start is not None:
            resolved = self._resolve_feed_beats_for_slug(
                slug_key,
                asset,
                float(epoch_start),
                allow_live=False,
            )
            out = {
                f"{fid}:{asset_u}": px
                for fid, px in resolved.items()
                if px is not None and px > 0
            }
            if out:
                return out

        pinned: dict[str, float | None] = {}
        if slug_key:
            row = self._beats_by_slug.get(slug_key) or {}
            pinned = dict(row.get("feed_beats") or {})

        out: dict[str, float] = {}
        for fid in enabled_feeds:
            fid = fid.strip()
            if not fid:
                continue
            beat: float | None = None
            pinned_px = pinned.get(fid)
            if pinned_px is not None and pinned_px > 0:
                beat = float(pinned_px)
            elif epoch_start is not None:
                px = self._history.price_at_time(
                    asset_u,
                    fid,
                    float(epoch_start),
                    max_after_sec=BEAT_LOOKUP_AFTER_SEC,
                )
                if px is not None and px > 0:
                    beat = float(px)
            if beat is not None and beat > 0:
                out[f"{fid}:{asset_u}"] = beat
        return out

    def snap_slug_feed_beats(
        self,
        asset: str,
        slug: str,
        epoch_start_ts: float,
        *,
        interval: str | None = None,
        allow_live: bool = False,
    ) -> dict[str, float | None]:
        """Pin each feed's beat to its price at slug epoch start."""
        slug_key = slug.strip().lower()
        asset_l = asset.lower().strip()
        asset_u = asset_l.upper()
        feed_beats = self._resolve_feed_beats_for_slug(
            slug_key,
            asset_l,
            epoch_start_ts,
            allow_live=allow_live,
        )
        self._apply_feed_beats(asset_l, feed_beats)

        pm_beat = None
        if interval:
            pm_beat = self._resolve_beat_price(asset_l, slug=slug_key, interval=interval)

        signal_beat = None
        if self._strategy is not None:
            signal_beat = feed_beats.get(self._strategy.price_feed)

        self._beats_by_slug[slug_key] = {
            "slug": slug_key,
            "asset": asset_u,
            "interval": interval,
            "epoch_start": int(epoch_start_ts),
            "feed_beats": dict(feed_beats),
            "beat": signal_beat,
            "polymarket_beat": pm_beat,
        }
        return feed_beats

    async def snap_slug_feed_beats_async(
        self,
        asset: str,
        slug: str,
        epoch_start_ts: float,
        *,
        interval: str | None = None,
        allow_live: bool = False,
    ) -> dict[str, float | None]:
        return await asyncio.to_thread(
            self.snap_slug_feed_beats,
            asset,
            slug,
            epoch_start_ts,
            interval=interval,
            allow_live=allow_live,
        )

    def _beat_delta_pct(self, snap: dict[str, Any], asset_u: str, beat: Any) -> float | None:
        if not isinstance(beat, (int, float)) or beat <= 0:
            return None
        cl_px = feed_asset_price(snap, "chainlink", asset_u)
        if cl_px is None:
            return None
        return round((cl_px - float(beat)) / float(beat) * 100.0, 4)

    def _beats_payload(
        self, snap: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        out: dict[str, Any] = {}
        for interval, assets in self._beats.items():
            interval_out: dict[str, Any] = {}
            for asset_u, row in assets.items():
                beat = row.get("beat")
                interval_out[asset_u] = {
                    **row,
                    "delta_pct": self._beat_delta_pct(snap, asset_u, beat),
                }
            out[interval] = interval_out

        by_slug: dict[str, Any] = {}
        for slug, row in self._beats_by_slug.items():
            asset_u = str(row.get("asset") or slug.split("-")[0]).upper()
            beat = row.get("beat")
            by_slug[slug] = {
                **row,
                "delta_pct": self._beat_delta_pct(snap, asset_u, beat),
            }
        return out, by_slug

    def _detection_states(self, snap: dict[str, Any]) -> dict[str, Any]:
        from bot.filters import resolve_delta_threshold

        assert self._strategy is not None
        strat = self._strategy
        feed_id = strat.price_feed
        now = time.time()
        out: dict[str, Any] = {}
        for asset in self._assets:
            asset_u = asset.upper()
            px = feed_asset_price(snap, feed_id, asset)
            if px is None:
                params = strat.asset_params(asset)
                out[asset_u] = {
                    "price": None,
                    "ref_price": None,
                    "delta_usd": None,
                    "max_abs_delta_usd": None,
                    "side": None,
                    "threshold_usd": params.threshold_usd_for_side("up"),
                    "threshold_up_usd": params.delta_threshold_up_usd,
                    "threshold_down_usd": params.delta_threshold_down_usd,
                    "lookback_seconds": params.lookback_seconds or strat.lookback_seconds,
                    "sustain_required_sec": params.sustain_seconds,
                    "sustain_elapsed_sec": 0.0,
                    "above_threshold": False,
                    "sustain_ready": False,
                    "feed_id": feed_id,
                }
                continue

            params = strat.asset_params(asset)
            lookback = float(params.lookback_seconds or strat.lookback_seconds)
            tracker = self._trackers.setdefault(asset, PriceDeltaTracker())
            prices = tracker.prices()
            threshold_up = resolve_delta_threshold(
                strat,
                asset,
                prices,
                params.delta_threshold_up_usd or 0.0,
            )
            threshold_down = resolve_delta_threshold(
                strat,
                asset,
                prices,
                params.delta_threshold_down_usd or 0.0,
            )
            out[asset_u] = tracker.status(
                now,
                px,
                volume_delta=feed_volume_delta(snap, feed_id, asset),
                quote_volume=feed_quote_volume(snap, asset),
                lookback_seconds=lookback,
                threshold_up_usd=threshold_up,
                threshold_down_usd=threshold_down,
                sustain_seconds=params.sustain_seconds,
                feed_id=feed_id,
            )
        return out

    def capture_trade_chart(
        self,
        asset: str,
        enabled_feeds: list[str],
        *,
        window_sec: float = DEFAULT_CAPTURE_WINDOW_SEC,
        slug: str | None = None,
        interval: str | None = None,
        epoch_start: int | None = None,
        epoch_end: int | None = None,
        order_ts: float | None = None,
        chart_id: str | None = None,
        window_before_sec: float | None = None,
        window_after_sec: float | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        del chart_id
        snap = self.snapshot()
        if (
            window_before_sec is not None
            and window_after_sec is not None
            and order_ts is not None
        ):
            center = float(order_ts)
            before = float(window_before_sec)
            after = float(window_after_sec)
            series = self._history.capture_around(
                asset,
                enabled_feeds,
                center,
                before_sec=before,
                after_sec=after,
            )
            window_start = center - before
            window_end = center + after
            payload: dict[str, Any] = {
                "captured_at": snap["updated_at"],
                "asset": asset.upper(),
                "source": source or "monitor",
                "window_before_sec": before,
                "window_after_sec": after,
                "window_start": window_start,
                "window_end": window_end,
                "series": series,
            }
        elif epoch_start is not None and epoch_end is not None:
            series = self._history.capture_epoch(
                asset, enabled_feeds, epoch_start, epoch_end, order_ts
            )
            payload = {
                "captured_at": snap["updated_at"],
                "asset": asset.upper(),
                "window_sec": window_sec,
                "series": series,
            }
        else:
            series = self._history.capture(asset, enabled_feeds, window_sec=window_sec)
            payload = {
                "captured_at": snap["updated_at"],
                "asset": asset.upper(),
                "window_sec": window_sec,
                "series": series,
            }
        if slug:
            payload["slug"] = slug
        if interval:
            payload["interval"] = interval
        if epoch_start is not None:
            payload["epoch_start"] = epoch_start
        if epoch_end is not None:
            payload["epoch_end"] = epoch_end
        if order_ts is not None:
            payload["order_ts"] = order_ts
        beat_epoch_start = epoch_start
        if beat_epoch_start is None and slug:
            bounds = slug_epoch_bounds(slug)
            if bounds:
                beat_epoch_start = bounds[0]
        feed_beats = self._feed_beats_payload(
            asset,
            enabled_feeds,
            epoch_start=beat_epoch_start,
            slug=slug,
            series=series if isinstance(series, dict) else None,
        )
        if feed_beats:
            payload["feed_beats"] = feed_beats
        beat = self._resolve_beat_price(
            asset,
            slug=slug,
            interval=interval,
        )
        if beat is not None:
            payload["polymarket_beat"] = beat
            payload["beat_price"] = beat
        return payload

    def enrich_monitor_chart(self, saved: dict[str, Any]) -> dict[str, Any]:
        window_start = saved.get("window_start")
        window_end = saved.get("window_end")
        series = saved.get("series")
        if (
            not isinstance(series, dict)
            or window_start is None
            or window_end is None
        ):
            return saved

        keys = [k for k in series if isinstance(k, str)]
        if not keys:
            return saved

        self.snapshot()
        end_t = min(time.time(), float(window_end))
        fresh = self._history.capture_keys(keys, float(window_start), end_t)
        merged: dict[str, list[dict[str, float]]] = {}
        changed = False
        for key in keys:
            old_pts = series.get(key) if isinstance(series.get(key), list) else []
            new_pts = fresh.get(key) or []
            combined = merge_series_points(old_pts, new_pts)
            if (
                not old_pts
                or len(combined) > len(old_pts)
                or (
                    combined
                    and old_pts
                    and combined[-1]["t"] > old_pts[-1]["t"] + 0.001
                )
            ):
                merged[key] = combined
                changed = True
            else:
                merged[key] = old_pts

        out = dict(saved) if changed else saved
        if changed:
            out["series"] = merged
            if end_t >= float(window_end) - 0.5 and not out.get("finalized_at"):
                out["finalized_at"] = utc_iso()

        slug = saved.get("slug")
        interval = saved.get("interval")
        if isinstance(slug, str) and isinstance(interval, str):
            asset = str(saved.get("asset") or slug.split("-")[0])
            bounds = slug_epoch_bounds(slug)
            epoch_start_val = bounds[0] if bounds else window_start
            enabled = [k.split(":", 1)[0] for k in keys if ":" in k]
            chart_series = merged if changed else series
            feed_beats = self._feed_beats_payload(
                asset,
                enabled or list(self._feed_map.keys()),
                epoch_start=epoch_start_val,
                slug=slug,
                series=chart_series if isinstance(chart_series, dict) else None,
            )
            if feed_beats:
                out = dict(out)
                out["feed_beats"] = feed_beats
            pm_beat = self._resolve_beat_price(asset, slug=slug, interval=interval)
            if pm_beat is not None:
                out = dict(out)
                out["polymarket_beat"] = pm_beat
                if out.get("beat_price") is None:
                    out["beat_price"] = pm_beat
        return out

    def beat_price(self, asset: str, *, slug: str, interval: str) -> float | None:
        """Polymarket price-to-beat for the active market slug."""
        return self._resolve_beat_price(asset, slug=slug, interval=interval)

    def _resolve_beat_price(
        self,
        asset: str,
        *,
        slug: str | None = None,
        interval: str | None = None,
        force: bool = False,
    ) -> float | None:
        """Polymarket beat = Chainlink benchmark at slug epoch start."""
        if not slug:
            return None
        slug_key = slug.strip().lower()
        cached = self._beat_cache.get(slug_key)
        if cached is not None and not force:
            return cached
        bounds = slug_epoch_bounds(slug_key)
        if not bounds:
            return cached
        epoch_start_ts = float(bounds[0])
        feed_beats = self._resolve_feed_beats_for_slug(
            slug_key,
            asset,
            epoch_start_ts,
            allow_live=False,
        )
        cl = feed_beats.get("chainlink")
        if cl is not None and cl > 0:
            self._beat_cache[slug_key] = float(cl)
            return float(cl)
        if interval:
            try:
                px = fetch_price_to_beat_sync(slug_key, asset, interval)
            except Exception:
                return cached
            if px is not None and px > 0:
                self._beat_cache[slug_key] = px
                return px
        return cached

    def enrich_trade_chart(self, saved: dict[str, Any]) -> dict[str, Any]:
        if saved.get("source") == "monitor":
            return self.enrich_monitor_chart(saved)
        epoch_start = saved.get("epoch_start")
        epoch_end = saved.get("epoch_end")
        series = saved.get("series")
        if not isinstance(series, dict) or epoch_start is None or epoch_end is None:
            return saved

        keys = [k for k in series if isinstance(k, str)]
        if not keys:
            return saved

        self.snapshot()
        end_t = min(time.time(), float(epoch_end))
        fresh = self._history.capture_keys(keys, float(epoch_start), end_t)
        merged: dict[str, list[dict[str, float]]] = {}
        changed = False
        for key in keys:
            old_pts = series.get(key) if isinstance(series.get(key), list) else []
            new_pts = fresh.get(key) or []
            combined = merge_series_points(old_pts, new_pts)
            if (
                not old_pts
                or len(combined) > len(old_pts)
                or (
                    combined
                    and old_pts
                    and combined[-1]["t"] > old_pts[-1]["t"] + 0.001
                )
            ):
                merged[key] = combined
                changed = True
            else:
                merged[key] = old_pts

        out = dict(saved) if changed else saved
        if changed:
            out["series"] = merged
            if end_t >= float(epoch_end) - 0.5 and not out.get("finalized_at"):
                out["finalized_at"] = utc_iso()

        slug = saved.get("slug")
        interval = saved.get("interval")
        if isinstance(slug, str) and isinstance(interval, str):
            asset = str(saved.get("asset") or slug.split("-")[0])
            bounds = slug_epoch_bounds(slug)
            epoch_start_val = saved.get("epoch_start") or (bounds[0] if bounds else None)
            enabled = [k.split(":", 1)[0] for k in keys if ":" in k]
            chart_series = merged if changed else series
            feed_beats = self._feed_beats_payload(
                asset,
                enabled or list(self._feed_map.keys()),
                epoch_start=epoch_start_val,
                slug=slug,
                series=chart_series if isinstance(chart_series, dict) else None,
            )
            if feed_beats:
                out = dict(out)
                out["feed_beats"] = feed_beats
            pm_beat = self._resolve_beat_price(asset, slug=slug, interval=interval)
            if pm_beat is not None:
                out = dict(out)
                out["polymarket_beat"] = pm_beat
                if out.get("beat_price") is None:
                    out["beat_price"] = pm_beat
        return out

    def feed(self, feed_id: str) -> ChainlinkFeed | BinanceFeed | None:
        return self._feed_map.get(feed_id)

    async def start(
        self,
        assets: list[str],
        *,
        intervals: list[str] | None = None,
    ) -> None:
        self._assets = [a.lower().strip() for a in assets if a.strip()]
        if intervals:
            self._intervals = [str(i).lower().strip() for i in intervals if str(i).strip()]
        self._stop.clear()
        for f in self._feeds:
            self._tasks.append(asyncio.create_task(f.run(self._stop)))
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        now = datetime.now(tz=UTC)
        for interval in self._intervals:
            for asset in self._assets:
                key = f"{interval}:{asset}"
                self._last_epoch_slugs[key] = compute_epoch_slugs(asset, interval, now).current_slug
        await self._refresh_beats_once()
        self._hydrate_beats_from_store()
        self._beat_task = asyncio.create_task(self._beat_loop())

    def _hydrate_beats_from_store(self) -> None:
        for slug, row in self._beat_store.all_rows().items():
            parsed = parse_market_slug(slug)
            if not parsed:
                continue
            asset, interval, _epoch = parsed
            feed_beats = self._beat_store.get_feed_beats(slug)
            if not feed_beats:
                continue
            existing = self._beats_by_slug.get(slug, {})
            merged = self._merge_feed_beats(existing.get("feed_beats"), feed_beats)
            self._beats_by_slug[slug] = {
                "slug": slug,
                "asset": asset.upper(),
                "interval": interval,
                "epoch_start": row.get("epoch_start"),
                "feed_beats": merged,
                "beat": existing.get("beat") or merged.get(
                    self._strategy.price_feed if self._strategy else ""
                ),
                "polymarket_beat": existing.get("polymarket_beat"),
            }

    async def _beat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.sleep(DEFAULT_BEAT_REFRESH_SEC)
                await self._refresh_beats_once()
            except asyncio.CancelledError:
                break
            except Exception:
                if self._stop.is_set():
                    break
                await asyncio.sleep(2.0)

    def _current_epoch_slugs(self, now: datetime | None = None) -> dict[str, tuple[str, str, str]]:
        """Return slug -> (asset, interval, slug) for each active asset interval."""
        ts = now or datetime.now(tz=UTC)
        out: dict[str, tuple[str, str, str]] = {}
        for interval in self._intervals:
            for asset in self._assets:
                slug = compute_epoch_slugs(asset, interval, ts).current_slug
                out[slug] = (asset, interval, slug)
        return out

    def _slugs_from_bot_status(self) -> list[tuple[str, str, str]]:
        path = self._status_path
        if path is None or not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        rows: list[tuple[str, str, str]] = []
        slugs = data.get("slugs")
        if isinstance(slugs, dict):
            for entry in slugs.values():
                if not isinstance(entry, dict):
                    continue
                slug = str(entry.get("slug") or "").strip().lower()
                asset = str(entry.get("asset") or "").strip().lower()
                interval = str(entry.get("interval") or "").strip().lower()
                if slug and asset and interval:
                    rows.append((slug, asset, interval))
        legacy = str(data.get("slug") or "").strip().lower()
        if legacy:
            parsed = parse_market_slug(legacy)
            if parsed:
                rows.append((legacy, parsed[0], parsed[1]))
        return rows

    def _collect_slug_targets(self) -> dict[str, tuple[str, str, bool]]:
        """slug -> (asset, interval, force_refresh)."""
        now = datetime.now(tz=UTC)
        current = self._current_epoch_slugs(now)
        targets: dict[str, tuple[str, str, bool]] = {
            slug: (asset, interval, True) for slug, (asset, interval, _s) in current.items()
        }
        for slug, asset, interval in self._slugs_from_bot_status():
            if slug not in targets:
                targets[slug] = (asset, interval, False)
        return targets

    def _epoch_slugs_changed(self) -> bool:
        now = datetime.now(tz=UTC)
        changed = False
        for interval in self._intervals:
            for asset in self._assets:
                key = f"{interval}:{asset}"
                slug = compute_epoch_slugs(asset, interval, now).current_slug
                if self._last_epoch_slugs.get(key) != slug:
                    self._last_epoch_slugs[key] = slug
                    changed = True
        return changed

    def _schedule_beat_refresh(self) -> None:
        if self._beat_refresh_pending or self._stop.is_set():
            return
        self._beat_refresh_pending = True
        asyncio.create_task(self._run_beat_refresh())

    async def _run_beat_refresh(self) -> None:
        try:
            await self._refresh_beats_once()
        finally:
            self._beat_refresh_pending = False

    async def _refresh_beats_once(self) -> None:
        async with self._beat_refresh_lock:
            await asyncio.to_thread(self._refresh_beats_sync)

    def _refresh_beats_sync(self) -> None:
        targets = self._collect_slug_targets()
        by_slug: dict[str, dict[str, Any]] = dict(self._beats_by_slug)
        now = time.time()
        for slug, (asset, interval, _force) in targets.items():
            bounds = slug_epoch_bounds(slug)
            epoch_start_ts = float(bounds[0]) if bounds else now
            allow_live = bounds is not None and 0 <= now - epoch_start_ts <= 5.0
            feed_beats = self._resolve_feed_beats_for_slug(
                slug,
                asset,
                epoch_start_ts,
                allow_live=allow_live,
            )
            self._apply_feed_beats(asset, feed_beats)
            pm_beat = self._resolve_beat_price(asset, slug=slug, interval=interval)
            signal_beat = None
            if self._strategy is not None:
                signal_beat = feed_beats.get(self._strategy.price_feed)
            by_slug[slug] = {
                "slug": slug,
                "asset": asset.upper(),
                "interval": interval,
                "epoch_start": int(epoch_start_ts),
                "feed_beats": feed_beats,
                "beat": signal_beat,
                "polymarket_beat": pm_beat,
            }

        beats: dict[str, dict[str, dict[str, Any]]] = {}
        for interval in self._intervals:
            assets_map: dict[str, dict[str, Any]] = {}
            for asset in self._assets:
                slug = compute_epoch_slugs(asset, interval, datetime.now(tz=UTC)).current_slug
                row = by_slug.get(slug, {})
                assets_map[asset.upper()] = {
                    "beat": row.get("beat"),
                    "slug": slug,
                    "feed_beats": row.get("feed_beats"),
                }
            beats[interval] = assets_map

        self._beats_by_slug = by_slug
        self._beats = beats

    async def ensure_beat_for_slug(self, slug: str) -> dict[str, Any] | None:
        slug_key = slug.strip().lower()
        parsed = parse_market_slug(slug_key)
        if not parsed:
            return None
        asset, interval = parsed[0], parsed[1]

        def _fetch() -> dict[str, Any]:
            bounds = slug_epoch_bounds(slug_key)
            epoch_start_ts = float(bounds[0]) if bounds else time.time()
            feed_beats = self._resolve_feed_beats_for_slug(
                slug_key,
                asset,
                epoch_start_ts,
                allow_live=False,
            )
            self._apply_feed_beats(asset, feed_beats)
            pm_beat = self._resolve_beat_price(
                asset,
                slug=slug_key,
                interval=interval,
                force=True,
            )
            signal_beat = feed_beats.get(self._strategy.price_feed) if self._strategy else None
            row = {
                "beat": signal_beat,
                "slug": slug_key,
                "asset": asset.upper(),
                "interval": interval,
                "epoch_start": int(epoch_start_ts),
                "feed_beats": feed_beats,
                "polymarket_beat": pm_beat,
            }
            self._beats_by_slug[slug_key] = row
            return row

        return await asyncio.to_thread(_fetch)

    async def _snap_beats_on_epoch_change(self) -> None:
        now = datetime.now(tz=UTC)
        async with self._beat_refresh_lock:
            await asyncio.to_thread(self._snap_beats_on_epoch_change_sync, now)

    def _snap_beats_on_epoch_change_sync(self, now: datetime) -> None:
        for interval in self._intervals:
            for asset in self._assets:
                slugs = compute_epoch_slugs(asset, interval, now)
                slug = slugs.current_slug
                epoch_start_ts = slugs.current_start.timestamp()
                self.snap_slug_feed_beats(
                    asset,
                    slug,
                    epoch_start_ts,
                    interval=interval,
                    allow_live=True,
                )

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(1.0)
            if self._epoch_slugs_changed():
                await self._snap_beats_on_epoch_change()
            self.snapshot()
            if self._clients and (self._broadcast_task is None or self._broadcast_task.done()):
                self._broadcast_task = asyncio.create_task(self._broadcast())

    async def stop(self) -> None:
        self._stop.set()
        if self._beat_task:
            self._beat_task.cancel()
            await asyncio.gather(self._beat_task, return_exceptions=True)
            self._beat_task = None
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        await asyncio.gather(self._heartbeat_task, return_exceptions=True)
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def reset_epoch(self) -> None:
        self._trackers.clear()

    async def wait_for_prices(
        self,
        asset: str,
        feed_ids: list[str],
        *,
        timeout_sec: float = 30.0,
    ) -> bool:
        from bot.detection import check_price_feed

        deadline = asyncio.get_event_loop().time() + timeout_sec
        while asyncio.get_event_loop().time() < deadline:
            snap = self.snapshot()
            ok = all(
                check_price_feed(snap, fid, asset=asset)[0] for fid in feed_ids
            )
            if ok:
                return True
            await asyncio.sleep(0.1)
        return False

    @classmethod
    def from_settings(cls, settings: Settings) -> FeedAggregator:
        from bot.history_store import resolve_storage_path

        status_path = resolve_storage_path(settings.storage.status_path, _PROJECT_ROOT)
        return cls(
            settings.feeds,
            strategy=settings.strategy,
            status_path=status_path,
            chainlink_cfg=settings.chainlink,
        )
