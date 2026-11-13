"""CLI entry point.

    trade-mcp serve        # run the MCP server on stdio (this is what OpenCode launches)
    trade-mcp doctor       # diagnose the MT5 <-> Python link, no MCP involved
    trade-mcp bridge       # run only the TCP bridge, watch the EA connect
    trade-mcp --mock serve # same server, synthetic data, no MetaTrader needed
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any

from . import __version__
from .config import ENV_PREFIX, Config

LOG = logging.getLogger("trade_mcp")


def _setup_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        stream=sys.stderr,  # stdout is reserved for the MCP protocol
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _apply_cli_overrides(args: argparse.Namespace) -> None:
    """Push CLI flags into the environment so Config.from_env() sees them."""
    if args.mock:
        os.environ[ENV_PREFIX + "BACKEND"] = "mock"
    if getattr(args, "allow_trading", False):
        os.environ[ENV_PREFIX + "ALLOW_TRADING"] = "true"
    if getattr(args, "symbol", None):
        os.environ[ENV_PREFIX + "SYMBOL"] = args.symbol
    if getattr(args, "port", None):
        os.environ[ENV_PREFIX + "PORT"] = str(args.port)
    if getattr(args, "files_dir", None):
        os.environ[ENV_PREFIX + "FILES_DIR"] = str(args.files_dir)
        os.environ.setdefault(ENV_PREFIX + "BACKEND", "file")


# ------------------------------------------------------------------- commands
def cmd_serve(args: argparse.Namespace) -> int:
    from .server import mcp

    cfg = Config.from_env()
    LOG.info(
        "trade-mcp %s | backend=%s symbol=%s trading=%s",
        __version__,
        cfg.backend,
        cfg.default_symbol,
        "ENABLED" if cfg.allow_trading else "disabled (proposals only)",
    )
    if cfg.allow_trading:
        LOG.warning(
            "TRADING IS ENABLED - confirm_proposal can send real orders (max %.2f lot, %.2f%% risk)",
            cfg.max_lot,
            cfg.max_risk_percent,
        )
    mcp.run(transport="stdio")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from .app import build_app

    cfg = Config.from_env()
    backend = "mock" if args.mock else cfg.backend
    print(f"trade-mcp {__version__} - doctor")
    print(f"  python      : {sys.version.split()[0]}  ({sys.executable})")
    print(f"  backend     : {backend}")
    if backend == "file":
        print(f"  files dir   : {cfg.files_dir or '(auto-detect)'}")
    else:
        print(f"  listen      : {cfg.host}:{cfg.port}")
    print(f"  symbol      : {cfg.default_symbol}  timeframes={','.join(cfg.timeframes)}")
    print(f"  trading     : {'ENABLED' if cfg.allow_trading else 'disabled'}")
    print(f"  journal     : {cfg.journal_path}")
    print("")

    ok = True
    app = None
    try:
        app = build_app(cfg, backend=backend)

        if backend != "mock":
            deadline = time.time() + max(0, args.wait)
            print(f"waiting up to {args.wait}s for the EA to connect", end="", flush=True)
            while time.time() < deadline and not app.mt5.status().get("ea_connected"):
                print(".", end="", flush=True)
                time.sleep(0.5)
            print("")
            status = app.mt5.status()
            if not status.get("ea_connected"):
                print("  [FAIL] no EA connected")
                print("         - is MetaTrader 5 running (Wine or Windows)?")
                print("         - is the trade-mcp EA on a chart and AutoTrading on?")
                if backend == "file":
                    print(f"         - is TradeMCPFile.mq5 on a chart? files dir {status.get('ea_dir')}")
                    print("         - mt5_status/json shows the last heartbeat it wrote")
                else:
                    print(f"         - does the EA point at {cfg.host}:{cfg.port}?")
                    print("         - check the terminal's Experts tab for socket errors")
                return 2
            print(f"  [ OK ] EA connected: {status.get('ea_hello')}")

        ping = app.mt5.ping()
        print(f"  [ OK ] ping: {ping.get('server_time')}")

        term = app.mt5.terminal_info()
        print(
            f"  [ OK ] terminal: build={term.get('build')} "
            f"trade_allowed={term.get('trade_allowed')} connected={term.get('connected')}"
        )

        acct = app.mt5.account_info(fresh=True)
        print(
            f"  [ OK ] account: {acct.get('login')}@{acct.get('server')} "
            f"balance={acct.get('balance')} {acct.get('currency')} "
            f"equity={acct.get('equity')} leverage=1:{acct.get('leverage')}"
        )

        symbol = app.mt5.resolve_symbol(cfg.default_symbol)
        info = app.mt5.symbol_info(symbol, fresh=True)
        print(
            f"  [ OK ] symbol: {symbol} digits={info.get('digits')} "
            f"tick_value={info.get('trade_tick_value')} "
            f"min_lot={info.get('volume_min')} spread={info.get('spread_points')}pt"
        )
        if symbol != cfg.default_symbol:
            print(f"         (resolved from '{cfg.default_symbol}' - broker suffix detected)")

        for tf in cfg.timeframes:
            bars = app.mt5.candles(symbol, tf, 50)
            status_txt = f"{len(bars)} bars, last={bars[-1].get('close') if bars else 'n/a'}"
            if len(bars) < 30:
                ok = False
                print(f"  [FAIL] {tf}: {status_txt}")
            else:
                print(f"  [ OK ] {tf}: {status_txt}")

        positions = app.mt5.positions()
        print(f"  [ OK ] positions: {len(positions)} open, "
              f"floating={round(sum(float(p.get('profit') or 0) for p in positions), 2)}")

        note = app.store.add_note("doctor run ok", kind="system")
        print(f"  [ OK ] journal writable (note #{note['id']})")

        print("")
        print("doctor: OK" if ok else "doctor: problems found")
        return 0 if ok else 1
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] {type(exc).__name__}: {exc}")
        print("")
        print("doctor: FAILED")
        return 1
    finally:
        if app is not None:
            app.close()


def cmd_bridge(args: argparse.Namespace) -> int:
    from .bridge.server import BridgeServer

    cfg = Config.from_env()
    server = BridgeServer(cfg.host, cfg.port, cfg.timeout).start()
    print(f"bridge listening on {cfg.host}:{cfg.port} - attach TradeMCPConnector in MT5", flush=True)
    print("Ctrl-C to stop", flush=True)
    last = None
    try:
        while True:
            st = server.status()
            key = (st.get("ea_connected"), st.get("ea_peer"))
            if key != last:
                if st.get("ea_connected"):
                    print(f"  EA connected from {st.get('ea_peer')} -> {st.get('ea_hello')}", flush=True)
                elif last is not None:
                    print("  EA disconnected", flush=True)
                last = key
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopping")
        return 0
    finally:
        server.close()


def cmd_console(args: argparse.Namespace) -> int:
    """Local web console: tabs for market, agents, news, journal."""
    from .app import build_app
    from .console import ConsoleServer

    app = build_app()
    host = args.host or app.cfg.console_host
    port = args.console_port or app.cfg.console_port
    server = ConsoleServer(app, host=host, port=port, open_browser=not args.no_browser)
    print(f"trade-mcp console {__version__} - {server.url}", flush=True)
    print(f"  backend : {app.cfg.backend}", flush=True)
    print(f"  trading : {'ENABLED' if app.cfg.allow_trading else 'disabled (proposals only)'}", flush=True)
    print("Ctrl-C to stop", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.stop()
        app.close()
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    """Find MQL5/Files directories of Wine/Windows MetaTrader installs."""
    from .bridge.file import discover_files_dirs

    dirs = discover_files_dirs(args.root)
    if not dirs:
        print("no MQL5/Files directory found - pass --root /path/to/wine/prefix")
        return 1
    print(f"{len(dirs)} candidate(s):")
    for path in dirs:
        marker = " (has trade-mcp/)" if (path / "trade-mcp").is_dir() else ""
        print(f"  {path}{marker}")
    print("")
    print(f'export TRADE_MCP_FILES_DIR="{dirs[0]}"')
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    """List the MCP tools/prompts without starting a client."""
    import asyncio

    from .server import mcp

    async def _list() -> list[Any]:
        return await mcp.list_tools()

    tools = asyncio.run(_list())
    print(f"{len(tools)} tools:")
    for tool in sorted(tools, key=lambda t: t.name):
        first = (tool.description or "").strip().splitlines()
        print(f"  - {tool.name}: {first[0] if first else ''}")
    return 0


# ----------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trade-mcp",
        description="MCP server bridging MetaTrader 5 to OpenCode / any MCP client.",
    )
    parser.add_argument("--mock", action="store_true", help="use synthetic data (no MT5 needed)")
    parser.add_argument("--allow-trading", action="store_true", help="force-enable order execution")
    parser.add_argument("--symbol", default=None, help="default symbol (e.g. XAUUSD)")
    parser.add_argument("--port", type=int, default=None, help="bridge listen port")
    parser.add_argument(
        "--files-dir",
        default=None,
        help="MQL5/Files directory of the terminal (implies --backend file)",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("--version", action="version", version=f"trade-mcp {__version__}")

    sub = parser.add_subparsers(dest="cmd")
    p_serve = sub.add_parser("serve", help="run the MCP server on stdio (default)")
    p_serve.set_defaults(func=cmd_serve)

    p_doctor = sub.add_parser("doctor", help="diagnose the MT5 connection")
    p_doctor.add_argument("--wait", type=int, default=15, help="seconds to wait for the EA")
    p_doctor.set_defaults(func=cmd_doctor)

    p_bridge = sub.add_parser("bridge", help="run only the TCP bridge and watch for the EA")
    p_bridge.set_defaults(func=cmd_bridge)

    p_tools = sub.add_parser("tools", help="list the exposed MCP tools")
    p_tools.set_defaults(func=cmd_tools)

    p_console = sub.add_parser("console", help="run the local web console (tabs + agents)")
    p_console.add_argument("--host", default=None, help="bind address (default 127.0.0.1)")
    p_console.add_argument("--console-port", type=int, default=None, help="port (default 8790)")
    p_console.add_argument("--no-browser", action="store_true", help="do not open a browser")
    p_console.set_defaults(func=cmd_console)

    p_discover = sub.add_parser("discover", help="find MQL5/Files directories (file backend)")
    p_discover.add_argument(
        "--root", action="append", default=None, help="extra root to search (repeatable)"
    )
    p_discover.set_defaults(func=cmd_discover)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    _apply_cli_overrides(args)
    if not getattr(args, "func", None):
        args.func = cmd_serve
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
