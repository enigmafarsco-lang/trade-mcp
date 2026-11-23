"""Bounded, offline source parsing based on the original Day Trader stock skills.

These functions accept text/JSON only, never URLs or local paths. Stock facts and
House disclosures are not fundamentals or trade signals for a gold CFD. The pure
parsers are MIT-licensed upstream excerpts; see LICENSES/day-trader-skill-MIT.txt.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any

from ._upstream_daytrade import disclosures as upstream_disclosures
from ._upstream_daytrade import facts as upstream_facts
from ._upstream_daytrade import news as upstream_news
from .daytrade import _integer, _number, _price
from .daytrade_extra import _flag, _symbol

MAX_XML_BYTES = 1_000_000
MAX_FACTS_BYTES = 3_000_000
MAX_ITEMS = 500
MAX_SEEN = 2000
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def _date(value: Any, name: str) -> dt.date:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ValueError(f"{name} must be a YYYY-MM-DD date")
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be a valid date") from None


def _utc(value: Any) -> dt.datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("now must be an ISO timestamp with timezone")
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("now must be an ISO timestamp with timezone") from None
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("now must include a UTC offset")
    try:
        return stamp.astimezone(dt.timezone.utc)
    except OverflowError:
        raise ValueError("now is outside the supported UTC range") from None


def _bounded_text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be nonempty text")
    if len(value.encode("utf-8")) > limit:
        raise ValueError(f"{name} exceeds the size limit")
    return value


def _xml(value: Any) -> str:
    text = _bounded_text(value, "XML", MAX_XML_BYTES)
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
        raise ValueError("XML DTDs and entities are not accepted")
    return text


def _small_label(value: Any, name: str, maximum: int = 120) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"Invalid {name}")
    return value


def stock_facts(*, companyfacts: Any, quarters: int = 6, as_of: str | None = None) -> dict[str, Any]:
    """Summarize supplied SEC companyfacts, with optional filing-date cutoff."""
    n = _integer(quarters, "quarters", 1, 12)
    cutoff = _date(as_of, "as_of") if as_of is not None else None
    if not isinstance(companyfacts, dict):
        raise ValueError("companyfacts must be an SEC XBRL object")
    try:
        encoded = json.dumps(companyfacts, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise ValueError("companyfacts must be finite JSON") from None
    if len(encoded.encode("utf-8")) > MAX_FACTS_BYTES:
        raise ValueError("companyfacts exceeds the size limit")
    cik = companyfacts.get("cik")
    if isinstance(cik, bool) or not isinstance(cik, (str, int)) or not re.fullmatch(r"\d{1,10}", str(cik)):
        raise ValueError("A numeric SEC CIK is required")
    entity = companyfacts.get("entityName")
    if entity is not None:
        _small_label(entity, "entity name", 200)
    facts = companyfacts.get("facts")
    if not isinstance(facts, dict) or not isinstance(facts.get("us-gaap"), dict):
        raise ValueError("companyfacts.facts.us-gaap must be an object")
    gaap = facts["us-gaap"]
    concepts = {name for names in upstream_facts.CONCEPTS.values() for name in names}
    eps_concepts = set(upstream_facts.CONCEPTS["eps_diluted"])
    selected = {}
    count = skipped_units = skipped_undated = excluded_future = 0
    for name in sorted(concepts):
        node = gaap.get(name)
        if node is None:
            continue
        if not isinstance(node, dict) or not isinstance(node.get("units"), dict):
            raise ValueError("Invalid XBRL concept units")
        unit = "USD/shares" if name in eps_concepts else "USD"
        values = node["units"].get(unit)
        if values is None:
            skipped_units += 1
            continue
        if not isinstance(values, list) or len(values) > 1000:
            raise ValueError("Too many facts in an XBRL concept")
        count += len(values)
        if count > 10_000:
            raise ValueError("Too many total XBRL facts")
        kept = []
        for fact in values:
            if not isinstance(fact, dict):
                raise ValueError("Each XBRL fact must be an object")
            form = fact.get("form")
            if not isinstance(form, str) or form.split("/")[0] not in ("10-Q", "10-K", "20-F", "40-F"):
                continue
            if "start" not in fact:  # Ignore instant rather than duration facts, like upstream.
                continue
            if not fact.get("filed"):
                skipped_undated += 1
                continue
            start, end, filed = (_date(fact.get(k), k) for k in ("start", "end", "filed"))
            if start > end or end > filed:
                raise ValueError("Invalid XBRL period or filing date")
            if cutoff is not None and filed > cutoff:
                excluded_future += 1
                continue
            number = _number(fact.get("val"), "XBRL value", minimum=-1e18, maximum=1e18)
            if 0 < abs(number) < 1e-100:
                raise ValueError("XBRL nonzero value is too small for safe growth arithmetic")
            accn, fp, fy = fact.get("accn", ""), fact.get("fp"), fact.get("fy")
            if not isinstance(accn, str) or len(accn) > 40 or (fp is not None and (not isinstance(fp, str) or len(fp) > 10)):
                raise ValueError("Invalid XBRL filing or fiscal period")
            if fy is not None:
                fy = _integer(fy, "fiscal year", 1800, 2200)
            kept.append({"start": start.isoformat(), "end": end.isoformat(), "filed": filed.isoformat(),
                         "val": number, "form": form, "accn": accn, "fp": fp, "fy": fy})
        selected[name] = {"units": {unit: kept}}
    safe_facts = {"cik": cik, "entityName": entity, "facts": {"us-gaap": selected}}
    try:
        result = upstream_facts.analyse(safe_facts, quarters=n)
        changes = upstream_facts.changes(result)
        json.dumps(result, allow_nan=False)
    except upstream_facts.FactsError as exc:
        raise ValueError(str(exc)) from None
    except (ValueError, OverflowError, ZeroDivisionError):
        raise ValueError("XBRL calculation produced an invalid or non-finite value") from None
    return {"ok": True, "executable": False, "execution_authorized": False,
            "scope": "US-company SEC XBRL, not XAUUSD or MT5 fundamentals", "as_of": as_of,
            "result": result, "changes": changes,
            "excluded_after_as_of": excluded_future, "skipped_missing_filing_date": skipped_undated,
            "skipped_non_usd_concepts": skipped_units,
            "warnings": ["Caller-supplied data; authenticity, freshness and latest filings are not checked.",
                         "Derived Q4 EPS is approximate. Date-only as_of is not an intraday publication time.",
                         "USD and USD/share concepts only; unknown units are not silently converted."]}


def parse_news(
    *, feeds: Any, watch: Any, now: str, hours: int = 48, seen_ids: Any = None,
    show_all: bool = False, market: str = "macro", moves: Any = None,
) -> dict[str, Any]:
    """Parse supplied RSS/SEC Atom, tag/dedupe and return caller-owned seen state."""
    stamp = _utc(now)
    window = _integer(hours, "hours", 1, 24 * 30)
    _flag(show_all, "show_all")
    if market not in ("macro", "us_stocks"):
        raise ValueError("market must be macro or us_stocks")
    if not isinstance(watch, dict) or len(watch) > 50:
        raise ValueError("watch must be a symbol-to-name object with at most 50 entries")
    for symbol, name in watch.items():
        _symbol(symbol)
        if name is not None:
            _small_label(name, "watch name")
    if not isinstance(feeds, list) or not 1 <= len(feeds) <= 10:
        raise ValueError("Provide 1 to 10 feeds")
    if seen_ids is None:
        seen_ids = []
    if (not isinstance(seen_ids, list) or len(seen_ids) > MAX_SEEN
            or any(not isinstance(x, str) or not re.fullmatch(r"[0-9a-f]{16}", x) for x in seen_ids)):
        raise ValueError("Invalid news seen_ids")
    seen = set(seen_ids)
    items = []
    for feed in feeds:
        if not isinstance(feed, dict) or set(feed) != {"kind", "symbol", "source", "xml"}:
            raise ValueError("Each feed requires kind, symbol, source and xml")
        kind = feed["kind"]
        if kind not in ("rss", "sec_atom") or (kind == "sec_atom" and market != "us_stocks"):
            raise ValueError("SEC Atom requires us_stocks; feed kind must be rss or sec_atom")
        symbol = _symbol(feed["symbol"])
        source = _small_label(feed["source"], "source")
        text = _xml(feed["xml"])
        try:
            parsed = (upstream_news.parse_rss(text, symbol) if kind == "rss"
                      else upstream_news.parse_sec_atom(text, symbol))
        except (upstream_news.NewsError, ValueError, TypeError, OverflowError) as exc:
            raise ValueError("Invalid RSS or SEC Atom feed") from exc
        if len(parsed) > MAX_ITEMS:
            raise ValueError("Too many entries in one feed")
        for item in parsed:
            item["source"] = source
            # Upstream's empty id would otherwise collapse unrelated stories.
            if item["id"] in ("rss:", "sec:"):
                basis = f"{symbol}|{item['title']}|{item.get('time')}"
                item["id"] = "synthetic-id:" + hashlib.sha256(basis.encode()).hexdigest()
        items.extend(parsed)
    upstream_news.tag(items, watch)
    items = upstream_news.dedupe(items)
    undated = sum(item["time"] is None for item in items)
    future = sum(item["time"] is not None and item["time"] > stamp for item in items)
    try:
        cutoff = stamp - dt.timedelta(hours=window)
    except OverflowError:
        raise ValueError("now is outside the supported news window") from None
    relevant = [item for item in items if item["time"] is not None and cutoff <= item["time"] <= stamp]
    relevant.sort(key=lambda item: item["time"], reverse=True)
    output = []
    newly_seen = []
    for item in relevant:
        fp = upstream_news.fingerprint(item)
        is_new = fp not in seen
        if is_new:
            newly_seen.append(fp)
        if is_new or show_all:
            output.append({**item, "time": item["time"].isoformat(), "fingerprint": fp, "new": is_new})
    ordered_seen = list(dict.fromkeys([*seen_ids, *newly_seen]))
    pruned = max(0, len(ordered_seen) - MAX_SEEN)
    ordered_seen = ordered_seen[-MAX_SEEN:]
    if moves is None:
        moves = []
    if not isinstance(moves, list) or len(moves) > 50:
        raise ValueError("moves must contain at most 50 supplied price comparisons")
    comparisons = []
    for move in moves:
        if not isinstance(move, dict) or set(move) != {"symbol", "price", "reference"}:
            raise ValueError("Each move requires symbol, price and reference")
        price = _price(move["price"], "price")
        ref = _price(move["reference"], "reference")
        comparisons.append({"symbol": _symbol(move["symbol"]), "price": price, "reference": ref,
                            "change_pct": (price / ref - 1) * 100})
    return {"ok": True, "executable": False, "execution_authorized": False,
            "market": market, "items": output, "price_moves": comparisons,
            "seen_ids": ordered_seen, "seen_pruned": pruned,
            "undated_excluded": undated, "future_excluded": future,
            "automatic_fetch": False, "automatic_monitoring": False,
            "warning": "Supplied feeds and prices only; timestamps and freshness are not independently verified."}


def stock_disclosures(
    *, kind: str, text: str, year: int | None = None, as_of: str | None = None,
    members: Any = None, since: str | None = None, seen_ids: Any = None,
    filed: str | None = None, new_only: bool = False,
) -> dict[str, Any]:
    """Read supplied US House XML index or already-extracted PTR text, offline."""
    if kind not in ("index", "report"):
        raise ValueError("kind must be index or report")
    _flag(new_only, "new_only")
    if kind == "report":
        if any(value is not None for value in (year, as_of, members, since, seen_ids)) or new_only:
            raise ValueError("Index-only options are not allowed for a report")
        if filed is not None:
            _date(filed, "filed")
        body = _bounded_text(text, "report text", 75_000)
        if any(len(line) > 1000 for line in body.splitlines()):
            raise ValueError("A report line exceeds the size limit")
        # The upstream option regex scans across continuation lines. Limit
        # repeated candidate phrases before handing untrusted text to it.
        if len(re.findall(r"\b(?:call|put)\s+options?\b", body, re.IGNORECASE)) > 50:
            raise ValueError("Too many option descriptions in one report")
        try:
            result = upstream_disclosures.parse_ptr_text(body, filed=filed)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError("Invalid PTR text") from exc
        if len(result["rows"]) > MAX_ITEMS:
            raise ValueError("Too many report rows")
        try:
            json.dumps(result, allow_nan=False)
        except (ValueError, TypeError, OverflowError):
            raise ValueError("Non-finite PTR value") from None
        return {"ok": True, "executable": False, "execution_authorized": False,
                "scope": "US House disclosure, not an XAUUSD signal", "report": result,
                "warning": "Text extraction can be incomplete; filing and transaction dates may be delayed."}
    if filed is not None:
        raise ValueError("filed is only used for a report")
    y = _integer(year, "year", 2000, 2100)
    today = _date(as_of, "as_of")
    if since is not None and _date(since, "since") > today:
        raise ValueError("since must not be later than as_of")
    if members is None:
        members = []
    if not isinstance(members, list) or len(members) > 20:
        raise ValueError("members must be a list of at most 20 names")
    for member in members:
        _small_label(member, "member name")
    if seen_ids is None:
        seen_ids = []
    if (not isinstance(seen_ids, list) or len(seen_ids) > MAX_SEEN
            or any(not isinstance(v, str) or not re.fullmatch(r"\d{1,16}", v) for v in seen_ids)):
        raise ValueError("Invalid disclosure seen_ids")
    xml = _xml(text)
    try:
        rows = upstream_disclosures.parse_index(xml, y)
    except (upstream_disclosures.PtrError, ValueError, TypeError, OverflowError) as exc:
        raise ValueError("Invalid House index XML") from exc
    if len(rows) > 5000:
        raise ValueError("Too many disclosure index rows")
    if any(not re.fullmatch(r"\d{1,16}", r["doc_id"]) or not 2000 <= r["year"] <= 2100 for r in rows):
        raise ValueError("Invalid disclosure document ID or year")
    selected = upstream_disclosures.select(rows, members=members, kind="P", since=since)
    selected = [r for r in selected if r["filed"] and r["filed"] <= today.isoformat()]
    if len(selected) > MAX_ITEMS:
        raise ValueError("Too many matching disclosures; narrow members or since")
    seen = set(seen_ids)
    output = [{**r, "new": r["doc_id"] not in seen} for r in selected if not new_only or r["doc_id"] not in seen]
    ordered_seen = list(dict.fromkeys([*seen_ids, *(r["doc_id"] for r in selected)]))
    pruned = max(0, len(ordered_seen) - MAX_SEEN)
    return {"ok": True, "executable": False, "execution_authorized": False,
            "scope": "US House disclosure, not an XAUUSD signal", "filings": output,
            "seen_ids": ordered_seen[-MAX_SEEN:], "seen_pruned": pruned, "as_of": as_of,
            "warning": "Caller-supplied index; filings can lag the transaction and data is not verified."}
