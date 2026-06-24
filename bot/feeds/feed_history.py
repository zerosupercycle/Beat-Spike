"""Ring buffer of price + momentum samples for trade-time chart snapshots."""

from __future__ import annotations

import re
import time
from collections import deque
from typing import Any

DEFAULT_CAPTURE_WINDOW_SEC = 600.0
_HISTORY_SEC = 3900.0
_MAX_POINTS = 5000
_APPEND_INTERVAL_MS = 200.0

INTERVAL_SECONDS = {"5m": 300, "15m": 900, "1h": 3600}

_SLUG_EPOCH_RE = re.compile(r"^[a-z]+-updown-(5m|15m|1h)-(\d+)$")


def slug_epoch_bounds(slug: str) -> tuple[int, int] | None:
    """Return (epoch_start_unix, epoch_end_unix) from a Polymarket slug."""
    m = _SLUG_EPOCH_RE.match(slug.strip().lower())
    if not m:
        return None
    iv, ts = m.group(1), int(m.group(2))
    sec = INTERVAL_SECONDS.get(iv, 300)
    return ts, ts + sec


_CHART_ID_RE = re.compile(
    r"^([a-z]+-updown-(?:5m|15m|1h)-\d+)_(.+)$",
    re.IGNORECASE,
)


def parse_chart_id(chart_id: str) -> tuple[str, float] | None:
    """Parse ``{slug}_{trade_ts}`` chart id back to slug and order unix time."""
    m = _CHART_ID_RE.match(chart_id.strip())
    if not m:
        return None
    slug = m.group(1).lower()
    ts_raw = m.group(2)
    if "T" in ts_raw:
        date_part, time_part = ts_raw.split("T", 1)
        time_part = time_part.replace("_", ":", 2)
        iso = f"{date_part}T{time_part}"
    else:
        iso = ts_raw.replace("_", ":", 2)
    try:
        from datetime import datetime, timezone

        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return slug, dt.timestamp()
    except ValueError:
        return None


def merge_series_points(
    old_pts: list[dict[str, float]],
    new_pts: list[dict[str, float]],
) -> list[dict[str, float]]:
    """Union series by timestamp; prefer newer samples on duplicate times."""
    if not new_pts:
        return old_pts
    if not old_pts:
        return new_pts
    by_t = {p["t"]: p for p in old_pts}
    for p in new_pts:
        by_t[p["t"]] = p
    return sorted(by_t.values(), key=lambda p: p["t"])


def _momentum_scalar(mom: dict[str, Any] | None) -> float:
    if not mom:
        return 0.0
    roc = mom.get("roc_percent")
    if isinstance(roc, dict):
        v = roc.get("value")
        if v is not None:
            return float(v)
    abs_m = mom.get("absolute")
    if isinstance(abs_m, dict):
        v = abs_m.get("value")
        if v is not None:
            return float(v)
    return 0.0


class FeedHistory:
    def __init__(self) -> None:
        self._series: dict[str, deque[dict[str, float]]] = {}
        self._last_append_ms: dict[str, float] = {}

    def ingest(self, snap: dict[str, Any]) -> None:
        now = time.time()
        now_ms = now * 1000.0
        cutoff = now - _HISTORY_SEC

        for feed_id, feed in (snap.get("feeds") or {}).items():
            if not isinstance(feed, dict):
                continue
            for asset, row in (feed.get("assets") or {}).items():
                if not isinstance(row, dict):
                    continue
                price = row.get("price")
                if price is None:
                    continue
                try:
                    px = float(price)
                except (TypeError, ValueError):
                    continue
                if not (px > 0):
                    continue

                key = f"{feed_id}:{str(asset).upper()}"
                mom = _momentum_scalar(row.get("momentum"))
                buf = self._series.setdefault(key, deque(maxlen=_MAX_POINTS))
                last_ms = self._last_append_ms.get(key, 0.0)

                point = {"t": now, "price": px, "momentum": mom}
                if buf and now_ms - last_ms < _APPEND_INTERVAL_MS:
                    buf[-1] = point
                else:
                    buf.append(point)
                    self._last_append_ms[key] = now_ms

                while buf and buf[0]["t"] < cutoff:
                    buf.popleft()

    def capture(
        self,
        asset: str,
        enabled_feeds: list[str],
        *,
        window_sec: float = DEFAULT_CAPTURE_WINDOW_SEC,
    ) -> dict[str, list[dict[str, float]]]:
        cutoff = time.time() - float(window_sec)
        return self._capture_range(asset, enabled_feeds, cutoff, time.time())

    def capture_around(
        self,
        asset: str,
        enabled_feeds: list[str],
        center_ts: float,
        *,
        before_sec: float,
        after_sec: float,
    ) -> dict[str, list[dict[str, float]]]:
        start_t = float(center_ts) - float(before_sec)
        end_t = min(time.time(), float(center_ts) + float(after_sec))
        return self._capture_range(asset, enabled_feeds, start_t, end_t)

    def capture_epoch(
        self,
        asset: str,
        enabled_feeds: list[str],
        epoch_start: int,
        epoch_end: int,
        order_ts: float | None = None,
    ) -> dict[str, list[dict[str, float]]]:
        del order_ts
        end_t = min(time.time(), float(epoch_end))
        return self._capture_range(asset, enabled_feeds, float(epoch_start), end_t)

    def capture_keys(
        self,
        keys: list[str],
        start_t: float,
        end_t: float,
    ) -> dict[str, list[dict[str, float]]]:
        out: dict[str, list[dict[str, float]]] = {}
        for key in keys:
            buf = self._series.get(key)
            if not buf:
                out[key] = []
                continue
            out[key] = [
                {"t": p["t"], "price": p["price"], "momentum": p["momentum"]}
                for p in buf
                if start_t <= p["t"] <= end_t
            ]
        return out

    def price_at_time(
        self,
        asset: str,
        feed_id: str,
        target_ts: float,
        *,
        max_after_sec: float = 120.0,
    ) -> float | None:
        """Return feed price at ``target_ts`` (last tick at/before open, else first tick soon after)."""
        key = f"{feed_id.strip()}:{asset.upper().strip()}"
        buf = self._series.get(key)
        if not buf:
            return None
        pts = list(buf)
        if not pts:
            return None

        best: dict[str, float] | None = None
        for p in pts:
            if p["t"] <= target_ts + 0.001:
                if best is None or p["t"] > best["t"]:
                    best = p
        if best is not None:
            return float(best["price"])

        cutoff = float(target_ts) + max(0.0, float(max_after_sec))
        for p in sorted(pts, key=lambda row: row["t"]):
            if float(target_ts) < p["t"] <= cutoff:
                return float(p["price"])
        return None

    def _capture_range(
        self,
        asset: str,
        enabled_feeds: list[str],
        start_t: float,
        end_t: float,
    ) -> dict[str, list[dict[str, float]]]:
        asset_u = asset.upper().strip()
        out: dict[str, list[dict[str, float]]] = {}

        for feed_id in enabled_feeds:
            fid = feed_id.strip()
            if not fid:
                continue
            key = f"{fid}:{asset_u}"
            buf = self._series.get(key)
            if not buf:
                out[key] = []
                continue
            out[key] = [
                {"t": p["t"], "price": p["price"], "momentum": p["momentum"]}
                for p in buf
                if start_t <= p["t"] <= end_t
            ]
        return out
