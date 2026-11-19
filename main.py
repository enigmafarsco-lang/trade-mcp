"""Text-only FastMCP entry point; existing CLI/web remain backward compatible.

Run: .venv/bin/python main.py
All registered tools return clean JSON strings through MCP TextContent.
"""
from mcp.server.fastmcp import FastMCP

from trade_mcp.config import load_dotenv
from trade_mcp.text_tools import register_text_tools

mcp = FastMCP('trade-mcp-text', instructions="""
All tools return plain JSON strings. Start MT5 sessions with mt5_status_text.
Use technical_analysis_text/strategy_signal_text for supplied closed OHLCV;
use mt5_analyze_market_text for broker evidence. Never invent prices or usage.
Rule agreement percentages are not probabilities or profit guarantees.
MT5 orders must use mt5_propose_order_text then mt5_confirm_proposal_text,
with explicit authorization for that proposal and all existing execution gates.
Never bypass blocked risk/ownership/cost checks. CCXT is sandbox read-only;
ccxt_execution_status_text explains unsupported exchange order execution.
No automatic model requests, provider rotation, local weights or strategy resets.
Answer Persian users in Persian; keep symbols and numeric fields unchanged.
""".strip())
register_text_tools(mcp)

if __name__ == '__main__':
    load_dotenv()
    mcp.run(transport='stdio')
