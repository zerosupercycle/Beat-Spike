"""Polymarket crypto \"Price To Beat\" (open price at window start) from event page HTML."""

from __future__ import annotations

import re
from typing import Any

import httpx

GAMMA_URL = "https://gamma-api.polymarket.com"
POLYMARKET_EVENT_URL = "https://polymarket.com/event"

_JSON_PRICE = r"[0-9]+(?:\.[0-9]+)?"


def _interval_query_token(interval: str) -> str:
    return {"5m": "fiveminute", "15m": "fifteen", "1h": "hourly"}[interval]


def _gamma_event_for_slug(client: httpx.Client, slug: str) -> dict[str, Any] | None:
    r = client.get(f"{GAMMA_URL}/events", params={"slug": slug}, timeout=30.0)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list) and data:
        ev = data[0]
        return ev if isinstance(ev, dict) else None
    return None


def _window_from_gamma_event(ev: dict[str, Any]) -> tuple[str, str] | None:
    markets = ev.get("markets") or []
    if not markets:
        return None
    m0 = markets[0] if isinstance(markets[0], dict) else {}
    start = m0.get("eventStartTime") or ev.get("eventStartTime")
    end = m0.get("endDate") or ev.get("endDate")
    if not start or not end:
        return None
    return str(start), str(end)


def _iso_variants(iso: str) -> list[str]:
    s = iso.strip()
    seen: set[str] = set()
    out: list[str] = []
    for cand in (s, s.replace("Z", ".000Z") if s.endswith("Z") and "." not in s else None):
        if cand and cand not in seen:
            seen.add(cand)
            out.append(cand)
    if "." in s and s.endswith("Z"):
        head = s[: s.rindex(".")] + "Z"
        if head not in seen:
            out.append(head)
    return out


def _query_key_anchor(
    html: str,
    pm_ticker: str,
    window_start: str,
    window_end: str,
    interval: str,
) -> int | None:
    tok = _interval_query_token(interval)
    for ws in _iso_variants(window_start):
        for we in _iso_variants(window_end):
            needle = f'"queryKey":["crypto-prices","price","{pm_ticker}","{ws}","{tok}","{we}"]'
            i = html.find(needle)
            if i >= 0:
                return i
    pat = re.compile(
        r'"queryKey":\["crypto-prices","price","'
        + re.escape(pm_ticker)
        + r'","([^"]+)","'
        + re.escape(tok)
        + r'","([^"]+)"\]',
    )
    m = pat.search(html)
    return m.start() if m else None


def _price_near_anchor(html: str, anchor: int) -> str | None:
    before = html[max(0, anchor - 6000) : anchor]
    after = html[anchor : min(len(html), anchor + 8000)]
    open_pat = re.compile(rf'"openPrice"\s*:\s*({_JSON_PRICE})')
    last_before: str | None = None
    for rm in open_pat.finditer(before):
        last_before = rm.group(1)
    if last_before is not None:
        return last_before
    fm = open_pat.search(after)
    if fm:
        return fm.group(1)
    beat_pat = re.compile(rf'"priceToBeat"\s*:\s*({_JSON_PRICE})')
    last_b: str | None = None
    for rm in beat_pat.finditer(before):
        last_b = rm.group(1)
    if last_b is not None:
        return last_b
    bm = beat_pat.search(after)
    return bm.group(1) if bm else None


def _price_from_event_metadata(html: str) -> str | None:
    m = re.search(
        rf'"eventMetadata"\s*:\s*\{{\s*"priceToBeat"\s*:\s*({_JSON_PRICE})',
        html,
    )
    return m.group(1) if m else None


def extract_open_price_from_event_html(
    html: str,
    pm_ticker: str,
    window_start: str,
    window_end: str,
    interval: str,
) -> str | None:
    anchor = _query_key_anchor(html, pm_ticker, window_start, window_end, interval)
    if anchor is not None:
        got = _price_near_anchor(html, anchor)
        if got is not None:
            return got
    return _price_from_event_metadata(html)


def fetch_price_to_beat_for_slug(
    client: httpx.Client,
    slug: str,
    asset: str,
    interval: str,
) -> float | None:
    """Return Polymarket official price-to-beat for a specific market slug."""
    slug_l = slug.strip().lower()
    iv = interval.lower().strip()
    pm_ticker = asset.upper().strip()

    ev = _gamma_event_for_slug(client, slug_l)
    if not ev:
        return None
    win = _window_from_gamma_event(ev)
    if not win:
        return None
    wstart, wend = win

    r = client.get(f"{POLYMARKET_EVENT_URL}/{slug_l}", timeout=45.0)
    r.raise_for_status()
    raw = extract_open_price_from_event_html(r.text, pm_ticker, wstart, wend, iv)
    if raw is None:
        return None
    try:
        px = float(raw)
    except (TypeError, ValueError):
        return None
    return px if px > 0 else None


def fetch_price_to_beat_sync(slug: str, asset: str, interval: str) -> float | None:
    headers = {"User-Agent": "beat-spike/1.0 (+polymarket beat)"}
    with httpx.Client(headers=headers, follow_redirects=True) as client:
        return fetch_price_to_beat_for_slug(client, slug, asset, interval)
