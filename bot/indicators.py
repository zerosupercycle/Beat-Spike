"""Technical indicators from price (and optional volume) series."""

from __future__ import annotations

import math
from typing import Sequence


def ema_last(prices: Sequence[float], period: int) -> float | None:
    if not prices:
        return None
    period = max(1, int(period))
    alpha = 2.0 / (period + 1.0)
    ema = float(prices[0])
    for p in prices[1:]:
        ema = alpha * float(p) + (1.0 - alpha) * ema
    return ema


def rsi_last(prices: Sequence[float], period: int) -> float | None:
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
    return 100.0 - 100.0 / (1.0 + rs)


def atr_usd(prices: Sequence[float], period: int) -> float | None:
    """Simplified ATR from close-only ticks: mean |Δprice| over last `period` samples."""
    if len(prices) < period + 1:
        return None
    period = max(1, int(period))
    deltas = [
        abs(float(prices[i]) - float(prices[i - 1]))
        for i in range(len(prices) - period, len(prices))
    ]
    if not deltas:
        return None
    return sum(deltas) / len(deltas)


def adx_approx(prices: Sequence[float], period: int) -> float | None:
    """ADX-like trend strength from close-only series (0–100 scale, simplified)."""
    if len(prices) < period + 2:
        return None
    period = max(2, int(period))
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    tr_list: list[float] = []
    for i in range(len(prices) - period, len(prices)):
        if i < 1:
            continue
        up = float(prices[i]) - float(prices[i - 1])
        down = float(prices[i - 1]) - float(prices[i])
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        tr_list.append(abs(up))
    if not tr_list or sum(tr_list) <= 1e-18:
        return 0.0
    atr = sum(tr_list) / len(tr_list)
    pdi = 100.0 * (sum(plus_dm) / len(plus_dm)) / atr if plus_dm else 0.0
    mdi = 100.0 * (sum(minus_dm) / len(minus_dm)) / atr if minus_dm else 0.0
    denom = pdi + mdi
    if denom <= 1e-18:
        return 0.0
    dx = 100.0 * abs(pdi - mdi) / denom
    return min(100.0, max(0.0, dx))


def volume_ratio(recent: Sequence[float], baseline: Sequence[float]) -> float | None:
    if not recent or not baseline:
        return None
    r = sum(recent) / len(recent)
    b = sum(baseline) / len(baseline)
    if b <= 1e-18:
        return None
    return r / b
