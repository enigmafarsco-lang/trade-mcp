"""Built-in trading agents: constrained decision loops on top of the MCP tools.

An agent is a small, explicit contract:

* which symbols it may touch (subset of ``TRADE_MCP_ALLOWED_SYMBOLS``),
* how much it may risk per trade (``max_risk`` percent, capped by config),
* how big its orders may get (``max_lot``, capped by config),
* when it may trade (optional ``hours`` window),
* how much autonomy it has (``mode``):

  ``advisory``  only ever creates a proposal - a human confirms.
  ``guarded``   auto-confirms, but only when the analysis is clean and confident.
  ``auto``      auto-confirms every proposal it creates inside its own limits.

Even in ``auto`` nothing is sent unless ``TRADE_MCP_ALLOW_TRADING=true``; the
two-step executor is the final gate, exactly like the MCP path.

The decision itself comes from an OpenAI-compatible LLM when one is configured
(``TRADE_MCP_LLM_BASE_URL``/``_MODEL``), and otherwise from the deterministic
scenario engine in :mod:`trade_mcp.analysis` - so the console is useful with no
API key at all.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .analysis import analyze_symbol
from .config import Config
from .execution import cancel, confirm, propose_open
from .store import Store

GUARDED_MIN_CONFIDENCE = 62
ADVISORY_MIN_CONFIDENCE = 45


# --------------------------------------------------------------------- llm
@dataclass
class LLMClient:
    """Minimal OpenAI-compatible chat client (urllib, no extra dependency)."""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout: float = 60.0

    @classmethod
    def from_config(cls, cfg: Config) -> LLMClient:
        return cls(
            base_url=cfg.llm_base_url,
            api_key=cfg.llm_api_key,
            model=cfg.llm_model,
        )

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    def chat(self, messages: list[dict[str, str]], temperature: float = 0.2) -> str:
        if not self.configured:
            raise RuntimeError("no LLM configured (set TRADE_MCP_LLM_BASE_URL and _MODEL)")
        url = self.base_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        body = json.dumps(
            {"model": self.model, "messages": messages, "temperature": temperature}
        ).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", "replace")
        data = json.loads(raw)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):  # some providers return content parts
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return str(content or "").strip()

    def chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any] | None:
        try:
            text = self.chat(messages)
        except (urllib.error.URLError, RuntimeError, TimeoutError, json.JSONDecodeError, OSError):
            return None
        return _extract_json(text)


def _extract_json(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


# ------------------------------------------------------------------ helpers
def _now_in_window(hours: str) -> bool:
    """``hours`` looks like "07:00-19:00" (local time). Empty means 24/7."""
    spec = (hours or "").strip()
    if not spec or "-" not in spec:
        return True
    try:
        start_s, end_s = spec.split("-", 1)
        h1, m1 = (int(x) for x in start_s.strip().split(":"))
        h2, m2 = (int(x) for x in end_s.strip().split(":"))
    except (ValueError, TypeError):
        return True
    now = datetime.now()
    minutes = now.hour * 60 + now.minute
    start, end = h1 * 60 + m1, h2 * 60 + m2
    if start <= end:
        return start <= minutes <= end
    return minutes >= start or minutes <= end  # window wraps midnight


def compact_context(analysis: dict[str, Any]) -> dict[str, Any]:
    """Small JSON blob we can hand to an LLM without burning a fortune."""
    tfs: dict[str, Any] = {}
    for tf, prof in (analysis.get("timeframes") or {}).items():
        trend = prof.get("trend") or {}
        tfs[tf] = {
            "trend": trend.get("direction"),
            "strength": trend.get("strength"),
            "rsi14": prof.get("rsi14"),
            "adx14": prof.get("adx14"),
            "atr": prof.get("atr"),
            "vol": (prof.get("volatility_regime") or {}).get("regime"),
            "close": prof.get("close"),
        }
    scenario = analysis.get("scenario") or {}
    return {
        "symbol": analysis.get("symbol"),
        "quote": analysis.get("quote"),
        "bias": analysis.get("bias"),
        "timeframes": tfs,
        "levels": analysis.get("levels"),
        "scenario": {
            "action": scenario.get("action"),
            "style": scenario.get("style"),
            "entry": scenario.get("entry_reference"),
            "stop": scenario.get("stop_loss"),
            "tp1": scenario.get("take_profit_1"),
            "tp2": scenario.get("take_profit_2"),
            "confidence": scenario.get("confidence"),
            "reason": scenario.get("reason"),
        },
        "warnings": analysis.get("warnings"),
        "account": analysis.get("account"),
    }


def _system_prompt(agent: dict[str, Any]) -> str:
    style = agent.get("style") or "balanced"
    return (
        "You are a disciplined discretionary trader working for a human operator. "
        f"Your style is {style}. You only answer with JSON, no prose, no markdown.\n"
        "Given market context, decide one of: buy, sell, wait.\n"
        'JSON schema: {"action":"buy|sell|wait","confidence":0-100,'
        '"stop_loss":number,"take_profit":number,"risk_percent":number,"reason":"..."}\n'
        "Rules: stop_loss must sit on the protective side of the current price; "
        f"risk_percent must not exceed {agent.get('max_risk')}; "
        "if the setup is unclear, answer wait with confidence and a reason. "
        "Write the reason in Persian (Farsi), one or two short sentences."
    )


# ------------------------------------------------------------------- engine
class AgentEngine:
    def __init__(self, *, cfg: Config, mt5, store: Store, llm: LLMClient | None = None):
        self.cfg = cfg
        self.mt5 = mt5
        self.store = store
        self.llm = llm or LLMClient.from_config(cfg)

    # ------------------------------------------------------------------ run
    def run(self, agent: dict[str, Any]) -> dict[str, Any]:
        started = time.time()
        report: dict[str, Any] = {
            "agent": agent.get("name"),
            "agent_id": agent.get("id"),
            "mode": agent.get("mode"),
            "started_at": started,
            "symbols": [],
            "proposals": [],
            "errors": [],
            "decisions": [],
            "used_llm": bool(self.llm.configured),
        }

        if not _now_in_window(agent.get("hours") or ""):
            report["skipped"] = f"outside the agent's hours window ({agent.get('hours')})"
            self._finish(agent, report)
            return report

        for symbol in agent.get("symbols") or []:
            try:
                entry = self._run_symbol(agent, symbol, report)
                if entry:
                    report["symbols"].append(entry)
            except Exception as exc:  # noqa: BLE001 - one symbol must not kill the run
                report["errors"].append({"symbol": symbol, "error": str(exc)})

        report["summary_fa"] = self._summary_fa(report)
        self._finish(agent, report)
        return report

    def _run_symbol(
        self, agent: dict[str, Any], symbol: str, report: dict[str, Any]
    ) -> dict[str, Any]:
        analysis = analyze_symbol(
            mt5=self.mt5,
            cfg=self.cfg,
            symbol=symbol,
            bars_count=300,
        )
        if not analysis.get("ok"):
            report["errors"].append({"symbol": symbol, "error": analysis.get("error")})
            return {}

        decision = self._decide(agent, analysis)
        entry: dict[str, Any] = {
            "symbol": analysis.get("symbol") or symbol,
            "price": (analysis.get("quote") or {}).get("bid"),
            "action": decision.get("action"),
            "confidence": decision.get("confidence"),
            "reason": decision.get("reason"),
            "source": decision.get("source"),
        }
        report["decisions"].append(entry)

        action = str(decision.get("action") or "wait").lower()
        if action not in {"buy", "sell"}:
            entry["note"] = "بدون معامله"
            return entry

        proposal = propose_open(
            mt5=self.mt5,
            cfg=self.cfg,
            store=self.store,
            symbol=analysis.get("symbol") or symbol,
            side=action,
            risk_percent=min(
                float(decision.get("risk_percent") or agent.get("max_risk") or 0.5),
                float(agent.get("max_risk") or 0.5),
                float(self.cfg.max_risk_percent),
            ),
            stop_loss=decision.get("stop_loss"),
            take_profit=decision.get("take_profit"),
            comment=f"agent:{agent.get('id')}",
        )
        if not proposal.get("ok"):
            entry["note"] = f"proposal refused: {proposal.get('error')} - {proposal.get('message')}"
            return entry

        pid = proposal.get("proposal_id") or proposal.get("id")
        payload = proposal.get("payload") or proposal.get("order") or {}

        # The agent's own lot ceiling is stricter than the global one.
        volume = float(payload.get("volume") or 0.0)
        if volume > float(agent.get("max_lot") or 0.01) + 1e-9 and pid:
            cancel(store=self.store, proposal_id=str(pid))
            entry["note"] = (
                f"cancelled: sized {volume} lot, above the agent's {agent.get('max_lot')} cap"
            )
            return entry

        entry["proposal_id"] = pid
        entry["volume"] = volume
        entry["stop_loss"] = payload.get("sl")
        entry["take_profit"] = payload.get("tp")
        report["proposals"].append({"id": pid, "symbol": entry["symbol"], "side": action})

        mode = str(agent.get("mode") or "advisory").lower()
        blocked_reasons = self._auto_confirm_blockers(agent, analysis, mode)
        if blocked_reasons:
            entry["status"] = "pending"
            entry["note"] = "; ".join(blocked_reasons)
            return entry

        result = confirm(mt5=self.mt5, cfg=self.cfg, store=self.store, proposal_id=str(pid))
        entry["status"] = "executed" if result.get("ok") else "pending"
        entry["note"] = result.get("message") or result.get("error")
        entry["execution"] = result
        return entry

    # -------------------------------------------------------------- decision
    def _decide(self, agent: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
        scenario = analysis.get("scenario") or {}
        if self.llm.configured:
            context = compact_context(analysis)
            payload = self.llm.chat_json(
                [
                    {"role": "system", "content": _system_prompt(agent)},
                    {
                        "role": "user",
                        "content": "Market context JSON:\n"
                        + json.dumps(context, ensure_ascii=False, default=str),
                    },
                ]
            )
            if payload:
                return self._sanitise(payload, scenario, source="llm")

        # Deterministic fallback: trust the scenario engine.
        action = str(scenario.get("action") or "wait").lower()
        confidence = int(scenario.get("confidence") or 0)
        if confidence < ADVISORY_MIN_CONFIDENCE:
            action = "wait"
        return {
            "action": action,
            "confidence": confidence,
            "stop_loss": scenario.get("stop_loss"),
            "take_profit": scenario.get("take_profit_1"),
            "risk_percent": agent.get("max_risk"),
            "reason": scenario.get("reason")
            or analysis.get("summary_fa")
            or "سیگنال کافی برای ورود وجود ندارد.",
            "source": "rules",
        }

    def _sanitise(
        self, payload: dict[str, Any], scenario: dict[str, Any], source: str
    ) -> dict[str, Any]:
        action = str(payload.get("action") or "wait").lower()
        if action not in {"buy", "sell", "wait"}:
            action = "wait"
        try:
            confidence = int(float(payload.get("confidence") or 0))
        except (TypeError, ValueError):
            confidence = 0
        sl = _as_float(payload.get("stop_loss")) or _as_float(scenario.get("stop_loss"))
        tp = _as_float(payload.get("take_profit")) or _as_float(scenario.get("take_profit_1"))
        risk = _as_float(payload.get("risk_percent")) or _as_float(scenario.get("risk_percent"))
        return {
            "action": action,
            "confidence": max(0, min(100, confidence)),
            "stop_loss": sl,
            "take_profit": tp,
            "risk_percent": risk,
            "reason": str(payload.get("reason") or "")[:400],
            "source": source,
        }

    def _auto_confirm_blockers(
        self, agent: dict[str, Any], analysis: dict[str, Any], mode: str
    ) -> list[str]:
        if mode == "advisory":
            return ["mode=advisory: waiting for your confirmation"]
        if not self.cfg.allow_trading:
            return ["TRADE_MCP_ALLOW_TRADING=false: proposal stays pending"]
        if mode == "guarded":
            confidence = int((analysis.get("scenario") or {}).get("confidence") or 0)
            if confidence < GUARDED_MIN_CONFIDENCE:
                return [f"mode=guarded: confidence {confidence} < {GUARDED_MIN_CONFIDENCE}"]

            risky_early = [
                w for w in analysis.get("warnings") or [] if "disagree" in w or "spiking" in w
            ]
            if risky_early:
                return ["mode=guarded: " + "; ".join(risky_early)]
            risky = [w for w in analysis.get("warnings") or [] if "disagree" in w or "spiking" in w]
            if risky:
                return ["mode=guarded: " + "; ".join(risky)]
        return []

    # ---------------------------------------------------------------- output
    def _finish(self, agent: dict[str, Any], report: dict[str, Any]) -> None:
        report["duration_ms"] = int((time.time() - float(report["started_at"])) * 1000)
        if agent.get("id"):
            self.store.mark_agent_run(str(agent["id"]), report)

    def _summary_fa(self, report: dict[str, Any]) -> str:
        lines: list[str] = []
        for item in report.get("decisions") or []:
            action = str(item.get("action") or "wait")
            fa = {"buy": "خرید", "sell": "فروش", "wait": "انتظار"}.get(action, action)
            line = f"{item.get('symbol')}: {fa}"
            if item.get("confidence"):
                line += f" (امتیاز تحلیل {item['confidence']}؛ نه احتمال برد)"
            if item.get("proposal_id"):
                line += f" - پیشنهاد {item['proposal_id']}"
            if item.get("status"):
                line += f" - وضعیت: {item['status']}"
            if item.get("note"):
                line += f" - {item['note']}"
            if item.get("reason"):
                line += f" - دلیل: {item['reason']}"
            lines.append(line)
        for err in report.get("errors") or []:
            lines.append(f"{err.get('symbol')}: خطا - {err.get('error')}")
        if not lines:
            lines.append("هیچ نمادی بررسی نشد.")
        return "\n".join(lines)


def _as_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out or None


def run_agent_once(app, agent_id: str) -> dict[str, Any]:
    """Convenience wrapper used by the console HTTP API and the CLI."""
    agent = app.store.get_agent(agent_id)
    if agent is None:
        return {"ok": False, "error": "unknown_agent", "message": f"no agent {agent_id}"}
    engine = AgentEngine(cfg=app.cfg, mt5=app.mt5, store=app.store)
    report = engine.run(agent)
    return {"ok": True, **report}
