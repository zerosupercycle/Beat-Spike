#!/usr/bin/env python3
"""Monitor Polymarket profiles via RTDS — capture price charts on their buys."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

import httpx

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from bot.monitor.capture import (  # noqa: E402
    _parse_trade,
    build_event_record,
    capture_monitor_chart,
    schedule_finalize,
)
from bot.monitor.config import load_monitor_config, monitor_paths  # noqa: E402
from bot.monitor.profile import MonitorTarget, resolve_targets_sync  # noqa: E402
from bot.monitor.rtds import run_rtds_activity  # noqa: E402
from bot.monitor.store import MonitorStore, monitor_chart_id  # noqa: E402

log = logging.getLogger("monitor")


class ProfileMonitor:
    def __init__(self, cfg_path: str | None = None) -> None:
        self.cfg = load_monitor_config(cfg_path)
        events_path, snapshots_dir, state_path = monitor_paths(self.cfg)
        self.store = MonitorStore(events_path, snapshots_dir, state_path)
        self.targets: list[MonitorTarget] = []
        self._wallet_map: dict[str, MonitorTarget] = {}
        self._stop = asyncio.Event()
        self._finalize_tasks: set[asyncio.Task] = set()

    def reload_targets(self) -> None:
        self.targets = resolve_targets_sync(self.cfg.targets)
        self._wallet_map = {t.proxy_wallet: t for t in self.targets}
        log.info(
            "Monitoring %d profile(s): %s",
            len(self.targets),
            ", ".join(f"@{t.handle}" for t in self.targets),
        )

    def _slug_ok(self, slug: str) -> bool:
        pat = self.cfg.capture.slug_pattern.strip().lower()
        return not pat or pat in slug.lower()

    async def _handle_trade(self, trade: dict[str, Any]) -> None:
        parsed = _parse_trade(trade)
        if not parsed:
            return
        if not self._slug_ok(parsed["slug"]):
            return
        wallet = parsed["proxy_wallet"]
        target = self._wallet_map.get(wallet)
        if not target:
            return
        tx = parsed["transaction_hash"]
        if self.store.has_transaction(tx):
            return

        before = float(self.cfg.capture.before_sec)
        after = float(self.cfg.capture.after_sec)
        chart_id = monitor_chart_id(parsed["timestamp"], parsed["slug"], tx)

        try:
            chart = await capture_monitor_chart(
                server_url=self.cfg.server_url,
                parsed=parsed,
                before_sec=before,
                after_sec=after,
                chart_id=chart_id,
            )
        except Exception as exc:
            log.error(
                "Chart capture failed for @%s %s: %s (is make server running?)",
                target.handle,
                parsed["slug"],
                exc,
            )
            return

        chart["monitor"] = {
            "target": target.label,
            "target_url": target.url,
            "proxy_wallet": wallet,
            "transaction_hash": tx,
            "slug": parsed["slug"],
            "outcome": parsed["outcome"],
            "token_price": parsed["price"],
            "token_size": parsed["size"],
            "usdc_size": parsed["usdc_size"],
        }

        record = build_event_record(
            parsed,
            target_label=target.label,
            target_url=target.url,
            chart_id=chart_id,
        )
        chart["trade"] = {
            "ts": record.ts,
            "side": (parsed["outcome"] or "buy").lower(),
            "price": parsed["price"],
            "shares": parsed["size"],
        }
        self.store.append_event(record, chart=chart)
        log.info(
            "Recorded @%s BUY %s · %s · $%.2f · chart=%s",
            target.handle,
            parsed["slug"],
            parsed["outcome"],
            parsed["usdc_size"],
            chart_id,
        )

        finalize_at = parsed["timestamp"] + after
        task = asyncio.create_task(
            schedule_finalize(
                server_url=self.cfg.server_url,
                chart_id=chart_id,
                finalize_at=finalize_at,
                stop=self._stop,
            )
        )
        self._finalize_tasks.add(task)
        task.add_done_callback(self._finalize_tasks.discard)

    async def _poll_fallback(self) -> None:
        interval = float(self.cfg.poll_fallback_sec)
        data_url = "https://data-api.polymarket.com/activity"
        while not self._stop.is_set():
            await asyncio.sleep(interval)
            if not self.targets:
                continue
            async with httpx.AsyncClient(timeout=20.0) as client:
                for target in self.targets:
                    try:
                        r = await client.get(
                            data_url,
                            params={
                                "user": target.proxy_wallet,
                                "limit": 5,
                                "type": "TRADE",
                                "side": "BUY",
                            },
                        )
                        r.raise_for_status()
                        rows = r.json()
                    except Exception:
                        continue
                    if not isinstance(rows, list):
                        continue
                    for row in rows:
                        if isinstance(row, dict):
                            row.setdefault("proxyWallet", target.proxy_wallet)
                            await self._handle_trade(row)

    async def run(self) -> None:
        if not self.cfg.enabled:
            log.info("Monitor disabled in config")
            return
        self.reload_targets()
        if not self.targets:
            log.error("No valid targets in monitor config")
            return

        poll_task = asyncio.create_task(self._poll_fallback())
        tasks: list[asyncio.Task] = [poll_task]
        if self.cfg.rtds_enabled:
            tasks.append(
                asyncio.create_task(
                    run_rtds_activity(
                        rtds_url=self.cfg.rtds_url,
                        on_trade=self._handle_trade,
                        stop=self._stop,
                        reconnect_min_sec=float(self.cfg.rtds_reconnect_min_sec),
                        reconnect_max_sec=float(self.cfg.rtds_reconnect_max_sec),
                        rate_limit_backoff_sec=float(self.cfg.rtds_rate_limit_backoff_sec),
                    )
                )
            )
        else:
            log.info("RTDS disabled — using Data API poll every %.0fs", self.cfg.poll_fallback_sec)
        log.info(
            "Profile monitor started — ±%.0fs charts via %s",
            self.cfg.capture.before_sec,
            self.cfg.server_url,
        )
        try:
            await asyncio.gather(*tasks)
        finally:
            self._stop.set()
            for t in list(self._finalize_tasks):
                t.cancel()
            await asyncio.gather(*self._finalize_tasks, return_exceptions=True)


def main() -> None:
    p = argparse.ArgumentParser(description="Beat Spike profile monitor (RTDS)")
    p.add_argument("--config", default=str(_ROOT / "config/monitor.yaml"))
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    mon = ProfileMonitor(args.config)
    try:
        asyncio.run(mon.run())
    except KeyboardInterrupt:
        print("\n[MONITOR] stopped")


if __name__ == "__main__":
    main()
