from __future__ import annotations

from collections import defaultdict
from typing import Any


class BestBidAsk:
    __slots__ = ("bid_price", "bid_size", "ask_price", "ask_size", "spread", "midpoint")

    def __init__(self, bid_price: float, bid_size: float, ask_price: float, ask_size: float):
        self.bid_price = bid_price
        self.bid_size = bid_size
        self.ask_price = ask_price
        self.ask_size = ask_size
        self.spread = round(ask_price - bid_price, 6)
        self.midpoint = round((bid_price + ask_price) / 2, 6)


class InMemoryOrderbookStore:
    def __init__(self) -> None:
        self._bids: dict[str, list[tuple[float, float]]] = defaultdict(list)
        self._asks: dict[str, list[tuple[float, float]]] = defaultdict(list)

    def apply_book_msg(self, data: dict[str, Any]) -> None:
        asset_id = data.get("asset_id", "")
        if not asset_id:
            return
        bids = [(float(b["price"]), float(b["size"])) for b in data.get("bids", [])]
        asks = [(float(a["price"]), float(a["size"])) for a in data.get("asks", [])]
        self._bids[asset_id] = sorted(bids, key=lambda x: -x[0])
        self._asks[asset_id] = sorted(asks, key=lambda x: x[0])

    def get_best_bid_ask(self, asset_id: str) -> BestBidAsk | None:
        bids, asks = self._bids.get(asset_id), self._asks.get(asset_id)
        if not bids or not asks:
            return None
        return BestBidAsk(bids[0][0], bids[0][1], asks[0][0], asks[0][1])
