"""Wiring: one process = one Config + one MT5 connection + one journal."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .bridge.base import BridgeError
from .bridge.client import MT5
from .config import Config, load_dotenv
from .store import Store

LOG = logging.getLogger(__name__)


@dataclass
class App:
    cfg: Config
    mt5: MT5
    store: Store

    def close(self) -> None:
        try:
            self.mt5.close()
        finally:
            self.store.close()


def build_app(cfg: Config | None = None, backend: str | None = None) -> App:
    load_dotenv()
    cfg = cfg or Config.from_env()
    mode = (backend or cfg.backend or "bridge").lower()

    if mode == "mock":
        from .bridge.mock import MockBridge

        bridge = MockBridge()
        LOG.info("using MOCK backend - synthetic data, no broker")
    elif mode == "file":
        from .bridge.file import FileBridge, discover_files_dirs

        files_dir = cfg.files_dir
        if not files_dir:
            candidates = discover_files_dirs()
            if candidates:
                files_dir = str(candidates[0])
                LOG.info("auto-detected MT5 files dir: %s", files_dir)
        if not files_dir:
            raise BridgeError(
                "backend=file needs TRADE_MCP_FILES_DIR pointing at the terminal's "
                "MQL5/Files folder (run `trade-mcp discover` to search for it).",
                code="files_dir_missing",
            )
        bridge = FileBridge(files_dir, timeout=cfg.timeout, stale_after=cfg.file_stale_after)
        bridge.ensure_dirs()
        LOG.info("using FILE backend at %s", bridge.root)
    elif mode in {"native", "mcp"}:
        from .bridge.native import NativeBridge

        if not cfg.mcp_token:
            raise BridgeError(
                "backend=native needs the terminal's MCP key: enable the internal server in "
                "MT5 Tools > Options > MCP, copy the key of the 'terminal' endpoint and export "
                "it as TRADE_MCP_MCP_TOKEN (see docs/NATIVE-MCP.md).",
                code="mcp_token_missing",
            )
        bridge = NativeBridge(
            cfg.mcp_url,
            cfg.mcp_token,
            timeout=max(cfg.timeout, 30.0),
            default_symbol=cfg.default_symbol,
        )
        LOG.info("using NATIVE MCP backend at %s", cfg.mcp_url)
    else:
        from .bridge.server import BridgeServer

        bridge = BridgeServer(cfg.host, cfg.port, cfg.timeout).start()
        LOG.info("bridge listening on %s:%s", cfg.host, cfg.port)

    store = Store(cfg.journal_path)
    return App(cfg=cfg, mt5=MT5(bridge, cfg.default_symbol), store=store)


_APP: App | None = None


def get_app() -> App:
    global _APP
    if _APP is None:
        _APP = build_app()
    return _APP


def reset_app() -> None:
    global _APP
    if _APP is not None:
        _APP.close()
    _APP = None
