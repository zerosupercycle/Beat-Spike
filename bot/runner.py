from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp

from bot.config import FEED_IDS, Settings
from bot.decision import EntryDecision, evaluate_entry
from bot.detection import (
    OpportunitySignal,
    PriceDeltaTracker,
    check_price_feed,
    feed_quote_volume,
    feed_volume_delta,
)
from bot.filters import resolve_beat_token_side, resolve_delta_threshold
from bot.risk import evaluate_daily_risk
from bot.executor import execute_with_style
from bot.pm.fast_orders import FastOrderSet, prepare_fast_orders
from bot.feeds.aggregator import FeedAggregator
from bot.feeds.feed_history import slug_epoch_bounds
from bot.history_store import HistoryStore, TradeRecord, is_filled_status, resolve_storage_path, trade_chart_id
from bot.pm.clob_exec import format_order_failure
from bot.pm.orderbook import InMemoryOrderbookStore
from bot.pm.clob_ws import ClobWebSocket
from bot.pm.gamma import fetch_market_tokens
from bot.pm.slug import compute_epoch_slugs, market_timer_str
from bot.startup import run_startup_session

_ROOT = Path(__file__).resolve().parents[1]
CHART_FEEDS = sorted(FEED_IDS)


class BeatSpikeRunner:
    def __init__(self, cfg: Settings, aggregator: FeedAggregator) -> None:
        self.cfg = cfg
        self.aggregator = aggregator
        self.history = HistoryStore(
            resolve_storage_path(cfg.storage.trades_path, _ROOT),
            resolve_storage_path(cfg.storage.status_path, _ROOT),
            resolve_storage_path(cfg.storage.trade_snapshots_dir, _ROOT),
        )
        self._server_offset = 0.0
        self._traded_slugs: set[str] = set()
        self._paper = cfg.bot.mode == "paper"
        self._active_cycles: dict[str, dict[str, Any]] = {}
        self._cycles_completed = 0
        self._buys_triggered = 0

    def _cycle_now(self) -> datetime:
        return datetime.now(tz=UTC) + timedelta(seconds=self._server_offset + self.cfg.bot.time_offset_sec)

    def _cycle_key(self, asset: str, interval: str) -> str:
        return f"{asset.upper()}/{interval}"

    def _in_entry_window(self, now: datetime, epoch_start: datetime, epoch_end: datetime) -> bool:
        elapsed = now.timestamp() - epoch_start.timestamp()
        remaining = epoch_end.timestamp() - now.timestamp()
        return (
            elapsed >= self.cfg.entry.entry_moment_seconds
            and remaining > self.cfg.entry.end_moment_seconds
        )

    async def _capture_trade_chart(
        self,
        asset: str,
        *,
        slug: str,
        interval: str,
        epoch_start: int | None,
        epoch_end: int | None,
        order_ts: float,
        chart_id: str | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "asset": asset,
            "enabled_feeds": CHART_FEEDS,
            "slug": slug,
            "interval": interval,
            "epoch_start": epoch_start,
            "epoch_end": epoch_end,
            "order_ts": order_ts,
        }
        if chart_id:
            kwargs["chart_id"] = chart_id
        fn = getattr(self.aggregator, "capture_trade_chart", None)
        if fn is None:
            return {"series": {}}
        if asyncio.iscoroutinefunction(fn):
            return await fn(**kwargs)
        return fn(**kwargs)

    async def _snap_slug_feed_beats(
        self,
        asset: str,
        slug: str,
        epoch_start_ts: float,
        *,
        interval: str,
    ) -> dict[str, float | None]:
        allow_live = True
        async_fn = getattr(self.aggregator, "snap_slug_feed_beats_async", None)
        sync_fn = getattr(self.aggregator, "snap_slug_feed_beats", None)
        target = async_fn or sync_fn
        if target is None:
            return {}

        sig = inspect.signature(target)
        kwargs: dict[str, Any] = {"interval": interval}
        if "allow_live" in sig.parameters:
            kwargs["allow_live"] = allow_live
        elif "live_snap" in sig.parameters:
            kwargs["live_snap"] = allow_live

        if async_fn is not None:
            return await async_fn(asset, slug, epoch_start_ts, **kwargs)
        result = sync_fn(asset, slug, epoch_start_ts, **kwargs)
        if asyncio.iscoroutine(result):
            return await result
        return result

    def _publish_status(self, state: str, slug: str = "") -> None:
        snap = self.aggregator.snapshot()
        payload: dict[str, Any] = {
            "state": state,
            "mode": self.cfg.bot.mode,
            "slug": slug,
            "slugs": dict(self._active_cycles),
            "cycles_completed": self._cycles_completed,
            "buys_triggered": self._buys_triggered,
            "version": snap.get("version"),
            "feeds": {
                k: v["health"]["state"] for k, v in snap.get("feeds", {}).items()
            },
        }
        self.history.write_status(payload)

    async def _place_order(
        self,
        *,
        key: str,
        cfg: Settings,
        decision: EntryDecision,
        tokens: Any,
        orderbook: InMemoryOrderbookStore,
        trade_chart_ctx: dict[str, Any],
        entered: asyncio.Event,
        fast_orders: FastOrderSet | None = None,
    ) -> str:
        """Return ``filled``, ``cancelled``, or ``failed``."""
        presigned = None
        if fast_orders is not None and fast_orders.enabled:
            await fast_orders.wait_ready()
            presigned = fast_orders.take(decision.side)
        result = await execute_with_style(
            cfg,
            decision,
            paper=self._paper,
            log_prefix=f"[{key}]",
            presigned=presigned,
        )
        status = str(result.get("status", "unknown"))
        detail = str(result.get("detail") or "").strip()
        if is_filled_status(status):
            print(f"  [{key}] [TRADE] {status}" + (f" | {detail[:120]}" if detail else ""))
        elif result.get("cancelled"):
            print(
                f"  [{key}] [TRADE] cancelled — {detail or format_order_failure(result)}; "
                f"continuing to monitor slug"
            )
            return "cancelled"
        else:
            print(f"  [{key}] [TRADE] not filled — {format_order_failure(result)}")
            return "failed"

        entered.set()
        trade_ts = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
        order_ts = datetime.now(tz=UTC).timestamp()
        bounds = slug_epoch_bounds(decision.slug)
        cid = trade_chart_id(trade_ts, decision.slug)
        chart_payload = await self._capture_trade_chart(
            decision.asset,
            slug=decision.slug,
            interval=decision.interval,
            epoch_start=bounds[0] if bounds else None,
            epoch_end=bounds[1] if bounds else None,
            order_ts=order_ts,
            chart_id=cid,
        )
        chart_payload["trade"] = {
            "ts": trade_ts,
            "slug": decision.slug,
            "side": decision.side,
            "price": decision.price,
            "shares": decision.shares,
            "size_usd": decision.size_usd,
            "price_delta_usd": decision.price_delta_usd,
            "signal_feed": decision.signal_feed,
            "feed_price": decision.feed_price,
        }
        trade_chart_ctx["chart_id"] = cid
        trade_chart_ctx["chart"] = chart_payload
        pt_count = sum(
            len(v) for v in (chart_payload.get("series") or {}).values() if isinstance(v, list)
        )
        if pt_count == 0:
            print(
                f"  [{key}] [CHART] warning: empty series — "
                f"restart dashboard server so feed history is active"
            )
        else:
            print(f"  [{key}] [CHART] captured {pt_count} points")

        rec = TradeRecord(
            ts=trade_ts,
            mode=cfg.bot.mode,
            asset=decision.asset,
            interval=decision.interval,
            slug=decision.slug,
            side=decision.side,
            token_id=decision.token_id,
            price=decision.price,
            shares=decision.shares,
            size_usd=decision.size_usd,
            position_size_mode=decision.position_size_mode,
            best_ask=decision.best_ask,
            feed_price=decision.feed_price,
            price_delta_usd=decision.price_delta_usd,
            signal_feed=decision.signal_feed,
            order_style=cfg.trading.order.style,
            order_type=cfg.trading.order.active_order_type(as_market=False),
            status=status,
            decision=decision.reason,
            detail=str(result.get("detail", "")),
            chart_id=cid,
        )
        self.history.append_trade(rec, chart=chart_payload)
        self._buys_triggered += 1
        self._publish_status(f"trade_{decision.asset}_{decision.interval}", decision.slug)
        print(f"  [{key}] [TRADE] recorded → {self.history.trades_path}")
        return "filled"

    async def run_interval_asset(self, asset: str, interval: str) -> None:
        cfg = self.cfg
        now = self._cycle_now()
        slugs = compute_epoch_slugs(asset, interval, now)
        slug = slugs.current_slug
        epoch_start = slugs.current_start
        epoch_end = slugs.epoch_end
        key = self._cycle_key(asset, interval)
        strat = cfg.strategy
        feed_id = strat.price_feed
        asset_params = strat.asset_params(asset)

        if slug in self._traded_slugs:
            return

        self._active_cycles[key] = {
            "slug": slug,
            "asset": asset.lower(),
            "interval": interval,
            "epoch_end": slugs.epoch_end_et,
        }
        self._publish_status("running")
        self.aggregator.reset_epoch()
        await self._snap_slug_feed_beats(
            asset,
            slug,
            epoch_start.timestamp(),
            interval=interval,
        )

        elapsed, left = market_timer_str(now, epoch_start, interval)
        print()
        print("=" * 50)
        print(f"🎯 BEAT SPIKE  {key}  slug={slug}  end={slugs.epoch_end_et}")
        print(f"  ⏱️ {elapsed} | {left}")
        print(
            f"  entry window: +{cfg.entry.entry_moment_seconds}s → "
            f"-{cfg.entry.end_moment_seconds}s before close"
        )
        print(
            f"  detection: {feed_id} |Δ|≥ {asset_params.format_threshold_usd()} "
            f"vs {asset_params.lookback_seconds:.0f}s ago"
            f" · beat cross: +Δ vs −Δ at slug open"
            + (
                f" sustained≥{asset_params.sustain_seconds:.0f}s"
                if asset_params.sustain_seconds > 0
                else ""
            )
        )
        print("=" * 50)

        async with aiohttp.ClientSession() as session:
            tokens = await fetch_market_tokens(session, cfg.api.gamma_url, slug)
        if not tokens:
            print(f"  [{key}] no market for {slug}")
            self._active_cycles.pop(key, None)
            return

        fast_orders_task: asyncio.Task[FastOrderSet] | None = asyncio.create_task(
            prepare_fast_orders(
                cfg,
                slug=slug,
                up_token_id=tokens.up_token_id,
                down_token_id=tokens.down_token_id,
                log_prefix=f"[{key}]",
                paper=self._paper,
            )
        )

        orderbook = InMemoryOrderbookStore()
        tracking = asyncio.Event()
        entered = asyncio.Event()
        attempted = asyncio.Event()
        order_placing = asyncio.Event()
        stop_tasks = asyncio.Event()
        trade_chart_ctx: dict[str, Any] = {}
        order_lock = asyncio.Lock()
        placing_gate = asyncio.Lock()
        order_tasks: set[asyncio.Task[None]] = set()
        tracker = PriceDeltaTracker()
        last_reject = ""

        async def on_book(msg: dict[str, Any]) -> None:
            if tracking.is_set():
                orderbook.apply_book_msg(msg)

        async def _try_signal(signal: OpportunitySignal) -> None:
            nonlocal last_reject
            if entered.is_set() or attempted.is_set():
                return
            async with placing_gate:
                if entered.is_set() or attempted.is_set() or order_placing.is_set():
                    return
                order_placing.set()
            try:
                async with order_lock:
                    if entered.is_set():
                        return
                    as_market_first = cfg.trading.order.style == "market"
                    decision = evaluate_entry(
                        asset=asset,
                        interval=interval,
                        slug=slug,
                        signal=signal,
                        tokens=tokens,
                        orderbook=orderbook,
                        trading=cfg.trading,
                        as_market=as_market_first,
                    )
                    if not decision:
                        sig = f"entry_{signal.token_side}"
                        if last_reject != sig:
                            last_reject = sig
                            print(
                                f"  [{key}] [ORDER] REJECT entry | side={signal.token_side.upper()} "
                                f"no book or size too small"
                            )
                        return

                    last_reject = ""
                    sign = "+" if signal.price_delta_usd >= 0 else ""
                    print(
                        f"  [{key}] [DETECT] PASS {signal.reason} | side={decision.side.upper()} "
                        f"p={decision.price:.4f} ask={decision.best_ask:.4f} sh={decision.shares:.2f} "
                        f"feed_px={signal.price:.2f} Δ={sign}{signal.price_delta_usd:.2f}$"
                    )
                    attempted.set()
                    self._traded_slugs.add(slug)
                    try:
                        fast_orders = await fast_orders_task
                        outcome = await self._place_order(
                            key=key,
                            cfg=cfg,
                            decision=decision,
                            tokens=tokens,
                            orderbook=orderbook,
                            trade_chart_ctx=trade_chart_ctx,
                            entered=entered,
                            fast_orders=fast_orders,
                        )
                        if outcome == "cancelled":
                            attempted.clear()
                            self._traded_slugs.discard(slug)
                    except Exception as exc:
                        print(f"  [{key}] [TRADE] error during execution: {exc}")
                        attempted.clear()
                        self._traded_slugs.discard(slug)
            finally:
                order_placing.clear()

        def _spawn_try_signal(signal: OpportunitySignal) -> None:
            if entered.is_set() or attempted.is_set() or order_placing.is_set():
                return
            task = asyncio.create_task(_try_signal(signal))
            order_tasks.add(task)
            task.add_done_callback(order_tasks.discard)

        async def detection_loop() -> None:
            nonlocal last_reject
            poll_sec = strat.poll_interval_ms / 1000.0
            while not stop_tasks.is_set() and not entered.is_set() and not attempted.is_set():
                if not tracking.is_set():
                    await asyncio.sleep(poll_sec)
                    continue

                now_dt = self._cycle_now()
                if not self._in_entry_window(now_dt, epoch_start, epoch_end):
                    await asyncio.sleep(poll_sec)
                    continue

                snap = self.aggregator.snapshot()
                ok, feeds_msg = check_price_feed(snap, feed_id, asset=asset)
                if not ok:
                    if "price=n/a" not in feeds_msg:
                        sig = f"feed_{feeds_msg[:50]}"
                        if last_reject != sig:
                            last_reject = sig
                            print(f"  [{key}] [DETECT] REJECT feed | {feeds_msg}")
                    await asyncio.sleep(poll_sec)
                    continue

                price = snap["feeds"][feed_id]["assets"][asset.upper()]["price"]
                if price is None:
                    await asyncio.sleep(poll_sec)
                    continue

                lookback = float(asset_params.lookback_seconds or strat.lookback_seconds)
                prices = tracker.prices()
                threshold_up = resolve_delta_threshold(
                    strat,
                    asset,
                    prices,
                    asset_params.delta_threshold_up_usd or 0.0,
                )
                threshold_down = resolve_delta_threshold(
                    strat,
                    asset,
                    prices,
                    asset_params.delta_threshold_down_usd or 0.0,
                )
                signal = tracker.update(
                    time.time(),
                    float(price),
                    volume_delta=feed_volume_delta(snap, feed_id, asset),
                    quote_volume=feed_quote_volume(snap, asset),
                    lookback_seconds=lookback,
                    threshold_up_usd=threshold_up,
                    threshold_down_usd=threshold_down,
                    sustain_seconds=asset_params.sustain_seconds,
                    feed_id=feed_id,
                )
                if signal:
                    beat_px = (
                        snap.get("feeds", {})
                        .get(feed_id, {})
                        .get("assets", {})
                        .get(asset.upper(), {})
                        .get("beat")
                    )
                    beat = resolve_beat_token_side(
                        signal.ref_price,
                        float(price),
                        float(beat_px) if beat_px is not None else None,
                    )
                    if beat.reject:
                        sig = f"beat_{beat.reject[:50]}"
                        if last_reject != sig:
                            last_reject = sig
                            print(f"  [{key}] [DETECT] REJECT beat | {beat.reject}")
                        await asyncio.sleep(poll_sec)
                        continue

                    beat_detail = (
                        f"beat_{beat.cross}(+Δ={beat.plus_delta:.2f} "
                        f"-Δ={beat.minus_delta:.2f} → {beat.token_side.upper()})"
                    )
                    signal = replace(
                        signal,
                        token_side=beat.token_side or signal.token_side,
                        reason=f"{signal.reason} | {beat_detail}",
                    )

                    if cfg.risk.enabled:
                        risk = evaluate_daily_risk(
                            self.history.trades_path,
                            max_daily_deployed_usd=cfg.risk.max_daily_deployed_usd,
                            max_daily_drawdown_pct=cfg.risk.max_daily_drawdown_pct,
                            starting_bankroll_usd=cfg.risk.starting_bankroll_usd,
                            max_trades_per_day=cfg.risk.max_trades_per_day,
                        )
                        if risk.paused:
                            sig = f"risk_{risk.reason[:40]}"
                            if last_reject != sig:
                                last_reject = sig
                                print(f"  [{key}] [RISK] PAUSE — {risk.reason}")
                            await asyncio.sleep(poll_sec)
                            continue

                    if not order_placing.is_set() and not attempted.is_set():
                        _spawn_try_signal(signal)
                await asyncio.sleep(poll_sec)

        ws: ClobWebSocket | None = None
        detect_task: asyncio.Task | None = None
        try:
            ws = ClobWebSocket(cfg.api.ws_url, on_book=on_book)
            await ws.connect([tokens.up_token_id, tokens.down_token_id])
            print(f"  [{key}] 🔗 CLOB WS connected")

            wait_until = epoch_start.timestamp() + cfg.entry.entry_moment_seconds - self._cycle_now().timestamp()
            if wait_until > 0:
                await asyncio.sleep(wait_until)

            ready = await self.aggregator.wait_for_prices(
                asset, [feed_id], timeout_sec=45.0
            )
            if not ready:
                print(f"  [{key}] [FEED] timeout waiting for {feed_id} price")
            else:
                snap = self.aggregator.snapshot()
                px = (snap.get("feeds", {}).get(feed_id, {}).get("assets") or {}).get(
                    asset.upper(), {}
                ).get("price")
                print(f"  [{key}] [FEED] {feed_id} ready px={px}")

            tracker.reset()
            tracking.set()
            detect_task = asyncio.create_task(detection_loop())
            print(f"  [{key}] [DETECT] monitoring {feed_id} delta momentum")

            end_wait = epoch_end.timestamp() - self._cycle_now().timestamp()
            if end_wait > 0:
                await asyncio.sleep(end_wait)

            stop_tasks.set()
            if detect_task:
                await asyncio.gather(detect_task, return_exceptions=True)
            if order_tasks:
                await asyncio.gather(*order_tasks, return_exceptions=True)

            if trade_chart_ctx.get("chart_id"):
                saved = trade_chart_ctx.get("chart") or {}
                bounds = slug_epoch_bounds(slug)
                refreshed = await self._capture_trade_chart(
                    asset,
                    slug=slug,
                    interval=interval,
                    epoch_start=bounds[0] if bounds else None,
                    epoch_end=bounds[1] if bounds else None,
                    order_ts=saved.get("order_ts"),
                    chart_id=trade_chart_ctx["chart_id"],
                )
                if saved.get("trade"):
                    refreshed["trade"] = saved["trade"]
                refreshed["captured_at"] = saved.get("captured_at", refreshed.get("captured_at"))
                refreshed["finalized_at"] = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
                if sum(len(v) for v in refreshed.get("series", {}).values() if v):
                    self.history.save_chart_snapshot(trade_chart_ctx["chart_id"], refreshed)
                last_t = max(
                    (pts[-1]["t"] for pts in refreshed.get("series", {}).values() if pts),
                    default=0.0,
                )
                print(
                    f"  [{key}] [CHART] finalized snapshot → {trade_chart_ctx['chart_id']} "
                    f"(through t={last_t:.0f})"
                )

            print(f"  [{key}] epoch ended")
            self._cycles_completed += 1
        except Exception as exc:
            print(f"  [{key}] cycle error: {exc}")
        finally:
            stop_tasks.set()
            if detect_task:
                detect_task.cancel()
                await asyncio.gather(detect_task, return_exceptions=True)
            if order_tasks:
                await asyncio.gather(*order_tasks, return_exceptions=True)
            if fast_orders_task is not None and not fast_orders_task.done():
                await asyncio.gather(fast_orders_task, return_exceptions=True)
            if ws is not None:
                await ws.disconnect()
            self._active_cycles.pop(key, None)
            self._publish_status("running")

    async def run_forever(self) -> None:
        self._server_offset = await run_startup_session(self.cfg)
        assets = self.cfg.markets.active_assets()
        intervals = [i.lower() for i in self.cfg.markets.intervals]

        await self.aggregator.start(assets)
        self.history.write_status({"state": "running", "mode": self.cfg.bot.mode, "slugs": {}})
        print(f"  Starting cycles: assets={assets} intervals={intervals}")

        async def asset_interval_loop(asset: str, interval: str) -> None:
            label = self._cycle_key(asset, interval)
            while True:
                try:
                    await self.run_interval_asset(asset, interval)
                except Exception as exc:
                    print(f"  [{label}] unexpected cycle error: {exc}")
                self._traded_slugs.clear()
                await asyncio.sleep(2.0)

        try:
            await asyncio.gather(*(asset_interval_loop(a, iv) for a in assets for iv in intervals))
        finally:
            await self.aggregator.stop()
            self._publish_stopped()

    def _publish_stopped(self) -> None:
        self.history.write_status(
            {
                "state": "stopped",
                "mode": self.cfg.bot.mode,
                "slugs": {},
                "detail": "bot exited",
            }
        )
