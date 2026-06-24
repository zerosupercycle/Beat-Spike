"""Presign limit buy orders at slug start for fast post on DETECT."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from bot.config import Settings
from bot.constants import POLYMARKET_MIN_SHARES
from bot.pm.clob_exec import post_signed_limit_buy_for_cfg, sign_limit_buy_for_cfg


@dataclass
class PresignedOrder:
    side: str
    token_id: str
    price: float
    shares: float
    signed: Any
    order_type: str
    used: bool = False


@dataclass
class FastOrderSet:
    slug: str
    up: PresignedOrder | None = None
    down: PresignedOrder | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    enabled: bool = False
    error: str = ""

    def take(self, side: str) -> PresignedOrder | None:
        row = self.up if side == "up" else self.down
        if row is None or row.used:
            return None
        row.used = True
        return row

    async def wait_ready(self, timeout_sec: float = 15.0) -> bool:
        if self.ready.is_set():
            return True
        try:
            await asyncio.wait_for(self.ready.wait(), timeout=timeout_sec)
            return True
        except asyncio.TimeoutError:
            return False


def fast_presign_enabled(cfg: Settings, *, paper: bool) -> bool:
    if paper or not cfg.execution.enabled or not cfg.bot.fast_order_presign_enabled:
        return False
    order = cfg.trading.order
    if order.style == "market" or order.limit_price is None:
        return False
    return True


def presign_limit_price(cfg: Settings) -> float:
    px = cfg.trading.order.limit_price
    if px is None:
        raise ValueError("presign requires trading.order.limit_price")
    return max(0.01, min(0.99, round(float(px), 4)))


def presign_shares(cfg: Settings, price: float) -> float:
    tr = cfg.trading
    if tr.position_size == "usd":
        target = float(tr.usd) / max(price, 0.01)
    else:
        target = float(tr.shares)
    if target < POLYMARKET_MIN_SHARES:
        raise ValueError(
            f"presign shares {target:.4f} below minimum {POLYMARKET_MIN_SHARES}"
        )
    return round(target, 4)


def _side_from_sign(side: str, result: dict[str, Any]) -> PresignedOrder | None:
    if result.get("status") != "signed":
        return None
    return PresignedOrder(
        side=side,
        token_id=str(result["token_id"]),
        price=float(result["price"]),
        shares=float(result["shares"]),
        signed=result["signed"],
        order_type="",
    )


async def prepare_fast_orders(
    cfg: Settings,
    *,
    slug: str,
    up_token_id: str,
    down_token_id: str,
    log_prefix: str = "",
    paper: bool = False,
) -> FastOrderSet:
    """Presign GTC limit buys for UP and DOWN when slug starts."""
    prefix = f"  {log_prefix} " if log_prefix else "  "
    fos = FastOrderSet(slug=slug)

    if not fast_presign_enabled(cfg, paper=paper):
        fos.ready.set()
        return fos

    fos.enabled = True
    order = cfg.trading.order
    price = presign_limit_price(cfg)
    shares = presign_shares(cfg, price)
    ot = order.limit_order_type

    if paper:
        fos.up = PresignedOrder("up", up_token_id, price, shares, signed=None, order_type=ot)
        fos.down = PresignedOrder("down", down_token_id, price, shares, signed=None, order_type=ot)
        print(
            f"{prefix}[FAST] presigned UP+DOWN limit {ot} @ {price:.4f} sh={shares:.2f} (paper)"
        )
        fos.ready.set()
        return fos

    try:
        up_res, down_res = await asyncio.gather(
            asyncio.to_thread(sign_limit_buy_for_cfg, cfg, up_token_id, price, shares),
            asyncio.to_thread(sign_limit_buy_for_cfg, cfg, down_token_id, price, shares),
        )
        fos.up = _side_from_sign("up", up_res)
        fos.down = _side_from_sign("down", down_res)
        if fos.up:
            fos.up.order_type = ot
        if fos.down:
            fos.down.order_type = ot

        if fos.up and fos.down:
            print(
                f"{prefix}[FAST] presigned UP+DOWN limit {ot} @ {price:.4f} sh={shares:.2f}"
            )
        else:
            parts = []
            if not fos.up:
                parts.append(f"UP: {up_res.get('detail', up_res.get('status'))}")
            if not fos.down:
                parts.append(f"DOWN: {down_res.get('detail', down_res.get('status'))}")
            fos.error = "; ".join(parts)
            print(f"{prefix}[FAST] presign incomplete — {fos.error}")
    except Exception as exc:
        fos.error = str(exc)[:300]
        print(f"{prefix}[FAST] presign failed — {fos.error}")
    finally:
        fos.ready.set()

    return fos
