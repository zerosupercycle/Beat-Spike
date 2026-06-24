"""Detection helpers applied after delta-momentum detection."""

from __future__ import annotations

from dataclasses import dataclass

from bot.config import StrategyConfig


@dataclass(frozen=True)
class BeatDirectionResult:
    token_side: str | None
    reject: str | None
    plus_delta: float | None = None
    minus_delta: float | None = None
    cross: str | None = None


def beat_split_deltas(
    ref_price: float,
    current_price: float,
    beat_price: float,
) -> tuple[float, float, str | None]:
    """Split lookback move at beat into +Δ (above beat) and −Δ (below beat).

    Returns (plus_delta, minus_delta, cross) where cross is ``up`` when price
    moves from below beat to above, ``down`` when from above to below, or None
    when the lookback window does not cross beat.
    """
    if ref_price < beat_price < current_price:
        return current_price - beat_price, beat_price - ref_price, "up"
    if current_price < beat_price < ref_price:
        return ref_price - beat_price, beat_price - current_price, "down"
    return 0.0, 0.0, None


def resolve_beat_token_side(
    ref_price: float,
    current_price: float,
    beat_price: float | None,
) -> BeatDirectionResult:
    """Pick UP/DOWN token from the beat-cross spike direction."""
    if beat_price is None or beat_price <= 0:
        return BeatDirectionResult(token_side=None, reject="beat=n/a")

    plus_d, minus_d, cross = beat_split_deltas(ref_price, current_price, beat_price)
    if cross is None:
        return BeatDirectionResult(
            token_side=None,
            reject=(
                f"no beat cross (ref={ref_price:.2f} now={current_price:.2f} "
                f"beat={beat_price:.2f})"
            ),
        )

    return BeatDirectionResult(
        token_side=cross,
        reject=None,
        plus_delta=plus_d,
        minus_delta=minus_d,
        cross=cross,
    )


def resolve_delta_threshold(
    cfg: StrategyConfig,
    asset: str,
    prices: list[float],
    fixed_usd: float,
) -> float:
    mode = cfg.threshold_mode
    if mode == "fixed_usd":
        return fixed_usd
    period = max(2, int(cfg.atr_period_samples))
    from bot.indicators import atr_usd

    atr = atr_usd(prices, period)
    if atr is None or atr <= 0:
        return fixed_usd
    return max(fixed_usd * 0.1, atr * float(cfg.atr_multiplier))
