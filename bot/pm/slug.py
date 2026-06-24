"""Polymarket crypto up/down epoch slugs (ET-aligned)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import NamedTuple
from zoneinfo import ZoneInfo

from bot.constants import INTERVAL_SECONDS

_SLUG_RE = re.compile(r"^([a-z]+)-updown-(5m|15m|1h)-(\d+)$")

ET = ZoneInfo("America/New_York")


class EpochSlugs(NamedTuple):
    current_slug: str
    current_start: datetime
    epoch_start_ts: int
    epoch_end: datetime
    epoch_end_et: str


def epoch_start_ts(now_utc: datetime, interval: str) -> int:
    sec = INTERVAL_SECONDS[interval]
    et = now_utc.replace(tzinfo=UTC).astimezone(ET)
    interval_minutes = sec // 60
    floored_minute = (et.minute // interval_minutes) * interval_minutes
    start_et = et.replace(minute=floored_minute, second=0, microsecond=0)
    return int(start_et.astimezone(UTC).timestamp())


def parse_market_slug(slug: str) -> tuple[str, str, int] | None:
    m = _SLUG_RE.match(slug.strip().lower())
    if not m:
        return None
    return m.group(1), m.group(2), int(m.group(3))


def compute_epoch_slugs(asset: str, interval: str, now_utc: datetime) -> EpochSlugs:
    ts = epoch_start_ts(now_utc, interval)
    slug = f"{asset.lower()}-updown-{interval}-{ts}"
    start = datetime.fromtimestamp(ts, tz=UTC)
    end = start + timedelta(seconds=INTERVAL_SECONDS[interval])
    end_et = end.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S ET")
    return EpochSlugs(
        current_slug=slug,
        current_start=start,
        epoch_start_ts=ts,
        epoch_end=end,
        epoch_end_et=end_et,
    )


def market_timer_str(now_utc: datetime, epoch_start: datetime, interval: str) -> tuple[str, str]:
    """Elapsed / total and time-left strings for cycle logs."""
    sec = INTERVAL_SECONDS[interval]
    elapsed = max(0.0, now_utc.timestamp() - epoch_start.timestamp())
    left = max(0.0, sec - elapsed)

    def fmt(s: float) -> str:
        m, r = divmod(int(s), 60)
        return f"{m}:{r:02d}"

    return f"{fmt(elapsed)} / {fmt(sec)}", f"{fmt(left)} left"
