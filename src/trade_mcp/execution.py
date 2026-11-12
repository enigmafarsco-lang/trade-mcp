"""Two-step order flow: propose -> human confirms -> execute.

Nothing here talks to a broker until ``confirm`` is called, and ``confirm``
refuses while ``TRADE_MCP_ALLOW_TRADING`` is false. That is the whole point of
the design: the model can analyse and propose freely, but a human owns the
trigger.
"""

from __future__ import annotations

import time
from typing import Any

from .bridge.base import BridgeError
from .config import Config
from .risk import (
    has_blockers,
    lots_for_risk,
    normalise_volume,
    validate_order,
)
from .store import Store

MAX_DRIFT_R = 1.0  # refuse to fill if price has moved more than 1R since proposing


def _market_price(mt5, symbol: str, side: str) -> float:
    tick = mt5.tick(symbol)
    if side == "buy":
        return float(tick.get("ask") or tick.get("bid") or 0.0)
    return float(tick.get("bid") or tick.get("ask") or 0.0)


def _blockers(issues: list[dict[str, str]]) -> list[str]:
    return [f"{i['code']}: {i['message']}" for i in issues if i.get("severity") == "blocker"]


def propose_open(
    *,
    mt5,
    cfg: Config,
    store: Store,
    symbol: str | None = None,
    side: str = "buy",
    volume: float | None = None,
    risk_percent: float | None = None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    comment: str = "trade-mcp",
) -> dict[str, Any]:
    side = str(side).lower()
    if side not in {"buy", "sell"}:
        return {"ok": False, "error": "bad_side", "message": "side must be 'buy' or 'sell'"}

    symbol = mt5.resolve_symbol(symbol or cfg.default_symbol)
    info = mt5.symbol_info(symbol)
    digits = int(float(info.get("digits") or 2))
    entry = _market_price(mt5, symbol, side)
    account = mt5.account_info(fresh=True)
    positions = mt5.positions()

    if stop_loss is None and volume is None:
        return {
            "ok": False,
            "error": "missing_stop_loss",
            "message": "provide stop_loss (and optionally risk_percent) so the volume can be sized",
        }

    sizing: dict[str, Any] | None = None
    if volume is None:
        sizing = lots_for_risk(
            balance=float(account.get("equity") or account.get("balance") or 0.0),
            risk_percent=(cfg.max_risk_percent if risk_percent is None else float(risk_percent)),
            entry=entry,
            stop=float(stop_loss),
            info=info,
            max_lot=cfg.max_lot,
        )
        if not sizing.get("ok"):
            return {"ok": False, "error": "sizing_failed", "message": sizing.get("error")}
        volume = float(sizing["volume"])
    volume, vol_warnings = normalise_volume(float(volume), info)

    if take_profit is None and stop_loss:
        risk = abs(entry - float(stop_loss))
        take_profit = entry + (1.5 * risk if side == "buy" else -1.5 * risk)

    margin = None
    try:
        margin = float(mt5.calc_order(symbol, side, volume).get("margin") or 0.0)
    except BridgeError:
        margin = None

    issues = validate_order(
        cfg=cfg,
        info=info,
        symbol=symbol,
        side=side,
        volume=volume,
        entry=entry,
        stop=float(stop_loss or 0.0),
        take_profit=float(take_profit or 0.0),
        account=account,
        open_positions=positions,
        margin_required=margin,
    )
    for w in (sizing or {}).get("warnings", []) + vol_warnings:
        issues.append({"severity": "warning", "code": "sizing", "message": w})

    payload = {
        "kind": "open",
        "symbol": symbol,
        "side": side,
        "volume": volume,
        "sl": round(float(stop_loss), digits) if stop_loss else 0.0,
        "tp": round(float(take_profit), digits) if take_profit else 0.0,
        "reference_price": entry,
        "risk_per_unit": abs(entry - float(stop_loss)) if stop_loss else None,
        "comment": comment,
        "digits": digits,
        "sizing": sizing,
    }
    proposal = store.create_proposal(payload, ttl=cfg.proposal_ttl)
    blocked = has_blockers(issues)

    return {
        "ok": True,
        "proposal_id": proposal["id"],
        "expires_in_seconds": int(proposal["expires_at"] - time.time()),
        "blocked": blocked,
        "blockers": _blockers(issues),
        "issues": issues,
        "preview": {
            "kind": "open",
            "symbol": symbol,
            "side": side,
            "volume": volume,
            "type": "market",
            "estimated_entry": entry,
            "stop_loss": payload["sl"] or None,
            "take_profit": payload["tp"] or None,
            "risk_per_unit": payload["risk_per_unit"],
            "estimated_margin": margin,
            "risk_amount": (sizing or {}).get("actual_risk"),
            "risk_percent_of_equity": (
                round(
                    float((sizing or {}).get("actual_risk") or 0)
                    / float(account.get("equity") or 1)
                    * 100,
                    2,
                )
                if sizing
                else None
            ),
            "trading_enabled": cfg.allow_trading,
        },
        "next_step": (
            "BLOCKED - fix the blockers before asking for confirmation."
            if blocked
            else "Show this preview to the user. Only after they say yes, call "
            f"confirm_proposal('{proposal['id']}')."
        ),
    }


