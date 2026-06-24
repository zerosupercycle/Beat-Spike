"""Rolling short-window momentum from mid/last prices (shared across CEX feeds)."""

from __future__ import annotations

import math
import time
from collections import deque
from typing import Any

_MONO = tuple[float, float]  # (time.monotonic(), price)

DEFAULT_MOMENTUM_CONFIG: dict[str, Any] = {
    "window_seconds": 4.0,
    "min_samples": 2,
    "absolute_neutral_epsilon": 0.0,
    "roc_neutral_epsilon_percent": 0.0,
    "slope_neutral_epsilon": 0.0,
    "ema_span_samples": 5,
    "rsi_period": 6,
    "rsi_neutral_band": 5.0,
}


def _dir_signed(x: float, eps: float) -> str:
    if x > eps:
        return "up"
    if x < -eps:
        return "down"
    return "neutral"


def _dir_rsi(rsi: float, band: float) -> str:
    mid = 50.0
    if rsi > mid + band:
        return "up"
    if rsi < mid - band:
        return "down"
    return "neutral"


def _least_squares_slope(times_s: list[float], prices: list[float]) -> float:
    n = len(times_s)
    if n < 2:
        return 0.0
    st = sum(times_s)
    sp = sum(prices)
    stt = sum(t * t for t in times_s)
    stp = sum(times_s[i] * prices[i] for i in range(n))
    den = n * stt - st * st
    if den == 0.0 or not math.isfinite(den):
        return 0.0
    m = (n * stp - st * sp) / den
    return float(m) if math.isfinite(m) else 0.0


def _ema_last(prices: list[float], span: int) -> float | None:
    if not prices:
        return None
    span = max(1, int(span))
    alpha = 2.0 / (span + 1.0)
    ema = float(prices[0])
    for p in prices[1:]:
        ema = alpha * float(p) + (1.0 - alpha) * ema
    return ema


def _rsi_last(prices: list[float], period: int) -> float | None:
    if len(prices) < period + 1:
        return None
    period = max(1, int(period))
    changes = [float(prices[i]) - float(prices[i - 1]) for i in range(1, len(prices))]
    if len(changes) < period:
        return None
    seg = changes[-period:]
    gains = sum(max(c, 0.0) for c in seg)
    losses = sum(max(-c, 0.0) for c in seg)
    ag = gains / period
    al = losses / period
    if al <= 1e-18:
        return 100.0 if ag > 0 else 50.0
    rs = ag / al
    return 100.0 - (100.0 / (1.0 + rs))


def _tick_net(prices: list[float]) -> int:
    net = 0
    for i in range(1, len(prices)):
        if prices[i] > prices[i - 1]:
            net += 1
        elif prices[i] < prices[i - 1]:
            net -= 1
    return net


def _aggregate_direction(directions: list[str]) -> str:
    u = sum(1 for d in directions if d == "up")
    d = sum(1 for d in directions if d == "down")
    if u > d:
        return "up"
    if d > u:
        return "down"
    return "neutral"


def compute_momentum_payload(
    samples: list[_MONO],
    *,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    c = {**DEFAULT_MOMENTUM_CONFIG, **(cfg or {})}
    window = float(c["window_seconds"])
    min_n = max(2, int(c["min_samples"]))
    abs_eps = float(c["absolute_neutral_epsilon"])
    roc_eps = float(c["roc_neutral_epsilon_percent"])
    slope_eps = float(c["slope_neutral_epsilon"])
    ema_span = int(c["ema_span_samples"])
    rsi_period = int(c["rsi_period"])
    rsi_band = float(c["rsi_neutral_band"])

    if len(samples) < min_n:
        return None

    t0 = samples[0][0]
    t_end = samples[-1][0]
    prices = [p for _, p in samples]
    times_s = [t - t0 for t, _ in samples]
    p_first, p_last = prices[0], prices[-1]
    span = max(0.0, t_end - t0)

    absolute = float(p_last - p_first)
    d_abs = _dir_signed(absolute, abs_eps)
    roc = (absolute / float(p_first)) * 100.0 if abs(p_first) >= 1e-18 else 0.0
    d_roc = _dir_signed(roc, roc_eps)
    slope = _least_squares_slope(times_s, prices)
    d_slope = _dir_signed(slope, slope_eps)
    ema_level = _ema_last(prices, ema_span)
    if ema_level is None:
        ema_final = p_last
        price_minus_ema = 0.0
    else:
        ema_final = float(ema_level)
        price_minus_ema = float(p_last) - ema_final
    d_ema = _dir_signed(price_minus_ema, abs_eps)
    rsi_val = _rsi_last(prices, rsi_period)
    if rsi_val is None:
        rsi_final = 50.0
        d_rsi = "neutral"
    else:
        rsi_final = float(rsi_val)
        d_rsi = _dir_rsi(rsi_final, rsi_band)
    ticks = _tick_net(prices)
    d_tick = "up" if ticks > 0 else "down" if ticks < 0 else "neutral"
    metric_dirs = [d_abs, d_roc, d_slope, d_ema, d_rsi]

    return {
        "window_seconds": round(window, 6),
        "samples": len(samples),
        "span_seconds": round(span, 6),
        "p_at_window_start": round(p_first, 10),
        "p_now": round(p_last, 10),
        "absolute": {"value": round(absolute, 10), "direction": d_abs},
        "roc_percent": {"value": round(roc, 10), "direction": d_roc},
        "regression_slope_per_s": {"value": round(slope, 10), "direction": d_slope},
        "ema": {
            "price_minus_ema": round(price_minus_ema, 10),
            "ema_level": round(ema_final, 10),
            "direction": d_ema,
        },
        "rsi": {"value": round(rsi_final, 6), "direction": d_rsi},
        "tick_net": {"net": int(ticks), "direction": d_tick},
        "aggregate_direction": _aggregate_direction(metric_dirs),
    }


class MomentumBuffers:
    def __init__(self, bases: tuple[str, ...], cfg: dict[str, Any] | None = None):
        self._cfg = {**DEFAULT_MOMENTUM_CONFIG, **(cfg or {})}
        self._window = float(self._cfg["window_seconds"])
        self._buffers: dict[str, deque[_MONO]] = {b: deque() for b in bases}

    def push(self, base: str, price: float) -> None:
        buf = self._buffers.get(base)
        if buf is None or not math.isfinite(price):
            return
        now = time.monotonic()
        buf.append((now, float(price)))
        cutoff = now - self._window
        while buf and buf[0][0] < cutoff:
            buf.popleft()

    def compute(self, base: str) -> dict[str, Any] | None:
        buf = self._buffers.get(base)
        if not buf:
            return None
        return compute_momentum_payload(list(buf), cfg=self._cfg)
