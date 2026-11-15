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
import logging
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from .analysis import analyze_symbol
from .config import Config
from .execution import cancel, confirm, propose_open
from .model_usage import (
    BudgetDenied,
    ProviderError,
    classify_http_error,
    normalise_convention,
    unknown_usage,
    usage_from_response,
)
from .opencode import NoRedirect
from .store import Store

LOG = logging.getLogger(__name__)

GUARDED_MIN_CONFIDENCE = 62
ADVISORY_MIN_CONFIDENCE = 45


def model_signature(cfg: Config) -> tuple[str, str]:
    """Identity of the dedicated web model used by a successful model test."""
    return (str(getattr(cfg, "llm_base_url", "") or ""),
            str(getattr(cfg, "llm_model", "") or ""))


# --------------------------------------------------------------------- llm
@dataclass
class LLMClient:
    """Minimal OpenAI-compatible chat client (urllib, no extra dependency).

    ``mode="web"`` is the fail-closed web-console path: it never falls back to
    OpenCode, requires a journal store for the usage ledger, and consumes the
    server-enforced test budget before every actual provider attempt.
    """

    base_url: str = ""
    api_key: str = field(default="", repr=False)
    model: str = ""
    timeout: float = 60.0
    mode: str = "auto"
    billing_project: str = ""
    usage_convention: str = "unknown"
    store: Store | None = field(default=None, repr=False)
    request_kind: str = "agent_run"
    last_usage: dict[str, Any] | None = None
    last_error: dict[str, Any] | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list, init=False)
    response_model: str = field(default="", init=False)
    response_variant: str = field(default="", init=False)
    attempt_index: int = field(default=0, init=False)
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex, init=False)

    def __post_init__(self) -> None:
        self.mode = "web" if str(self.mode).lower() == "web" else "auto"
        self.usage_convention = normalise_convention(self.usage_convention)

    @classmethod
    def from_config(
        cls,
        cfg: Config,
        mode: str = "auto",
        *,
        store: Store | None = None,
        request_kind: str = "agent_run",
    ) -> LLMClient:
        if str(mode).lower() == "web":
            # Dedicated web path: only the direct OpenAI-compatible settings.
            # A missing configuration stays unconfigured; it never falls back
            # to the development chat's OpenCode provider.
            return cls(
                base_url=cfg.llm_base_url,
                api_key=cfg.llm_api_key,
                model=cfg.llm_model,
                mode="web",
                billing_project=getattr(cfg, "llm_billing_project", ""),
                usage_convention=getattr(cfg, "llm_usage_convention", "unknown"),
                store=store,
                request_kind=request_kind,
            )
        if cfg.opencode_url:
            from .opencode import OpenCodeClient
            return OpenCodeClient(cfg.opencode_url, cfg.opencode_model, cfg.opencode_password, variant=cfg.opencode_variant, free_fallback=cfg.opencode_free_fallback)
        return cls(
            base_url=cfg.llm_base_url,
            api_key=cfg.llm_api_key,
            model=cfg.llm_model,
        )

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    @property
    def provider(self) -> str:
        try:
            return str(urlparse(self.base_url).hostname or "unknown")
        except ValueError:
            return "unknown"

    def _endpoint(self) -> str:
        parsed = urlparse(self.base_url)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query
                or parsed.fragment
                or (parsed.scheme != "https" and not (local and parsed.scheme == "http"))):
            raise RuntimeError(
                "Model base URL must be HTTPS (or loopback HTTP) without credentials, query or fragment."
            )
        url = self.base_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        return url

    def chat(self, messages: list[dict[str, str]], temperature: float = 0.2) -> str:
        self.attempts = []
        self.last_error = None
        self.last_usage = None
        self.response_model = ""
        self.response_variant = ""
        if not self.configured:
            raise RuntimeError("no LLM configured (set TRADE_MCP_LLM_BASE_URL and _MODEL)")
        try:
            url = self._endpoint()
        except RuntimeError:
            # Invalid endpoints are a local configuration problem, not an
            # authorised provider attempt: do not spend budget or write a row.
            error = ProviderError("connection_failed")
            self.last_error = error.safe_dict()
            raise error from None
        if self.mode == "web" and self.store is None:
            raise RuntimeError("web model mode requires a usage ledger")

        attempt_index = 0
        if self.mode == "web":
            reservation = self.store.reserve_model_budget()
            if not reservation.get("allowed"):
                error = BudgetDenied(
                    str(reservation.get("reason") or "test_budget_not_activated"),
                    retry_after_ms=reservation.get("retry_after_ms"),
                )
                self.last_error = error.safe_dict()
                self.attempts.append({
                    "model": self.model, "variant": "", "status": "denied",
                    "error": error.reason,
                })
                raise error
            attempt_index = int(reservation.get("attempt_index") or 0)
            self.attempt_index = attempt_index
        else:
            self.attempt_index += 1
            attempt_index = self.attempt_index

        body = json.dumps(
            {"model": self.model, "messages": messages, "temperature": temperature,
             "stream": False}
        ).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")

        started = time.time()
        usage = unknown_usage(self.usage_convention)
        http_status: int | None = None
        success = False
        error_code: str | None = None
        try:
            with urllib.request.build_opener(NoRedirect).open(
                req, timeout=self.timeout
            ) as resp:  # noqa: S310
                http_status = int(getattr(resp, "status", 200) or 200)
                raw = resp.read(2 * 1024 * 1024).decode("utf-8", "replace")
            data = json.loads(raw)
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            content = message.get("content")
            if isinstance(content, list):  # some providers return content parts
                content = "".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            text = str(content or "").strip()
            if not text:
                raise ProviderError("invalid_response", http_status)
            usage = usage_from_response(data, self.usage_convention)
            self.last_usage = usage
            self.response_model = str(data.get("model") or self.model)
            self.response_variant = ""
            self.attempts.append(
                {"model": self.model, "variant": "", "status": "responded"}
            )
            success = True
            return text
        except urllib.error.HTTPError as exc:
            http_status = int(exc.code)
            raw_error = exc.read(32768).decode("utf-8", "replace")
            error = classify_http_error(http_status, raw_error, exc.headers)
            error_code = error.code
            if self.mode == "web" and error.retry_after_ms is not None:
                self.store.note_model_budget_cooldown(
                    error.retry_after_ms, reason=error.code
                )
            self.last_error = error.safe_dict()
            self.attempts.append({
                "model": self.model, "variant": "", "status": "failed",
                "error": error.code,
            })
            raise error from None
        except ProviderError as exc:
            error_code = exc.code
            self.last_error = exc.safe_dict()
            self.attempts.append({
                "model": self.model, "variant": "", "status": "failed",
                "error": exc.code,
            })
            raise
        except (urllib.error.URLError, TimeoutError, OSError, ValueError,
                json.JSONDecodeError, RuntimeError) as exc:
            error = ProviderError("connection_failed", http_status)
            error_code = error.code
            self.last_error = error.safe_dict()
            self.attempts.append({
                "model": self.model, "variant": "", "status": "failed",
                "error": error.code,
            })
            raise error from None
        finally:
            if self.mode == "web" and self.store is not None:
                payload = {
                    **usage,
                    "request_id": self.request_id,
                    "created_at": started,
                    "request_kind": self.request_kind,
                    "attempt_index": attempt_index,
                    "is_fallback": False,
                    "provider": self.provider,
                    "model": self.model,
                    "variant": "",
                    "billing_project": self.billing_project or None,
                    "ok": success,
                    "error_code": error_code,
                    "http_status": http_status,
                    "latency_ms": int((time.time() - started) * 1000),
                    "retry_after_ms": (
                        self.last_error or {}
                    ).get("retry_after_ms"),
                }
                try:
                    self.store.record_model_usage(payload)
                except Exception as exc:  # noqa: BLE001 - never lose a response over a ledger fault
                    LOG.warning("model usage ledger write failed: %s", type(exc).__name__)

    def chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any] | None:
        self.last_error = None
        try:
            text = self.chat(messages)
        except BudgetDenied:
            raise
        except ProviderError as exc:
            self.last_error = exc.safe_dict()
            return None
        except (urllib.error.URLError, RuntimeError, TimeoutError,
                json.JSONDecodeError, OSError):
            self.last_error = ProviderError("connection_failed").safe_dict()
            return None
        payload = _extract_json(text)
        if not payload:
            self.last_error = ProviderError("invalid_response").safe_dict()
        return payload


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
            "sizing": scenario.get("sizing"),
        },
        "warnings": analysis.get("warnings"),
        "account": {k: (analysis.get("account") or {}).get(k)
                    for k in ("currency", "balance", "equity", "margin_free", "leverage")},
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
            "used_llm": False,
            "model_backend": (
                "openai-compatible"
                if getattr(self.llm, "mode", "auto") == "web" or not self.cfg.opencode_url
                else "opencode"
            ),
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

        if agent.get('require_open_market') and (analysis.get('market') or {}).get('open_estimate') is False:
            decision = {'action': 'wait', 'confidence': 0, 'source': 'market_estimate',
                        'llm_status': 'not_requested',
                        'reason': 'تخمین جلسهٔ بازار بسته است؛ درخواست مدل و پیشنهاد معامله ایجاد نشد.'}
        else:
            decision = self._decide(agent, analysis)
        entry: dict[str, Any] = {
            "symbol": analysis.get("symbol") or symbol,
            "price": (analysis.get("quote") or {}).get("bid"),
            "action": decision.get("action"),
            "confidence": decision.get("confidence"),
            "reason": decision.get("reason"),
            "source": decision.get("source"),
            "model_error": decision.get("model_error"),
            "model_attempts": decision.get("model_attempts", []),
            "response_model": decision.get("response_model"),
            "response_variant": decision.get("response_variant"),
            "wait_reasons": decision.get("wait_reasons", []),
            "llm_status": decision.get("llm_status", "used" if decision.get("source") in {"llm", "opencode", "web"} else "unknown"),
        }
        entry["technical_analysis"] = {
            "summary": analysis.get("summary_fa"),
            "warnings": analysis.get("warnings") or [],
            "scenario": {k: (analysis.get("scenario") or {}).get(k)
                         for k in ("action", "entry_reference", "stop_loss", "take_profit_1", "sizing")},
        }
        report["decisions"].append(entry)

        if decision.get("source") in {"llm", "opencode", "web"}:
            report["used_llm"] = True
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
        payload = proposal.get("payload") or proposal.get("order") or proposal.get("preview") or {}
        entry["preview"] = proposal.get("preview") or {}
        entry["blocked"] = bool(proposal.get("blocked"))
        entry["blockers"] = proposal.get("blockers", [])

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
        entry["stop_loss"] = payload.get("sl", payload.get("stop_loss"))
        entry["take_profit"] = payload.get("tp", payload.get("take_profit"))
        report["proposals"].append({"id": pid, "symbol": entry["symbol"], "side": action})

        if proposal.get("blocked"):
            entry["status"] = "blocked"
            entry["note"] = "; ".join(proposal.get("blockers") or [])
            return entry

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
        llm_mode = getattr(self.llm, "mode", "auto")
        if llm_mode == "web" and not self.llm.configured:
            # The console's web path is fail-closed: no provider means no model
            # decision and no deterministic substitute proposal.
            return {
                "action": "wait", "confidence": 0,
                "source": "model_not_configured", "llm_status": "blocked",
                "reason": "provider اختصاصی وب تنظیم نشده است؛ هیچ درخواست مدل یا تحلیل جایگزینی اجرا نشد.",
            }
        scenario = analysis.get("scenario") or {}
        if self.llm.configured:
            context = compact_context(analysis)
            try:
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
            except BudgetDenied as exc:
                return {
                    "action": "wait", "confidence": 0, "source": "model_budget",
                    "llm_status": "blocked", "model_error": exc.safe_dict(),
                    "model_attempts": getattr(self.llm, "attempts", []),
                    "reason": exc.message,
                }
            if payload and payload.get('action') in {'buy', 'sell', 'wait'}:
                source = ("web" if getattr(self.llm, "mode", "auto") == "web"
                          else "opencode" if self.cfg.opencode_url else "llm")
                result = self._sanitise(payload, scenario, source=source)
                result.update(model_attempts=getattr(self.llm, 'attempts', []),
                              response_model=getattr(self.llm, 'response_model', None),
                              response_variant=getattr(self.llm, 'response_variant', None))
                return result
            if getattr(self.llm, "mode", "auto") == "web":
                # The dedicated web path fails closed. It never turns a failed
                # provider attempt into a rule-based proposal automatically.
                error = getattr(self.llm, 'last_error', None) or ProviderError(
                    'invalid_response'
                ).safe_dict()
                return {
                    "action": "wait", "confidence": 0, "source": "model_error",
                    "model_error": error,
                    "model_attempts": getattr(self.llm, "attempts", []),
                    "llm_status": "failed", "reason": error["message"],
                }
            if self.cfg.opencode_url:
                from .opencode import ModelServiceError
                error = getattr(self.llm, 'last_error', None) or ModelServiceError('invalid_response').safe_dict()
                return {"action": "wait", "confidence": 0, "source": "opencode_error",
                        "model_error": error, "model_attempts": getattr(self.llm, "attempts", []), "llm_status": "failed", "reason": error['message']}

        # Deterministic fallback: trust the scenario engine.
        action = str(scenario.get("action") or "wait").lower()
        confidence = int(scenario.get("confidence") or 0)
        reason = (scenario.get("reason") or analysis.get("summary_fa")
                  or "سیگنال کافی برای ورود وجود ندارد.")
        wait_reasons = []
        if confidence < ADVISORY_MIN_CONFIDENCE:
            action = "wait"
            wait_reasons.append(f"امتیاز تحلیل {confidence} کمتر از حداقل ورود {ADVISORY_MIN_CONFIDENCE} است؛ سناریو به سفارش تبدیل نشد.")
        elif action not in {"buy", "sell"}:
            wait_reasons.append("تحلیل سناریوی ورود خرید یا فروش نداده است.")
        llm_status = "fallback" if self.llm.configured else "not_configured"
        model_note = ("پاسخ معتبر مدل دریافت نشد؛ از قواعد داخلی استفاده شد."
                      if self.llm.configured else "مدل زبانی تنظیم نشده؛ این تحلیل قاعده‌محور است.")
        reason = " | ".join([model_note, *wait_reasons, reason])
        return {
            "action": action,
            "confidence": confidence,
            "stop_loss": scenario.get("stop_loss"),
            "take_profit": scenario.get("take_profit_1"),
            "risk_percent": agent.get("max_risk"),
            "reason": reason,
            "wait_reasons": wait_reasons,
            "llm_status": llm_status,
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
        if getattr(self.llm, "mode", "auto") == "web":
            return ["dedicated web model: explicit human confirmation required"]
        if self.cfg.opencode_url:
            return ["OpenCode: explicit human confirmation required"]
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