def propose_manage(
    *,
    mt5,
    cfg: Config,
    store: Store,
    action: str = "close",
    ticket: int = 0,
    sl: float | None = None,
    tp: float | None = None,
    volume: float | None = None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    comment: str = "trade-mcp-manage",
) -> dict[str, Any]:
    action = str(action).lower()
    if action not in {"close", "modify"}:
        return {"ok": False, "error": "bad_action", "message": "action must be 'close' or 'modify'"}
    sl = sl if sl is not None else stop_loss
    tp = tp if tp is not None else take_profit

    positions = mt5.positions()
    target = next((p for p in positions if int(p.get("ticket") or 0) == int(ticket)), None)
    if target is None:
        return {
            "ok": False,
            "error": "position_not_found",
            "message": f"no open position with ticket {ticket}",
            "open_tickets": [p.get("ticket") for p in positions],
        }

    symbol = target.get("symbol") or ""
    info = mt5.symbol_info(symbol)
    digits = int(float(info.get("digits") or 2))
    side = "buy" if str(target.get("type", "")).lower() in {"buy", "0"} else "sell"
    entry = float(target.get("price_open") or 0.0)
    current = float(target.get("price_current") or 0.0)

    issues: list[dict[str, str]] = []
    if not cfg.allow_trading:
        issues.append(
            {
                "severity": "blocker",
                "code": "trading_disabled",
                "message": "TRADE_MCP_ALLOW_TRADING is false - cannot change positions",
            }
        )

    payload: dict[str, Any] = {
        "kind": action,
        "ticket": int(ticket),
        "symbol": symbol,
        "side": side,
        "digits": digits,
        "comment": comment,
    }
    volume_out = float(target.get("volume") or 0.0)

    if action == "close":
        volume_out = float(volume or target.get("volume") or 0.0)
        if volume_out <= 0 or volume_out > float(target.get("volume") or 0):
            return {"ok": False, "error": "bad_volume", "message": "invalid close volume"}
        payload["volume"] = volume_out
    else:
        new_sl = float(sl or 0.0)
        new_tp = float(tp or 0.0)
        if not new_sl and not new_tp:
            return {
                "ok": False,
                "error": "nothing_to_change",
                "message": "provide sl and/or tp",
            }
        if new_sl:
            # widening a stop increases risk -> treat as a hard error
            current_sl = float(target.get("sl") or 0.0)
            widening = bool(current_sl) and (
                (side == "buy" and new_sl < current_sl) or (side == "sell" and new_sl > current_sl)
            )
            if widening:
                issues.append(
                    {
                        "severity": "blocker",
                        "code": "stop_widening",
                        "message": "this move widens the stop loss and increases risk",
                    }
                )
            wrong_side = (side == "buy" and new_sl >= current) or (side == "sell" and new_sl <= current)
            if wrong_side:
                issues.append(
                    {
                        "severity": "blocker",
                        "code": "stop_wrong_side",
                        "message": "stop loss is on the wrong side of the current price",
                    }
                )
        payload["sl"] = round(new_sl, digits) if new_sl else 0.0
        payload["tp"] = round(new_tp, digits) if new_tp else 0.0
        payload["volume"] = volume_out

    proposal = store.create_proposal(payload, ttl=cfg.proposal_ttl)
    blocked = has_blockers(issues)
    return {
        "ok": True,
        "proposal_id": proposal["id"],
        "expires_in_seconds": int(proposal["expires_at"] - time.time()),
        "blocked": blocked,
        "blockers": _blockers(issues),
        "issues": issues,
        "preview": {
            "kind": action,
            "ticket": int(ticket),
            "symbol": symbol,
            "side": side,
            "open_volume": target.get("volume"),
            "close_volume": volume_out if action == "close" else None,
            "current_price": current,
            "entry_price": entry,
            "current_sl": target.get("sl"),
            "current_tp": target.get("tp"),
            "new_sl": payload.get("sl"),
            "new_tp": payload.get("tp"),
            "floating_profit": target.get("profit"),
        },
        "next_step": (
            "BLOCKED - fix the blockers first."
            if blocked
            else f"Confirm with the user, then call confirm_proposal('{proposal['id']}')."
        ),
    }


