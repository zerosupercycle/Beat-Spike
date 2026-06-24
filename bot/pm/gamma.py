from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import aiohttp


@dataclass
class MarketTokens:
    slug: str
    up_token_id: str
    down_token_id: str
    up_outcome: str
    down_outcome: str


def _parse_tokens(payload: Any) -> tuple[str, str, str, str] | None:
    markets = payload if isinstance(payload, list) else [payload]
    for m in markets:
        if not isinstance(m, dict):
            continue
        outcomes_raw = m.get("outcomes") or m.get("outcome")
        ids_raw = m.get("clobTokenIds") or m.get("clob_token_ids")
        if isinstance(outcomes_raw, str):
            outcomes = json.loads(outcomes_raw)
        else:
            outcomes = list(outcomes_raw or [])
        if isinstance(ids_raw, str):
            ids = json.loads(ids_raw)
        else:
            ids = list(ids_raw or [])
        if len(outcomes) < 2 or len(ids) < 2:
            continue
        up_i, down_i = 0, 1
        for i, o in enumerate(outcomes):
            ol = str(o).lower()
            if "up" in ol or ol == "yes":
                up_i = i
            elif "down" in ol or ol == "no":
                down_i = i
        return str(ids[up_i]), str(ids[down_i]), str(outcomes[up_i]), str(outcomes[down_i])
    return None


async def fetch_market_tokens(session: aiohttp.ClientSession, gamma_url: str, slug: str) -> MarketTokens | None:
    base = gamma_url.rstrip("/")
    url = f"{base}/events?slug={slug}"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
    parsed = _parse_tokens(data)
    if not parsed:
        url2 = f"{base}/markets/slug/{slug}"
        async with session.get(url2, timeout=aiohttp.ClientTimeout(total=30)) as resp2:
            if resp2.status != 200:
                return None
            data2 = await resp2.json()
        parsed = _parse_tokens(data2)
    if not parsed:
        return None
    up_id, down_id, up_o, down_o = parsed
    return MarketTokens(slug=slug, up_token_id=up_id, down_token_id=down_id, up_outcome=up_o, down_outcome=down_o)
