"""The probe script: argument parsing and one end-to-end run against the stub."""

from __future__ import annotations

import importlib.util
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from test_mcpclient import TOKEN, _Stub

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mt5_mcp_probe.py"
_spec = importlib.util.spec_from_file_location("mt5_mcp_probe", SCRIPT)
probe = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(probe)


def test_parse_targets():
    assert probe._parse_targets(None) == [("terminal", probe.TERMINAL_URL)]
    assert probe._parse_targets(["terminal=http://127.0.0.1:22346/mcp"])[0][0] == "terminal"
    assert probe._parse_targets(["http://127.0.0.1:22345/mcp"])[0][0] == "metaeditor"
    assert probe._parse_targets(["https://www.metatrader.com/mcp"])[0][0] == "marketdata"
    # a URL with '=' inside is not a NAME=URL pair
    assert probe._parse_targets(["http://host/mcp?a=1"])[0] == ("terminal", "http://host/mcp?a=1")


def test_parse_tokens():
    bare, named = probe._parse_tokens(["abc", "terminal=xyz"])
    assert bare == "abc"
    assert named == {"terminal": "xyz"}
    assert probe._parse_tokens(None)[1] == {}


def test_probe_end_to_end_with_per_server_key(capsys):
    _Stub.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/mcp"
    try:
        rc = probe.main(
            [
                "--url",
                f"terminal={url}",
                "--token",
                f"terminal={TOKEN}",
                "--call",
                "get_account",
                "--dump",
                "/dev/null",
            ]
        )
    finally:
        server.shutdown()
        server.server_close()

    out = capsys.readouterr().out
    assert rc == 0
    assert "key      : given" in out
    assert "handshake" in out
    assert "get_account" in out
    assert "balance" in out
