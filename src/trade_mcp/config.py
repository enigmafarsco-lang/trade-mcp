"""Configuration, loaded from TRADE_MCP_* environment variables (and .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

ENV_PREFIX = "TRADE_MCP_"

DEFAULT_SYMBOL = "XAUUSD"
DEFAULT_TIMEFRAMES = ("M15", "H1", "H4")

VALID_TIMEFRAMES = (
    "M1", "M2", "M3", "M4", "M5", "M6", "M10", "M12",
    "M15", "M20", "M30",
    "H1", "H2", "H3", "H4", "H6", "H8", "H12",
    "D1", "W1", "MN1",
)

TF_SECONDS = {
    "M1": 60, "M2": 120, "M3": 180, "M4": 240, "M5": 300, "M6": 360,
    "M10": 600, "M12": 720, "M15": 900, "M20": 1200, "M30": 1800,
    "H1": 3600, "H2": 7200, "H3": 10800, "H4": 14400, "H6": 21600,
    "H8": 28800, "H12": 43200, "D1": 86400, "W1": 604800, "MN1": 2592000,
}


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(ENV_PREFIX + name, default)


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def _env_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _env(name)
    if not raw:
        return default
    items = tuple(x.strip() for x in raw.split(",") if x.strip())
    return items or default


def load_dotenv(path: str | os.PathLike[str] = ".env") -> None:
    """Minimal .env loader (no dependency). Existing env vars win."""
    p = Path(path).expanduser()
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(slots=True)
class Config:
    # transport
    host: str = "127.0.0.1"
    port: int = 8787
    backend: str = "bridge"  # "bridge" (socket) | "file" (shared files) | "native" (MT5 MCP) | "mock"
    timeout: float = 20.0

    # file transport (backend="file")
    files_dir: str | None = None
    file_stale_after: float = 8.0

    # native transport (backend="native"): MetaTrader's own MCP server
    mcp_url: str = "http://127.0.0.1:22346/mcp"
    mcp_token: str = ""
    mcp_marketdata_url: str = "https://www.metatrader.com/mcp"
    mcp_marketdata_token: str = ""

    # local console app + built-in agent
    console_host: str = "127.0.0.1"
    console_port: int = 8790
    opencode_url: str = ""
    opencode_model: str = ""
    opencode_password: str = field(default="", repr=False)
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    news_feeds: tuple[str, ...] = ()

    # instrument defaults
    default_symbol: str = DEFAULT_SYMBOL
    timeframes: tuple[str, ...] = DEFAULT_TIMEFRAMES

    # trading guard rails
    allow_trading: bool = False
    max_lot: float = 0.10
    training_budget: float = 100.0  # shared conservative sizing cap; account currency
    max_risk_percent: float = 1.0
    max_open_positions: int = 3
    max_spread_points: float = 60.0
    allowed_symbols: tuple[str, ...] = ("XAUUSD", "GOLD", "XAU")
    proposal_ttl: int = 180
    deviation_points: int = 30

    # storage
    journal_path: str = "~/.local/share/trade-mcp/journal.sqlite3"

    # extra
    allowed_prefix_suffix_len: int = 3
    raw: dict[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def from_env(cls) -> Config:
        cfg = cls(
            host=_env("HOST", "127.0.0.1") or "127.0.0.1",
            port=_env_int("PORT", 8787),
            backend=(_env("BACKEND", "bridge") or "bridge").strip().lower(),
            timeout=_env_float("TIMEOUT", 20.0),
            files_dir=(_env("FILES_DIR") or "").strip() or None,
            file_stale_after=_env_float("FILE_STALE_AFTER", 8.0),
            mcp_url=(_env("MCP_URL") or "http://127.0.0.1:22346/mcp").strip(),
            mcp_token=(
                (_env("MCP_TOKEN") or "").strip()
                or (os.environ.get("MT5_MCP_TOKEN") or "").strip()
                or (os.environ.get("MT5_MCP_TOKEN_TERMINAL") or "").strip()
            ),
            mcp_marketdata_url=(
                _env("MCP_MARKETDATA_URL") or "https://www.metatrader.com/mcp"
            ).strip(),
            mcp_marketdata_token=(
                (_env("MCP_MARKETDATA_TOKEN") or "").strip()
                or (os.environ.get("MT5_MCP_TOKEN_MARKETDATA") or "").strip()
            ),
            console_host=(_env("CONSOLE_HOST", "127.0.0.1") or "127.0.0.1").strip(),
            console_port=_env_int("CONSOLE_PORT", 8790),
            opencode_url=(_env("OPENCODE_URL") or "").strip(),
            opencode_model=(_env("OPENCODE_MODEL") or "").strip(),
            opencode_password=(_env("OPENCODE_PASSWORD") or os.environ.get("OPENCODE_PASSWORD") or os.environ.get("OPENCODE_SERVER_PASSWORD", "")),
            llm_base_url=(_env("LLM_BASE_URL") or "").strip(),
            llm_api_key=(_env("LLM_API_KEY") or "").strip(),
            llm_model=(_env("LLM_MODEL") or "").strip(),
            news_feeds=_env_list("NEWS_FEEDS", ()),
            default_symbol=(_env("SYMBOL", DEFAULT_SYMBOL) or DEFAULT_SYMBOL).strip(),
            timeframes=tuple(
                tf.upper() for tf in _env_list("TIMEFRAMES", DEFAULT_TIMEFRAMES)
            ),
            allow_trading=_env_bool("ALLOW_TRADING", False),
            max_lot=_env_float("MAX_LOT", 0.10),
            training_budget=_env_float("TRAINING_BUDGET", 100.0),
            max_risk_percent=_env_float("MAX_RISK_PERCENT", 1.0),
            max_open_positions=_env_int("MAX_OPEN_POSITIONS", 3),
            max_spread_points=_env_float("MAX_SPREAD_POINTS", 60.0),
            allowed_symbols=tuple(
                s.upper() for s in _env_list("ALLOWED_SYMBOLS", ("XAUUSD", "GOLD", "XAU"))
            ),
            proposal_ttl=_env_int("PROPOSAL_TTL", 180),
            deviation_points=_env_int("DEVIATION", 30),
            journal_path=str(Path(_env("JOURNAL", "~/.local/share/trade-mcp/journal.sqlite3") or "~/.local/share/trade-mcp/journal.sqlite3").expanduser()),
        )
        if cfg.backend == "mcp":  # friendly alias
            cfg.backend = "native"
        if cfg.backend not in {"bridge", "file", "native", "mock"}:
            cfg.backend = "bridge"
        if cfg.port <= 0:
            cfg.port = 8787
        if cfg.console_port <= 0:
            cfg.console_port = 8790
        return cfg

    def with_overrides(self, **kwargs) -> Config:
        return replace(self, **kwargs)

    @property
    def trading_enabled(self) -> bool:
        return bool(self.allow_trading)

    def is_symbol_allowed(self, symbol: str) -> bool:
        sym = (symbol or "").strip().upper()
        if not sym:
            return False
        for allowed in self.allowed_symbols:
            allowed = allowed.upper()
            if sym == allowed:
                return True
            if sym.startswith(allowed) and len(sym) - len(allowed) <= self.allowed_prefix_suffix_len:
                return True
        return False

    def normalise_timeframes(self, timeframes) -> tuple[str, ...]:
        if timeframes is None:
            return self.timeframes
        if isinstance(timeframes, str):
            timeframes = [t for t in timeframes.replace(";", ",").split(",") if t.strip()]
        out: list[str] = []
        for tf in timeframes:
            tf = str(tf).strip().upper()
            if tf in VALID_TIMEFRAMES and tf not in out:
                out.append(tf)
        return tuple(out) or self.timeframes


def sizing_balance(account: dict, cfg) -> float:
    """Never size against more than the account or the explicit practice cap."""
    import math

    equity = float(account.get("equity") or account.get("balance") or 0)
    budget = float(getattr(cfg, "training_budget", 100))
    if not math.isfinite(budget) or budget <= 0:
        return 0.0
    return max(0.0, min(equity, budget)) if math.isfinite(equity) else 0.0
