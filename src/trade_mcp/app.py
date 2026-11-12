"""Wiring: one process = one Config + one MT5 connection + one journal."""

from __future__ import annotations

import logging
from dataclasses import dataclass

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
