"""Buy opportunity detection — |USD Δ| from lookback point to current price vs threshold."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any


def feed_health_ok(snap: dict[str, Any], feed_id: str) -> tuple[bool, str]:
    feeds = snap.get("feeds") or {}
    row = feeds.get(feed_id)
    if not row:
        return False, f"{feed_id}: missing"
    health = row.get("health") or {}
    state = str(health.get("state") or "")
    if state != "connected":
        return False, f"{feed_id}: state={state!r}"
    if health.get("data_stale"):
        return False, f"{feed_id}: data_stale"
    return True, f"{feed_id}: ok"


def feed_asset_price(snap: dict[str, Any], feed_id: str, asset: str) -> float | None:
    feeds = snap.get("feeds") or {}
    row = (feeds.get(feed_id, {}).get("assets") or {}).get(asset.upper())
    if not row:
        return None
    price = row.get("price")
    if price is None:
        return None
    try:
        v = float(price)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


VOLUME_FEED_IDS = ("binance",)


def feed_quote_volume(
    snap: dict[str, Any],
    asset: str,
    *,
    volume_feeds: tuple[str, ...] = VOLUME_FEED_IDS,
) -> float | None:
    """Cumulative quote volume (USDT) from a feed that tracks it."""
    feeds = snap.get("feeds") or {}
    asset_u = asset.upper()
    for fid in volume_feeds:
        row = (feeds.get(fid, {}).get("assets") or {}).get(asset_u, {})
        qv = row.get("quote_volume")
        if qv is None:
            continue
        try:
            v = float(qv)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v) and v >= 0:
            return v
    return None


def feed_volume_delta(
    snap: dict[str, Any],
    feed_id: str,
    asset: str,
    *,
    volume_feeds: tuple[str, ...] = VOLUME_FEED_IDS,
) -> float:
    """Per-tick volume delta; prefers binance when the signal feed has no volume."""
    feeds = snap.get("feeds") or {}
    asset_u = asset.upper()
    for fid in (*volume_feeds, feed_id):
        row = (feeds.get(fid, {}).get("assets") or {}).get(asset_u, {})
        vd = row.get("volume_delta")
        if vd is None:
            continue
        try:
            v = float(vd)
        except (TypeError, ValueError):
            continue
        if v > 0:
            return v
    return 0.0


def check_price_feed(
    snap: dict[str, Any],
    feed_id: str,
    *,
    asset: str,
) -> tuple[bool, str]:
    ok, msg = feed_health_ok(snap, feed_id)
    if not ok:
        return False, msg
    px = feed_asset_price(snap, feed_id, asset)
    if px is None:
        return False, f"{feed_id}: price=n/a ({asset.upper()})"
    return True, f"{feed_id}: ok px={px:.2f}"


@dataclass
class OpportunitySignal:
    side: str  # momentum direction that triggered (up | down)
    token_side: str  # outcome token to buy (from beat +Δ/−Δ split when beat crosses)
    price: float
    price_delta_usd: float
    ref_price: float
    lookback_seconds: float
    sustain_seconds: float
    threshold_usd: float
    feed_id: str
    reason: str


class PriceDeltaTracker:
    """Track feed prices; fire when |Δ| from lookback point to now exceeds threshold."""

    def __init__(self) -> None:
        self._history: deque[tuple[float, float, float]] = deque()
        self._sustain_start: float | None = None
        self._sustain_side: str | None = None
        self._last_quote_vol: float | None = None

    def reset(self) -> None:
        self._history.clear()
        self._sustain_start = None
        self._sustain_side = None
        self._last_quote_vol = None

    def prices(self) -> list[float]:
        return [px for _, px, _ in self._history]

    def volumes(self) -> list[float]:
        return [vol for _, _, vol in self._history]

    def _price_at(self, target_ts: float) -> float | None:
        if not self._history:
            return None
        best: tuple[float, float, float] | None = None
        for ts, px, _ in self._history:
            if ts <= target_ts:
                if best is None or ts > best[0]:
                    best = (ts, px, 0.0)
        return best[1] if best else None

    def _lookback_delta(
        self,
        ts: float,
        current_price: float,
        lookback_seconds: float,
    ) -> tuple[float, float, float] | None:
        """|Δ| and signed Δ from price at (ts − lookback) to current_price."""
        ref_price = self._price_at(ts - lookback_seconds)
        if ref_price is None:
            return None
        delta = current_price - ref_price
        return abs(delta), delta, ref_price

    def update(
        self,
        ts: float,
        price: float,
        *,
        volume_delta: float = 0.0,
        quote_volume: float | None = None,
        lookback_seconds: float,
        threshold_up_usd: float,
        threshold_down_usd: float,
        sustain_seconds: float,
        feed_id: str,
    ) -> OpportunitySignal | None:
        if quote_volume is not None:
            if self._last_quote_vol is not None:
                volume_delta = max(0.0, quote_volume - self._last_quote_vol)
            else:
                volume_delta = 0.0
            self._last_quote_vol = quote_volume
        self._history.append((ts, price, max(0.0, volume_delta)))
        keep_sec = max(lookback_seconds, sustain_seconds) + 120.0
        cutoff = ts - keep_sec
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()

        peak = self._lookback_delta(ts, price, lookback_seconds)
        if peak is None:
            self._sustain_start = None
            self._sustain_side = None
            return None

        abs_delta, delta, ref_price = peak
        side = "up" if delta > 0 else "down"
        threshold_usd = threshold_up_usd if side == "up" else threshold_down_usd
        if abs_delta < threshold_usd:
            self._sustain_start = None
            self._sustain_side = None
            return None

        if sustain_seconds <= 0:
            self._sustain_start = ts
            self._sustain_side = side
        else:
            if self._sustain_start is None or self._sustain_side != side:
                self._sustain_start = ts
                self._sustain_side = side
                return None
            if ts - self._sustain_start < sustain_seconds:
                return None

        sign = "+" if delta >= 0 else ""
        sustain_part = (
            f" sustained≥{sustain_seconds:.0f}s"
            if sustain_seconds > 0
            else ""
        )
        return OpportunitySignal(
            side=side,
            token_side=side,
            price=price,
            price_delta_usd=delta,
            ref_price=ref_price,
            lookback_seconds=lookback_seconds,
            sustain_seconds=sustain_seconds,
            threshold_usd=threshold_usd,
            feed_id=feed_id,
            reason=(
                f"delta_momentum_{side}("
                f"|Δ|={abs_delta:.2f}$/{lookback_seconds:.0f}s"
                f" Δ={sign}{delta:.2f}$"
                f"{sustain_part} thr={threshold_usd:.2f}$)"
            ),
        )

    def status(
        self,
        ts: float,
        price: float,
        *,
        volume_delta: float = 0.0,
        quote_volume: float | None = None,
        lookback_seconds: float,
        threshold_up_usd: float,
        threshold_down_usd: float,
        sustain_seconds: float,
        feed_id: str,
    ) -> dict[str, Any]:
        """Update history and return live detection metrics (no trade signal)."""
        if quote_volume is not None:
            if self._last_quote_vol is not None:
                volume_delta = max(0.0, quote_volume - self._last_quote_vol)
            else:
                volume_delta = 0.0
            self._last_quote_vol = quote_volume
        self._history.append((ts, price, max(0.0, volume_delta)))
        keep_sec = max(lookback_seconds, sustain_seconds) + 120.0
        cutoff = ts - keep_sec
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()

        peak = self._lookback_delta(ts, price, lookback_seconds)
        if peak is None:
            self._sustain_start = None
            self._sustain_side = None
            return {
                "price": price,
                "ref_price": None,
                "delta_usd": None,
                "max_abs_delta_usd": None,
                "side": None,
                "threshold_usd": threshold_up_usd,
                "threshold_up_usd": threshold_up_usd,
                "threshold_down_usd": threshold_down_usd,
                "lookback_seconds": lookback_seconds,
                "sustain_required_sec": sustain_seconds,
                "sustain_elapsed_sec": 0.0,
                "above_threshold": False,
                "sustain_ready": False,
                "feed_id": feed_id,
            }

        abs_delta, delta, ref_price = peak
        side = "up" if delta > 0 else "down"
        threshold_usd = threshold_up_usd if side == "up" else threshold_down_usd
        above = abs_delta >= threshold_usd
        if not above:
            self._sustain_start = None
            self._sustain_side = None
            return {
                "price": price,
                "ref_price": ref_price,
                "delta_usd": delta,
                "max_abs_delta_usd": abs_delta,
                "side": None,
                "threshold_usd": threshold_usd,
                "threshold_up_usd": threshold_up_usd,
                "threshold_down_usd": threshold_down_usd,
                "lookback_seconds": lookback_seconds,
                "sustain_required_sec": sustain_seconds,
                "sustain_elapsed_sec": 0.0,
                "above_threshold": False,
                "sustain_ready": False,
                "feed_id": feed_id,
            }

        if self._sustain_start is None or self._sustain_side != side:
            self._sustain_start = ts
            self._sustain_side = side

        elapsed = ts - (self._sustain_start or ts)
        return {
            "price": price,
            "ref_price": ref_price,
            "delta_usd": delta,
            "max_abs_delta_usd": abs_delta,
            "side": side,
            "threshold_usd": threshold_usd,
            "threshold_up_usd": threshold_up_usd,
            "threshold_down_usd": threshold_down_usd,
            "lookback_seconds": lookback_seconds,
            "sustain_required_sec": sustain_seconds,
            "sustain_elapsed_sec": round(elapsed, 2),
            "above_threshold": True,
            "sustain_ready": elapsed >= sustain_seconds,
            "feed_id": feed_id,
        }
