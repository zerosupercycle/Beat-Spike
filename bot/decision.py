from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bot.config import OrderConfig, TradingConfig
from bot.constants import POLYMARKET_MIN_SHARES
from bot.detection import OpportunitySignal
from bot.pm.orderbook import BestBidAsk, InMemoryOrderbookStore


@dataclass
class EntryDecision:
    asset: str
    interval: str
    slug: str
    side: str
    token_id: str
    price: float
    shares: float
    size_usd: float
    position_size_mode: str
    best_ask: float
    feed_price: float
    price_delta_usd: float
    signal_feed: str
    reason: str


def limit_price_from_book(best: BestBidAsk, order: OrderConfig) -> float:
    ref = order.limit_reference
    if ref == "best_bid":
        px = best.bid_price
    elif ref == "best_ask":
        px = best.ask_price
    else:
        px = best.midpoint
    px = float(px) + float(order.limit_price_offset)
    return max(0.01, min(0.99, round(px, 4)))


def resolve_limit_price(order: OrderConfig, best: BestBidAsk) -> float:
    if order.limit_price is not None:
        return max(0.01, min(0.99, round(float(order.limit_price), 4)))
    return limit_price_from_book(best, order)


def compute_entry_shares(trading: TradingConfig, price: float, ask_size: float) -> float | None:
    if trading.position_size == "usd":
        target = float(trading.usd) / max(price, 0.01)
    else:
        target = float(trading.shares)
    if ask_size > 0:
        target = min(target, ask_size)
    if target < POLYMARKET_MIN_SHARES:
        return None
    return round(target, 4)


def evaluate_entry(
    *,
    asset: str,
    interval: str,
    slug: str,
    signal: OpportunitySignal,
    tokens: Any,
    orderbook: InMemoryOrderbookStore,
    trading: TradingConfig,
    as_market: bool,
) -> EntryDecision | None:
    side = signal.token_side
    token_id = tokens.up_token_id if side == "up" else tokens.down_token_id

    best = orderbook.get_best_bid_ask(token_id)
    if best is None:
        return None

    order_cfg = trading.order
    if as_market:
        price = best.ask_price
    else:
        price = resolve_limit_price(order_cfg, best)

    shares = compute_entry_shares(trading, price, best.ask_size)
    if shares is None:
        return None

    return EntryDecision(
        asset=asset,
        interval=interval,
        slug=slug,
        side=side,
        token_id=token_id,
        price=price,
        shares=shares,
        size_usd=round(shares * price, 4),
        position_size_mode=trading.position_size,
        best_ask=best.ask_price,
        feed_price=signal.price,
        price_delta_usd=signal.price_delta_usd,
        signal_feed=signal.feed_id,
        reason=signal.reason,
    )
