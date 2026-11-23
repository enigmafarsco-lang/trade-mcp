#!/usr/bin/env python3
"""Run the bundled, unmodified Day Trader tests in a disposable offline cwd.

The reference suite contains public upstream stock fixtures, not MT5 data.
No connector, network, workstation state, or trading credentials are used.
"""
from __future__ import annotations

import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path

UPSTREAM = Path(__file__).resolve().parents[1] / "third_party" / "day-trader-skill" / "upstream"


def _offline(*_args, **_kwargs):
    raise OSError("network disabled during bundled upstream tests")


def main() -> int:
    tests = UPSTREAM / "tests"
    if not tests.is_dir():
        print("Bundled upstream tests are missing", file=sys.stderr)
        return 2
    # Enforce offline behavior even if a future reference test unexpectedly tries
    # to use an unmocked HTTP client. Tests that mock HTTP still work normally.
    socket.socket.connect = _offline
    socket.socket.connect_ex = _offline
    socket.socket.sendto = _offline
    if hasattr(socket.socket, "sendmsg"):
        socket.socket.sendmsg = _offline
    socket.create_connection = _offline
    socket.getaddrinfo = _offline
    socket.gethostbyname = _offline
    socket.gethostbyname_ex = _offline
    socket.gethostbyaddr = _offline
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(tests))
    with tempfile.TemporaryDirectory(prefix="daytrader-upstream-tests-") as cwd:
        os.chdir(cwd)
        os.environ["HOME"] = cwd
        os.environ.pop("SEC_USER_AGENT", None)
        suite = unittest.defaultTestLoader.discover(str(tests), pattern="test_*.py")
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
