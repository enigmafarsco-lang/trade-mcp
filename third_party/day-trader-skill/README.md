# Bundled upstream Day Trader reference

`upstream/` is the complete, unmodified source, examples, original tests and
small public test fixtures of `Jakeschincariol/day-trader-skill`, pinned to
commit `3d0f4ef6986f4984b7c6bd46f7ff4f87744c3abd`. Copyright 2026 Jake
Schincariol; MIT license in `upstream/LICENSE` and
`../../LICENSES/day-trader-skill-MIT.txt`. `SHA256SUMS` records every bundled
file. No separate download is needed for this reference checkout. The standalone
reference lives in the Git checkout and is not included in the installed wheel;
the integrated Python adapters and pure excerpts are packaged separately.

This directory is **not** automatically installed as a Claude plugin, loaded
as MT5 tools, or connected to an account. It is the original US-equity program,
not an MT5/XAUUSD port. Its CLI defaults can access public internet services and
can write `trader/` files relative to the working directory. The upstream broker
skill describes an independent Alpaca/IBKR workflow; an upstream PASS or yes is
**not** a trade-mcp proposal, MT5 risk validation, or authorization. Do not use
that independent flow as a substitute for trade-mcp's guarded MT5 execution.
The project does not automatically execute upstream skill instructions.

Run the original suite without a network or access to the current directory:

```sh
.venv/bin/python scripts/test_upstream_daytrader.py
(cd third_party/day-trader-skill && sha256sum -c SHA256SUMS)
```

The first command uses a temporary working directory and blocks sockets. Its
fixtures are upstream's public stock/House examples, not user or MT5 data. It
creates no runtime report and does not check a live broker. See
`docs/DAY-TRADER-EXTENDED-FA.md` for what is actually integrated in trade-mcp,
its distinct MT5 safeguards, and the features that remain reference-only.
