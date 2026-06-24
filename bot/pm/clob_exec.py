"""Live CLOB execution with optional time-limit cancel."""

from __future__ import annotations

import asyncio
from typing import Any

from bot.config import Settings
from bot.constants import POLYMARKET_MIN_SHARES

POLYMARKET_MIN_MARKETABLE_BUY_USDC = 1.0


def _build_client(cfg: Settings):
    from py_clob_client_v2.client import ClobClient as PyClobClient
    from py_clob_client_v2.clob_types import ApiCreds

    ex = cfg.execution
    creds = ApiCreds(
        api_key=ex.api_key,
        api_secret=ex.api_secret,
        api_passphrase=ex.api_passphrase,
    )
    return PyClobClient(
        cfg.api.clob_url,
        chain_id=ex.chain_id,
        key=ex.private_key,
        creds=creds,
        signature_type=ex.signature_type,
        funder=ex.funder or None,
    )


def _order_type_from_str(order_type: str):
    from py_clob_client_v2.clob_types import OrderType

    u = (order_type or "GTC").upper()
    if u == "FOK":
        return OrderType.FOK
    if u == "FAK":
        return OrderType.FAK
    if u == "GTD":
        return OrderType.GTD
    return OrderType.GTC


def _is_invalid_signature_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    if "invalid signature" in msg:
        return True
    code = getattr(exc, "status_code", None)
    err = getattr(exc, "error_msg", None)
    if code == 400 and isinstance(err, dict) and err.get("error") == "invalid signature":
        return True
    return False


def _normalize_post_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"status": "unknown", "detail": str(result)[:500]}
    status = str(result.get("status") or result.get("orderStatus") or "unknown")
    order_id = result.get("orderID") or result.get("orderId") or result.get("id")
    err_msg = result.get("errorMsg") or result.get("error") or result.get("message")
    detail = err_msg if err_msg else str(result)[:500]
    return {
        "status": status,
        "order_id": str(order_id) if order_id else "",
        "detail": str(detail)[:500],
        "raw_success": bool(result.get("success")),
        "raw": result,
    }


def is_order_post_success(result: dict[str, Any]) -> bool:
    if result.get("raw_success"):
        return True
    status = str(result.get("status", "")).lower().strip()
    if status in ("matched", "live", "delayed", "unmatched", "filled", "success"):
        return True
    return "match" in status or "fill" in status


def format_order_failure(result: dict[str, Any]) -> str:
    status = result.get("status", "unknown")
    err_type = result.get("error_type")
    detail = str(result.get("detail") or result.get("error") or "").strip()
    parts = [f"status={status!r}"]
    if err_type:
        parts.append(f"type={err_type}")
    if detail:
        parts.append(detail[:400])
    oid = result.get("order_id")
    if oid:
        parts.append(f"order_id={str(oid)[:24]}")
    return " | ".join(parts)


def format_order_post_line(result: dict[str, Any]) -> str:
    status = result.get("status", "unknown")
    oid = str(result.get("order_id") or "")
    oid_part = f" oid={oid[:28]}…" if oid else ""
    detail = str(result.get("detail") or "").strip()
    if detail and detail not in (status, str(result.get("raw", ""))[:80]):
        return f"status={status!r}{oid_part} | {detail[:200]}"
    return f"status={status!r}{oid_part}"


def _create_market_buy(
    client,
    token_id: str,
    amount_usdc: float,
    order_type_str: str,
) -> dict[str, Any]:
    from py_clob_client_v2.clob_types import MarketOrderArgs, PartialCreateOrderOptions
    from py_clob_client_v2.order_builder.constants import BUY

    if amount_usdc < POLYMARKET_MIN_MARKETABLE_BUY_USDC:
        return {
            "status": "error",
            "error_type": "AmountTooSmall",
            "detail": (
                f"market buy USDC ${amount_usdc:.4f} below minimum "
                f"${POLYMARKET_MIN_MARKETABLE_BUY_USDC:.2f}"
            ),
        }

    ot = _order_type_from_str(order_type_str)
    last_exc: BaseException | None = None

    for neg_override in (None, True, False):
        try:
            neg_risk = (
                neg_override if neg_override is not None else client.get_neg_risk(token_id)
            )
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=amount_usdc,
                side=BUY,
                order_type=ot,
            )
            options = PartialCreateOrderOptions(neg_risk=neg_risk)
            signed = client.create_market_order(order_args, options)
            result = client.post_order(signed, order_type=ot)
            out = _normalize_post_result(result)
            out["neg_risk"] = neg_risk
            return out
        except Exception as e:
            last_exc = e
            if _is_invalid_signature_error(e) and neg_override is not False:
                continue
            break

    exc = last_exc or RuntimeError("market buy failed with no exception")
    return {
        "status": "error",
        "error_type": type(exc).__name__,
        "detail": str(exc)[:500],
    }


