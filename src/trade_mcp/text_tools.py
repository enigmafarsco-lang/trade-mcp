"""Clean-string MCP facade; preserves the existing app and guarded MT5 execution."""
import functools
import json


def clean_tool(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs) -> str:
        try:
            result = fn(*args, **kwargs)
            return json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (ImportError, ModuleNotFoundError):
            return '{"ok":false,"error":"optional_dependency_unavailable"}'
        except Exception:
            # Provider exceptions may contain URLs, account IDs or credentials.
            return '{"ok":false,"error":"tool_failed","details_withheld":true}'
    wrapped.__annotations__ = {**fn.__annotations__, 'return': str}
    return wrapped


def register_text_tools(mcp):
    from .engines import exchange
    from .engines.strategy import strategy_signal
    from .engines.technical import analyze_ohlcv, candlestick_patterns

    @mcp.tool()
    @clean_tool
    def technical_analysis_text(ohlcv: list[list[float]], provider: str = 'builtin') -> str:
        """RSI14, MACD12/26/9, Supertrend10/3, EMA20/50/200 on supplied closed OHLCV."""
        return analyze_ohlcv(ohlcv, provider)

    @mcp.tool()
    @clean_tool
    def candlestick_patterns_text(ohlcv: list[list[float]]) -> str:
        """Optional TA-Lib Doji, Hammer, Engulfing; no automatic dependency fallback."""
        return candlestick_patterns(ohlcv)

    @mcp.tool()
    @clean_tool
    def strategy_signal_text(ohlcv: list[list[float]], provider: str = 'builtin') -> str:
        """Independent BUY/SELL/HOLD rule agreement, not probability or upstream strategy."""
        return strategy_signal(ohlcv, provider)

    @mcp.tool()
    @clean_tool
    def position_size_text(symbol: str, entry: float, stop_loss: float,
                           risk_percent: float = 0.5) -> str:
        """Use existing MT5 sizing/risk checks; no orders and no invented exchange lot rules."""
        from .app import get_app
        from .engines.risk import size_position
        app = get_app()
        return size_position(app.mt5, app.cfg, symbol, entry, stop_loss, risk_percent)

    @mcp.tool()
    @clean_tool
    def ccxt_quote_text(exchange_id: str, symbol: str) -> str:
        """Fetch CCXT sandbox quote only; unsupported sandbox fails, no mainnet fallback."""
        return exchange.quote(exchange_id, symbol)

    @mcp.tool()
    @clean_tool
    def ccxt_balance_text(exchange_id: str) -> str:
        """Read private sandbox balances using local environment secrets; no orders."""
        return exchange.balance(exchange_id)

    @mcp.tool()
    @clean_tool
    def ccxt_execution_status_text() -> str:
        """Explain why exchange execution cannot bypass MT5 demo ownership/risk gates."""
        return exchange.order_status()

    @mcp.tool()
    @clean_tool
    def mt5_propose_order_text(side: str, symbol: str, stop_loss: float,
                               take_profit: float, risk_percent: float = 0.5) -> str:
        """Create a guarded MT5 proposal; never sends an order by itself."""
        from .server import propose_order
        return propose_order(side=side, symbol=symbol, stop_loss=stop_loss,
                             take_profit=take_profit, risk_percent=risk_percent)

    @mcp.tool()
    @clean_tool
    def mt5_confirm_proposal_text(proposal_id: str) -> str:
        """Existing gated MT5 confirmation; requires explicit authorized proposal."""
        from .server import confirm_proposal
        return confirm_proposal(proposal_id)

    @mcp.tool()
    @clean_tool
    def mt5_status_text() -> str:
        """Read MT5 connection and current trading gate status."""
        from .server import mt5_status
        return mt5_status()

    @mcp.tool()
    @clean_tool
    def mt5_account_text() -> str:
        """Read MT5 account balance/equity using the existing account tool."""
        from .server import account_info
        return account_info()

    @mcp.tool()
    @clean_tool
    def mt5_analyze_market_text(symbol: str = '', timeframes: str = '', bars: int = 400) -> str:
        """Read existing broker multi-timeframe evidence; never invokes a cloud model."""
        from .server import analyze_market
        return analyze_market(symbol=symbol, timeframes=timeframes, bars=min(2000, max(120, bars)))

    @mcp.tool()
    @clean_tool
    def technical_broker_text(symbol: str = '', timeframe: str = 'M15',
                              provider: str = 'builtin') -> str:
        """Analyze up to 400 closed broker bars; no model request or order execution."""
        from .app import get_app
        from .engines.live import analyze_broker
        app = get_app()
        return analyze_broker(app.mt5, app.cfg, symbol, timeframe, provider)