def cancel(*, store: Store, proposal_id: str) -> dict[str, Any]:
    prop = store.get_proposal(proposal_id)
    if prop is None:
        return {"ok": False, "error": "unknown_proposal", "message": f"no proposal {proposal_id}"}
    if prop["status"] != "pending":
        return {"ok": False, "error": "not_pending", "message": f"proposal is {prop['status']}"}
    store.mark_proposal(proposal_id, "cancelled")
    return {"ok": True, "proposal_id": proposal_id, "status": "cancelled"}


def confirm(
    *, mt5, cfg: Config, store: Store, proposal_id: str, deviation: int | None = None
) -> dict[str, Any]:
    prop = store.get_proposal(proposal_id)
    if prop is None:
        return {"ok": False, "error": "unknown_proposal", "message": f"no proposal {proposal_id}"}
    if prop["status"] != "pending":
        return {
            "ok": False,
            "error": "not_pending",
            "message": f"proposal {proposal_id} is already {prop['status']}",
        }
    if prop["expired"]:
        store.mark_proposal(proposal_id, "expired")
        return {
            "ok": False,
            "error": "expired",
            "message": "the proposal expired - re-run the analysis and propose again",
        }
    if not cfg.allow_trading:
        return {
            "ok": False,
            "error": "trading_disabled",
            "message": "TRADE_MCP_ALLOW_TRADING is false. Re-enable it and restart the MCP "
            "server before confirming real orders.",
        }

    payload = prop["payload"]
    kind = payload.get("kind")
    dev = int(deviation if deviation is not None else cfg.deviation_points)

    try:
        if kind == "open":
            symbol = payload["symbol"]
            side = payload["side"]
            current = _market_price(mt5, symbol, side)
            reference = float(payload.get("reference_price") or current)
            risk_unit = float(payload.get("risk_per_unit") or 0.0)
            drift = abs(current - reference)
            if risk_unit and drift > MAX_DRIFT_R * risk_unit:
                return {
                    "ok": False,
                    "error": "price_moved",
                    "message": (
                        f"price moved {drift:.5f} since the proposal ({(drift / risk_unit):.2f}R). "
                        "Re-run analyze_market and propose a fresh order."
                    ),
                    "current_price": current,
                    "reference_price": reference,
                }
            result = mt5.open_order(
                symbol=symbol,
                side=side,
                volume=float(payload["volume"]),
                sl=float(payload.get("sl") or 0.0),
                tp=float(payload.get("tp") or 0.0),
                comment=payload.get("comment") or "trade-mcp",
                deviation=dev,
            )
            retcode = int(result.get("retcode") or 0) if isinstance(result, dict) else 0
            if retcode not in (0, 10008, 10009):
                store.mark_proposal(proposal_id, "failed", result)
                return {
                    "ok": False,
                    "error": "broker_rejected",
                    "message": f"MT5 returned retcode {retcode}: {result.get('retcode_text')}",
                    "result": result,
                }
            store.mark_proposal(proposal_id, "executed", result)
            store.record_trade(proposal_id, payload, result)
            return {
                "ok": True,
                "proposal_id": proposal_id,
                "executed": True,
                "result": result,
                "message": "order accepted by MetaTrader",
            }

        if kind == "close":
            result = mt5.close_position(
                ticket=int(payload["ticket"]), volume=float(payload.get("volume") or 0), deviation=dev
            )
            store.mark_proposal(proposal_id, "executed", result)
            store.record_trade(proposal_id, payload, result)
            return {"ok": True, "proposal_id": proposal_id, "executed": True, "result": result}

        if kind == "modify":
            result = mt5.modify_position(
                ticket=int(payload["ticket"]),
                sl=float(payload.get("sl") or 0),
                tp=float(payload.get("tp") or 0),
            )
            store.mark_proposal(proposal_id, "executed", result)
            store.record_trade(proposal_id, payload, result)
            return {"ok": True, "proposal_id": proposal_id, "executed": True, "result": result}

        return {"ok": False, "error": "unknown_kind", "message": f"unsupported proposal kind {kind}"}
    except BridgeError as exc:
        store.mark_proposal(proposal_id, "failed", exc.to_dict())
        return {"ok": False, "error": exc.code, "message": exc.message, "executed": False}