def sign_limit_buy(
    client,
    token_id: str,
    price: float,
    shares: float,
) -> dict[str, Any]:
    """Sign a limit buy without posting (for presigned fast execution)."""
    from py_clob_client_v2.clob_types import OrderArgs, PartialCreateOrderOptions

    if shares < POLYMARKET_MIN_SHARES:
        return {
            "status": "error",
            "error_type": "SizeTooSmall",
            "detail": f"limit buy shares {shares:.4f} below minimum {POLYMARKET_MIN_SHARES}",
        }

    last_exc: BaseException | None = None
    for neg_override in (None, True, False):
        try:
            neg_risk = (
                neg_override if neg_override is not None else client.get_neg_risk(token_id)
            )
            options = PartialCreateOrderOptions(neg_risk=neg_risk)
            signed = client.create_order(
                OrderArgs(token_id=token_id, price=price, size=shares, side="BUY"),
                options,
            )
            return {
                "status": "signed",
                "signed": signed,
                "neg_risk": neg_risk,
                "token_id": token_id,
                "price": price,
                "shares": shares,
            }
        except Exception as e:
            last_exc = e
            if _is_invalid_signature_error(e) and neg_override is not False:
                continue
            break

    exc = last_exc or RuntimeError("limit sign failed with no exception")
    return {
        "status": "error",
        "error_type": type(exc).__name__,
        "detail": str(exc)[:500],
    }


def post_signed_limit_buy(
    client,
    signed: Any,
    order_type_str: str,
) -> dict[str, Any]:
    ot = _order_type_from_str(order_type_str)
    result = client.post_order(signed, order_type=ot)
    return _normalize_post_result(result)


def sign_limit_buy_for_cfg(
    cfg: Settings,
    token_id: str,
    price: float,
    shares: float,
) -> dict[str, Any]:
    return sign_limit_buy(_build_client(cfg), token_id, price, shares)


def post_signed_limit_buy_for_cfg(
    cfg: Settings,
    signed: Any,
    order_type: str,
) -> dict[str, Any]:
    return post_signed_limit_buy(_build_client(cfg), signed, order_type)


def _create_limit_buy(
    client,
    token_id: str,
    price: float,
    shares: float,
    order_type_str: str,
) -> dict[str, Any]:
    signed_out = sign_limit_buy(client, token_id, price, shares)
    if signed_out.get("status") != "signed":
        return signed_out
    out = post_signed_limit_buy(client, signed_out["signed"], order_type_str)
    out["neg_risk"] = signed_out.get("neg_risk")
    return out


def post_entry_order(
    cfg: Settings,
    token_id: str,
    price: float,
    shares: float,
    order_type: str,
    as_market: bool,
) -> dict[str, Any]:
    client = _build_client(cfg)
    if as_market:
        amount_usdc = round(float(price) * float(shares), 4)
        return _create_market_buy(client, token_id, amount_usdc, order_type)
    return _create_limit_buy(client, token_id, price, shares, order_type)


def cancel_order(cfg: Settings, order_id: str) -> dict[str, Any]:
    from py_clob_client_v2.clob_types import OrderPayload

    client = _build_client(cfg)
    result = client.cancel_order(OrderPayload(orderID=order_id))
    if isinstance(result, dict):
        return result
    return {"status": "unknown", "detail": str(result)[:500]}


def get_order(cfg: Settings, order_id: str) -> dict[str, Any]:
    client = _build_client(cfg)
    result = client.get_order(order_id)
    if not isinstance(result, dict):
        raise RuntimeError(f"get_order returned non-dict: {result!r}")
    return result


async def wait_for_fill_or_timeout(
    cfg: Settings,
    order_id: str,
    *,
    timeout_sec: float,
    poll_sec: float = 0.25,
) -> tuple[str, dict[str, Any]]:
    """Poll order until filled/matched or timeout. Returns (status, last_response)."""
    if not order_id:
        return "timeout", {}

    deadline = asyncio.get_event_loop().time() + timeout_sec
    last: dict[str, Any] = {}
    while asyncio.get_event_loop().time() < deadline:
        try:
            last = await asyncio.to_thread(get_order, cfg, order_id)
        except Exception as e:
            last = {"error": str(e)}
        status = str(last.get("status") or last.get("orderStatus") or "").lower()
        if status in ("matched", "filled", "live_matched"):
            return status, last
        if status in ("cancelled", "canceled"):
            return status, last
        await asyncio.sleep(poll_sec)
    return "timeout", last
