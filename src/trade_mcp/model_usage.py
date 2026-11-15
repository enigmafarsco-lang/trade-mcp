"""Usage accounting and the shared, explicitly activated web test budget.

Only metadata about an actual provider attempt belongs here. Prompt text,
response text, credentials and account identifiers are never accepted by the
ledger helpers. Unknown values stay ``None``/SQL ``NULL``; they are never
converted to zero.

The accounting convention is deliberately explicit. OpenAI-compatible providers
usually report cached input as a subset of input tokens and reasoning as a
subset of output tokens, but this adapter does not assume that for an arbitrary
provider. Set ``TRADE_MCP_LLM_USAGE_CONVENTION=openai_inclusive`` when the
provider follows that convention, ``additive`` when the detail fields are
separate, or leave the default ``unknown``. The convention is stored per row so
a reader can avoid double counting.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

TEST_WINDOW_SECONDS = 300.0
TEST_MAX_ATTEMPTS = 5
MAX_RETRY_AFTER_MS = 30 * 24 * 3600 * 1000

USAGE_CONVENTIONS = {"unknown", "openai_inclusive", "additive"}

BUDGET_MESSAGES = {
    "test_budget_not_activated": "بودجهٔ آزمایش فعال نیست؛ ابتدا آن را به‌صورت صریح فعال کنید.",
    "test_budget_exhausted": "سقف پنج تلاش این پنجرهٔ آزمایشی مصرف شده است؛ پنجرهٔ جدید فقط با فعال‌سازی صریح شروع می‌شود.",
    "test_budget_expired": "پنجرهٔ ۳۰۰ ثانیه‌ای آزمایش پایان یافته است؛ پنجرهٔ جدید را صریحاً فعال کنید.",
    "provider_cooldown": "سرویس مدل تا پایان دورهٔ انتظار اعلام‌شده درخواست جدید نمی‌پذیرد؛ تلاش خودکار انجام نمی‌شود.",
    "test_budget_revoked": "بودجهٔ آزمایش لغو شده است؛ برای ادامه باید پنجرهٔ جدید را صریحاً فعال کنید.",
}

PROVIDER_MESSAGES = {
    "authentication_failed": "احراز هویت provider مدل وب ناموفق بود.",
    "billing_required": "اعتبار provider مدل وب کافی نیست؛ درخواست متوقف شد.",
    "quota_exhausted": "سهمیهٔ provider مدل وب تمام شده است؛ درخواست متوقف شد.",
    "rate_limited": "provider مدل وب موقتاً محدودیت تعداد درخواست دارد.",
    "endpoint_unavailable": "مسیر API provider مدل وب در دسترس نیست.",
    "connection_failed": "ارتباط با provider مدل وب برقرار نشد یا پاسخ معتبر نبود.",
    "invalid_response": "پاسخ provider مدل وب معتبر نبود.",
}


def normalise_convention(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"openai", "openai-compatible", "inclusive", "openai_inclusive"}:
        return "openai_inclusive"
    if text in {"additive", "separate", "additional"}:
        return "additive"
    return "unknown" if text not in USAGE_CONVENTIONS else text


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _first_int(*values: Any) -> int | None:
    for value in values:
        parsed = _as_int(value)
        if parsed is not None:
            return parsed
    return None


def _first_float(*values: Any) -> float | None:
    for value in values:
        parsed = _as_float(value)
        if parsed is not None:
            return parsed
    return None


def _currency(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    if 2 <= len(text) <= 12 and all(ch.isalnum() or ch in {"_", "-"} for ch in text):
        return text
    return None


def unknown_usage(convention: Any = "unknown") -> dict[str, Any]:
    return {
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "cost_amount": None,
        "cost_currency": None,
        "usage_source": "unknown",
        "cost_source": "unknown",
        "accounting_convention": normalise_convention(convention),
    }


def usage_from_response(data: Any, convention: Any = "unknown") -> dict[str, Any]:
    """Extract only usage/cost fields from an OpenAI-compatible response.

    Cached input is kept separate from input and reasoning separate from output.
    No total is computed, so an inclusive convention cannot be double counted.
    Missing fields stay ``None`` and are marked unknown/partial.
    """
    if not isinstance(data, dict):
        return unknown_usage(convention)
    usage = data.get("usage")
    if not isinstance(usage, dict):
        usage = {}

    prompt_details = usage.get("prompt_tokens_details")
    if not isinstance(prompt_details, dict):
        prompt_details = {}
    input_details = usage.get("input_tokens_details")
    if not isinstance(input_details, dict):
        input_details = {}
    completion_details = usage.get("completion_tokens_details")
    if not isinstance(completion_details, dict):
        completion_details = {}
    output_details = usage.get("output_tokens_details")
    if not isinstance(output_details, dict):
        output_details = {}

    input_tokens = _first_int(usage.get("prompt_tokens"), usage.get("input_tokens"))
    cached_input_tokens = _first_int(
        prompt_details.get("cached_tokens"),
        usage.get("cached_tokens"),
        usage.get("prompt_cache_hit_tokens"),
        usage.get("cache_read_input_tokens"),
        input_details.get("cached_tokens"),
    )
    output_tokens = _first_int(usage.get("completion_tokens"), usage.get("output_tokens"))
    reasoning_tokens = _first_int(
        completion_details.get("reasoning_tokens"),
        usage.get("reasoning_tokens"),
        output_details.get("reasoning_tokens"),
    )

    cost_amount = _first_float(
        usage.get("cost"),
        usage.get("total_cost"),
        usage.get("cost_amount"),
        data.get("cost"),
    )
    cost_currency = _currency(
        usage.get("cost_currency") or usage.get("currency") or data.get("cost_currency")
    )

    token_values = (input_tokens, cached_input_tokens, output_tokens, reasoning_tokens)
    known = sum(value is not None for value in token_values)
    usage_source = "provider" if known == 4 else ("partial" if known else "unknown")

    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cost_amount": cost_amount,
        "cost_currency": cost_currency,
        "usage_source": usage_source,
        "cost_source": "provider" if cost_amount is not None else "unknown",
        "accounting_convention": normalise_convention(convention),
    }


class ProviderError(RuntimeError):
    """Safe, classified direct-provider failure; never carries the raw body."""

    def __init__(
        self,
        code: str,
        http_status: int | None = None,
        retry_after_ms: int | None = None,
    ):
        self.code = code
        self.http_status = http_status
        if (
            isinstance(retry_after_ms, int)
            and not isinstance(retry_after_ms, bool)
            and 0 <= retry_after_ms <= MAX_RETRY_AFTER_MS
        ):
            self.retry_after_ms = retry_after_ms
        else:
            self.retry_after_ms = None
        self.message = PROVIDER_MESSAGES.get(code, PROVIDER_MESSAGES["connection_failed"])
        super().__init__(self.message)

    def safe_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "http_status": self.http_status,
        }
        if self.retry_after_ms is not None:
            result["retry_after_ms"] = self.retry_after_ms
        return result


class BudgetDenied(RuntimeError):
    """A provider attempt was not authorised, so it must not reach the network."""

    def __init__(
        self,
        reason: str,
        *,
        retry_after_ms: int | None = None,
        message: str | None = None,
    ):
        self.reason = reason
        self.retry_after_ms = retry_after_ms
        self.message = message or BUDGET_MESSAGES.get(
            reason, "درخواست مدل به دلیل محدودیت بودجهٔ آزمایش مجاز نیست."
        )
        super().__init__(self.message)

    def safe_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.reason,
            "message": self.message,
            "status": "denied",
        }
        if self.retry_after_ms is not None:
            result["retry_after_ms"] = self.retry_after_ms
        return result


def _retry_after_ms(body: str, headers: Any = None) -> int | None:
    header_value = None
    try:
        header_value = headers.get("Retry-After") if headers is not None else None
    except (AttributeError, TypeError):
        header_value = None
    if header_value is not None:
        try:
            seconds = float(str(header_value).strip())
        except (TypeError, ValueError):
            seconds = None
        if seconds is not None and math.isfinite(seconds) and seconds >= 0:
            return min(int(seconds * 1000), MAX_RETRY_AFTER_MS)
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    for key in ("retryAfterMs", "retry_after_ms"):
        value = parsed.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_RETRY_AFTER_MS:
            return value
    return None


def classify_http_error(status: int, body: str, headers: Any = None) -> ProviderError:
    """Classify a bounded response without echoing its body or headers."""
    text = (body or "").lower()
    if status in (401, 403):
        code = "authentication_failed"
    elif status == 402 or any(
        marker in text
        for marker in ("insufficient balance", "insufficient_quota", "billing", "payment required")
    ):
        code = "billing_required"
    elif any(marker in text for marker in ("quota exceeded", "quota_exhausted", "free usage limit")):
        code = "quota_exhausted"
    elif status == 429 or "rate_limit" in text or "rate limited" in text:
        code = "rate_limited"
    elif status == 404:
        code = "endpoint_unavailable"
    else:
        code = "connection_failed"
    return ProviderError(code, status, retry_after_ms=_retry_after_ms(body, headers))


def budget_message(status: dict[str, Any]) -> str:
    state = str(status.get("state") or "not_activated")
    if status.get("reason") == "provider_cooldown":
        return BUDGET_MESSAGES["provider_cooldown"]
    if state == "exhausted":
        return BUDGET_MESSAGES["test_budget_exhausted"]
    if state == "expired":
        return BUDGET_MESSAGES["test_budget_expired"]
    if state == "revoked":
        return BUDGET_MESSAGES["test_budget_revoked"]
    if state == "active":
        if status.get("first_attempt_at") is None:
            return "بودجهٔ آزمایش فعال است؛ پنجرهٔ ۳۰۰ ثانیه‌ای با نخستین درخواست مجاز آغاز می‌شود."
        return "بودجهٔ آزمایش فعال است."
    return BUDGET_MESSAGES["test_budget_not_activated"]
