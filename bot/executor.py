from __future__ import annotations

import asyncio
from typing import Any

from bot.config import Settings, TradingConfig
from bot.decision import EntryDecision
from bot.pm.clob_exec import (
    format_order_failure,
    format_order_post_line,
    is_order_post_success,
    post_signed_limit_buy_for_cfg,
)
from bot.pm.fast_orders import PresignedOrder


def _order_succeeded(result: dict[str, Any]) -> bool:
    status = str(result.get("status", "")).lower()
    if status in ("matched", "filled", "live_matched", "paper_filled", "simulated"):
        return True
    return is_order_post_success(result)


async def execute_entry(
    cfg: Settings,
    decision: EntryDecision,
    *,
    paper: bool,
    trading: TradingConfig | None = None,
    log_prefix: str = "",
    as_market: bool | None = None,
    cancel_after_sec: float | None = None,
    presigned: PresignedOrder | None = None,
) -> dict[str, Any]:
    tr = trading or cfg.trading
    order = tr.order
    market = as_market if as_market is not None else order.style == "market"
    ot = order.active_order_type(as_market=market)
    prefix = f"  {log_prefix} " if log_prefix else "  "
    style_label = "MARKET" if market else "LIMIT"

    if market:
        print(
            f"{prefix}[{style_label}] {ot} buy {decision.side.upper()} "
            f"ask={decision.best_ask:.4f} ~${decision.size_usd:.2f} sh≈{decision.shares:.2f}"
        )
    else:
        print(
            f"{prefix}[{style_label}] {ot} buy {decision.side.upper()} "
            f"@ {decision.price:.4f} ask={decision.best_ask:.4f} sh={decision.shares:.2f}"
        )

    if paper or not cfg.execution.enabled:
        mode = "PAPER" if paper else "SIM"
        if not market and cancel_after_sec:
            sim_oid = f"sim-{decision.token_id[:12]}"
            fast = " presigned" if presigned else ""
            print(f"{prefix}[{mode}] posting{fast} limit to CLOB…")
            print(
                f"{prefix}[{mode}] posted {format_order_post_line({'status': 'live', 'order_id': sim_oid})}"
            )
            if decision.best_ask <= decision.price + 1e-9:
                status = "paper_filled" if paper else "simulated"
                print(f"{prefix}✅ Limit filled on post ({mode}): ask={decision.best_ask:.4f} ≤ limit")
                return {
                    "status": status,
                    "order_id": sim_oid,
                    "detail": f"[{mode}] limit filled on post @ {decision.price} x {decision.shares}",
                }
            print(
                f"{prefix}[{mode}] watching {cancel_after_sec:.1f}s for fill "
                f"(order={sim_oid[:20]}…)"
            )
            await asyncio.sleep(cancel_after_sec)
            print(
                f"{prefix}[{mode}] time limit cancel after {cancel_after_sec:.1f}s — "
                f"unfilled (simulated)"
            )
            return {
                "status": "cancelled",
                "cancelled": True,
                "order_id": sim_oid,
                "detail": f"[{mode}] limit unfilled after {cancel_after_sec:.1f}s — cancelled",
            }

        status = "paper_filled" if paper else "simulated"
        print(f"{prefix}[{mode}] posted → {status}")
        print(f"{prefix}✅ Order {status}: {style_label.lower()} {ot} {decision.side.upper()} @{decision.price}")
        return {
            "status": status,
            "detail": f"[{mode}] {style_label} {ot} {decision.side} "
            f"@{decision.price} x {decision.shares}",
        }

    try:
        from bot.pm.clob_exec import cancel_order, post_entry_order, wait_for_fill_or_timeout
    except ImportError as e:
        err = {"status": "error", "detail": f"clob client unavailable: {e}"}
        print(f"{prefix}[ORDER] post failed — {format_order_failure(err)}")
        return err

    use_presigned = (
        presigned is not None
        and not market
        and presigned.token_id == decision.token_id
        and presigned.signed is not None
    )
    if use_presigned:
        print(
            f"{prefix}[FAST] posting presigned {presigned.order_type} "
            f"buy {decision.side.upper()} @ {presigned.price:.4f} sh={presigned.shares:.2f}"
        )
    else:
        print(f"{prefix}[ORDER] posting {style_label.lower()} {ot} to CLOB…")
    try:
        if use_presigned:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    post_signed_limit_buy_for_cfg,
                    cfg,
                    presigned.signed,
                    presigned.order_type or ot,
                ),
                timeout=30.0,
            )
        else:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    post_entry_order,
                    cfg,
                    decision.token_id,
                    decision.price,
                    decision.shares,
                    ot,
                    market,
                ),
                timeout=30.0,
            )
    except asyncio.TimeoutError:
        result = {
            "status": "error",
            "error_type": "TimeoutError",
            "detail": "CLOB post_order timed out after 30s",
        }
    except Exception as e:
        result = {
            "status": "error",
            "error_type": type(e).__name__,
            "detail": str(e)[:500],
        }

    print(f"{prefix}[ORDER] posted {format_order_post_line(result)}")

    if not _order_succeeded(result):
        print(f"{prefix}[ORDER] post rejected — {format_order_failure(result)}")
        return result

    status = str(result.get("status", "unknown")).lower()
    order_id = str(result.get("order_id") or "")

    if market or not cancel_after_sec:
        if status in ("matched", "filled", "live_matched") or "match" in status:
            print(f"{prefix}✅ Order filled on post: {result.get('status')}")
        return result

    if not order_id:
        print(
            f"{prefix}[LIMIT] skip watch/cancel — no order_id on post "
            f"(status={result.get('status')!r})"
        )
        return result

    if status in ("matched", "filled", "live_matched") or "match" in status:
        print(f"{prefix}✅ Limit filled on post: {result.get('status')}")
        return result

    print(
        f"{prefix}[LIMIT] watching fill for {cancel_after_sec:.1f}s "
        f"(order={order_id[:20]}… status={result.get('status')!r})"
    )
    fill_status, poll = await wait_for_fill_or_timeout(
        cfg, order_id, timeout_sec=cancel_after_sec
    )
    if fill_status in ("matched", "filled", "live_matched"):
        result["status"] = fill_status
        result["detail"] = str(poll)[:500]
        print(f"{prefix}✅ Limit filled during watch: {fill_status}")
        return result

    poll_status = str(poll.get("status") or poll.get("orderStatus") or fill_status)
    print(
        f"{prefix}[LIMIT] no fill after {cancel_after_sec:.1f}s "
        f"(last_status={poll_status!r}) — cancelling"
    )
    try:
        cancel_res = await asyncio.to_thread(cancel_order, cfg, order_id)
        cx = ""
        if isinstance(cancel_res, dict):
            cx = str(cancel_res.get("canceled") or cancel_res.get("status") or "")[:80]
        print(
            f"{prefix}[LIMIT] time limit cancel ok "
            f"(order={order_id[:20]}… {cx})".strip()
        )
    except Exception as e:
        print(f"{prefix}[LIMIT] cancel failed: {e}")
        return {"status": "cancel_failed", "detail": str(e), "order_id": order_id}

    result["status"] = "cancelled"
    result["cancelled"] = True
    result["detail"] = f"unfilled after {cancel_after_sec:.1f}s — cancelled"
    return result


