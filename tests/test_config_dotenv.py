"""load_dotenv must not pollute test environments with a local .env file."""
import os

from trade_mcp.config import load_dotenv


def test_load_dotenv_skipped_under_pytest(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("TRADE_MCP_UNIQUE_TEST_VAR=from_dotenv\n", encoding="utf-8")
    monkeypatch.delenv("TRADE_MCP_UNIQUE_TEST_VAR", raising=False)
    # Even if someone explicitly asks to load, pytest/test runs stay hermetic.
    load_dotenv(env_file)
    assert "TRADE_MCP_UNIQUE_TEST_VAR" not in os.environ


def test_load_dotenv_can_be_explicitly_skipped(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("TRADE_MCP_UNIQUE_TEST_VAR=from_dotenv\n", encoding="utf-8")
    monkeypatch.delenv("TRADE_MCP_UNIQUE_TEST_VAR", raising=False)
    monkeypatch.setenv("TRADE_MCP_SKIP_DOTENV", "1")
    load_dotenv(env_file)
    assert "TRADE_MCP_UNIQUE_TEST_VAR" not in os.environ
