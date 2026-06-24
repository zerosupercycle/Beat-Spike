#!/usr/bin/env python3
"""Beat Spike — Polymarket beat-cross momentum bot."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from bot.config import load_config, validate_for_startup
from bot.feeds.aggregator import FeedAggregator
from bot.log_tee import setup_log_file
from bot.runner import BeatSpikeRunner


async def _run(runner: BeatSpikeRunner) -> None:
    loop = asyncio.get_running_loop()
    main_task = asyncio.create_task(runner.run_forever())

    def _request_stop() -> None:
        if not main_task.done():
            main_task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            # Windows / non-main thread
            signal.signal(sig, lambda *_: _request_stop())

    try:
        await main_task
    except asyncio.CancelledError:
        pass


def main() -> None:
    p = argparse.ArgumentParser(description="Beat Spike Polymarket bot")
    p.add_argument("--config", default=str(_ROOT / "config/default.yaml"))
    args = p.parse_args()

    cfg = load_config(args.config)
    validate_for_startup(cfg)
    log_path = Path(cfg.storage.log_path)
    if not log_path.is_absolute():
        log_path = _ROOT / log_path
    setup_log_file(log_path)

    if cfg.feeds.source == "server":
        from bot.feeds.remote import RemoteFeedClient

        aggregator = RemoteFeedClient(cfg.feeds.server_url)
        print(f"  [FEEDS] using dashboard server {cfg.feeds.server_url}")
    else:
        aggregator = FeedAggregator.from_settings(cfg)
        print("  [FEEDS] local WebSocket feeds (do not run make server concurrently)")
    runner = BeatSpikeRunner(cfg, aggregator)

    try:
        asyncio.run(_run(runner))
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[BOT] stopped")


if __name__ == "__main__":
    main()
