"""Resolve Polymarket profile URLs/handles to proxy wallet addresses."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

GAMMA_URL = "https://gamma-api.polymarket.com"
_PROFILE_HANDLE_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


@dataclass(frozen=True)
class MonitorTarget:
    label: str
    url: str
    handle: str
    proxy_wallet: str


def _normalize_target(raw: str) -> str:
    s = raw.strip()
    if not s:
        return ""
    if s.startswith("@"):
        return s[1:].strip().lower()
    if "polymarket.com" in s:
        path = urlparse(s).path.strip("/")
        if path.startswith("@"):
            return path[1:].lower()
        return path.split("/")[-1].lower()
    return s.lower()


def profile_url(handle: str) -> str:
    h = handle.strip().lstrip("@").lower()
    return f"https://polymarket.com/@{h}"


def resolve_profile_sync(handle: str, *, gamma_url: str = GAMMA_URL) -> MonitorTarget | None:
    h = _normalize_target(handle)
    if not h or not _PROFILE_HANDLE_RE.match(h):
        return None
    url = profile_url(h)
    headers = {"User-Agent": "beat-spike-monitor/1.0"}
    with httpx.Client(headers=headers, follow_redirects=True, timeout=30.0) as client:
        r = client.get(
            f"{gamma_url.rstrip('/')}/public-search",
            params={"q": h, "search_profiles": "true"},
        )
        r.raise_for_status()
        data = r.json()
        profiles = data.get("profiles") if isinstance(data, dict) else None
        if isinstance(profiles, list):
            for row in profiles:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("name") or "").strip().lower()
                wallet = str(row.get("proxyWallet") or "").strip().lower()
                if name == h and wallet.startswith("0x") and len(wallet) == 42:
                    return MonitorTarget(label=name, url=url, handle=h, proxy_wallet=wallet)
        page = client.get(url)
        page.raise_for_status()
        m = re.search(r'"proxyWallet"\s*:\s*"(0x[a-fA-F0-9]{40})"', page.text)
        if m:
            return MonitorTarget(
                label=h,
                url=url,
                handle=h,
                proxy_wallet=m.group(1).lower(),
            )
    return None


def resolve_targets_sync(raw_targets: list[str]) -> list[MonitorTarget]:
    out: list[MonitorTarget] = []
    seen: set[str] = set()
    for raw in raw_targets:
        t = resolve_profile_sync(raw)
        if t and t.proxy_wallet not in seen:
            seen.add(t.proxy_wallet)
            out.append(t)
    return out
