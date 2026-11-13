import pathlib
import sys

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture()
def mock_app(tmp_path, monkeypatch):
    """An App wired to the mock backend with a throwaway journal."""
    monkeypatch.setenv("TRADE_MCP_BACKEND", "mock")
    monkeypatch.setenv("TRADE_MCP_JOURNAL", str(tmp_path / "journal.sqlite3"))
    monkeypatch.setenv("TRADE_MCP_ALLOW_TRADING", "false")
    monkeypatch.setenv("TRADE_MCP_SYMBOL", "XAUUSD")
    monkeypatch.setenv("TRADE_MCP_TRAINING_BUDGET", "10000")
    monkeypatch.setenv("TRADE_MCP_MAX_LOT", "0.10")
    monkeypatch.setenv("TRADE_MCP_MAX_RISK_PERCENT", "1.0")

    from trade_mcp.app import build_app, reset_app
    from trade_mcp.config import Config

    reset_app()
    app = build_app(Config.from_env(), backend="mock")
    yield app
    app.close()
    reset_app()
