from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

from bot.constants import ASSETS
from bot.feeds.momentum import MomentumBuffers


def utc_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class AssetTick:
    base: str
    price: float
    received_at: str = field(default_factory=utc_iso)


@dataclass
class FeedHealth:
    state: str
    endpoint: str
    summary: str
    data_stale: bool = True
    last_error: str | None = None
    connected_since: str | None = None
    transport: str = "websocket"


class PriceFeed(ABC):
    id: str
    label: str

    def __init__(
        self,
        *,
        on_tick: Callable[[str, AssetTick], None],
        momentum_cfg: dict[str, Any] | None = None,
    ) -> None:
        self._on_tick = on_tick
        self._buffers = MomentumBuffers(ASSETS, momentum_cfg)
        self.health = FeedHealth(
            state="connecting",
            endpoint=self.endpoint,
            summary=f"{self.label}: connecting…",
            data_stale=True,
        )
        self._assets: dict[str, dict[str, Any]] = {
            b: {"price": None, "received_at": None, "momentum": None, "beat": None, "delta": None}
            for b in ASSETS
        }

    @property
    @abstractmethod
    def endpoint(self) -> str:
        ...

    def _set_health(
        self,
        state: str,
        *,
        summary: str | None = None,
        last_error: str | None = None,
        connected: bool = False,
    ) -> None:
        self.health.state = state
        self.health.data_stale = state != "connected"
        if summary:
            self.health.summary = summary
        if last_error is not None:
            self.health.last_error = last_error
        if connected and self.health.connected_since is None:
            self.health.connected_since = utc_iso()
        if not connected and state != "connected":
            self.health.connected_since = None

    def set_beat(self, base: str, beat: float | None) -> None:
        base = base.upper()
        if base not in self._assets:
            return
        row = self._assets[base]
        row["beat"] = beat
        price = row.get("price")
        if price is not None and beat is not None and beat > 0:
            delta = float(price) - float(beat)
            row["delta"] = round(delta, 8)
            row["outcome"] = "Up" if delta >= 0 else "Down"
        else:
            row["delta"] = None
            row["outcome"] = None

    def handle_price(
        self,
        base: str,
        price: float,
        *,
        volume_delta: float | None = None,
    ) -> None:
        base = base.upper()
        if base not in self._assets:
            return
        received_at = utc_iso()
        self._buffers.push(base, price)
        mom = self._buffers.compute(base)
        beat = self._assets[base].get("beat")
        delta = None
        outcome = None
        if beat is not None and beat > 0:
            delta = round(price - float(beat), 8)
            outcome = "Up" if delta >= 0 else "Down"
        vol_d = max(0.0, float(volume_delta)) if volume_delta is not None else None
        self._assets[base] = {
            "price": round(price, 8),
            "received_at": received_at,
            "momentum": mom,
            "beat": beat,
            "delta": delta,
            "outcome": outcome,
            "volume_delta": vol_d,
        }
        self._on_tick(self.id, AssetTick(base=base, price=price, received_at=received_at))

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "health": {
                "state": self.health.state,
                "endpoint": self.health.endpoint,
                "summary": self.health.summary,
                "data_stale": self.health.data_stale,
                "last_error": self.health.last_error,
                "connected_since": self.health.connected_since,
                "transport": self.health.transport,
            },
            "assets": {k: dict(v) for k, v in self._assets.items()},
        }

    @abstractmethod
    async def run(self, stop: asyncio.Event) -> None:
        ...