async def execute_with_style(
    cfg: Settings,
    decision: EntryDecision,
    *,
    paper: bool,
    log_prefix: str = "",
    presigned: PresignedOrder | None = None,
) -> dict[str, Any]:
    """Run limit (with cancel), market, or market/limit hybrid per config."""
    tr = cfg.trading
    style = tr.order.style
    cancel_sec = cfg.strategy.time_limit_cancel_seconds

    if style == "market":
        return await execute_entry(
            cfg, decision, paper=paper, log_prefix=log_prefix, as_market=True
        )

    if style == "limit":
        return await execute_entry(
            cfg,
            decision,
            paper=paper,
            log_prefix=log_prefix,
            as_market=False,
            cancel_after_sec=cancel_sec,
            presigned=presigned,
        )

    # market/limit: limit first, market FAK on cancel/timeout
    limit_result = await execute_entry(
        cfg,
        decision,
        paper=paper,
        log_prefix=log_prefix,
        as_market=False,
        cancel_after_sec=cancel_sec,
        presigned=presigned,
    )
    st = str(limit_result.get("status", "")).lower()
    if st in ("matched", "filled", "live_matched", "paper_filled", "simulated"):
        return limit_result

    print(
        f"  {log_prefix} [MARKET/LIMIT] limit not filled ({st}) — "
        f"posting market {tr.order.market_order_type}"
    )
    return await execute_entry(
        cfg, decision, paper=paper, log_prefix=log_prefix, as_market=True
    )
