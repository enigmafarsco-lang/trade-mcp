"""Tiny RSS/Atom reader for the console's news tab (stdlib only)."""

from __future__ import annotations

import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

USER_AGENT = "trade-mcp/0.1 (+local console)"
CACHE_TTL = 300.0

#: Used when TRADE_MCP_NEWS_FEEDS is empty.  Override it with your own list.
DEFAULT_FEEDS: tuple[tuple[str, str], ...] = (
    ("ForexLive", "https://www.forexlive.com/feed/news"),
    ("FXStreet", "https://www.fxstreet.com/rss/news"),
    ("Kitco", "https://www.kitco.com/rss/KitcoNews.xml"),
)

_CACHE: dict[str, Any] = {"at": 0.0, "items": []}


def _parse_feed(source: str, raw: bytes) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(raw)  # noqa: S314 - feeds are plain XML from the user's list
    except ET.ParseError:
        return []

    items: list[dict[str, Any]] = []
    # RSS 2.0
    for node in root.iter("item"):
        items.append(
            {
                "source": source,
                "title": _text(node.find("title")),
                "link": _text(node.find("link")),
                "published": _text(node.find("pubDate")),
                "summary": _strip(_text(node.find("description")))[:400],
            }
        )
    # Atom
    ns = "{http://www.w3.org/2005/Atom}"
    for node in root.iter(f"{ns}entry"):
        link_node = node.find(f"{ns}link")
        items.append(
            {
                "source": source,
                "title": _text(node.find(f"{ns}title")),
                "link": link_node.get("href") if link_node is not None else "",
                "published": _text(node.find(f"{ns}updated")) or _text(node.find(f"{ns}published")),
                "summary": _strip(_text(node.find(f"{ns}summary")))[:400],
            }
        )
    return [item for item in items if item["title"]]


def _text(node: ET.Element | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def _strip(text: str) -> str:
    out, depth = [], 0
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return " ".join("".join(out).split())


def fetch_news(
    feeds: tuple[str, ...] = (),
    *,
    limit: int = 25,
    timeout: float = 8.0,
    use_cache: bool = True,
) -> list[dict[str, Any]]:
    """Return the newest headlines from the configured feeds.

    ``feeds`` items may be bare URLs or ``"Name|url"`` pairs.
    Failures are swallowed per-feed so one dead feed cannot break the tab.
    """
    now = time.time()
    if use_cache and now - float(_CACHE["at"]) < CACHE_TTL:
        return list(_CACHE["items"])[:limit]

    targets: list[tuple[str, str]] = []
    for raw in feeds:
        raw = (raw or "").strip()
        if not raw:
            continue
        if "|" in raw:
            name, _, url = raw.partition("|")
            targets.append((name.strip(), url.strip()))
        else:
            targets.append((_host(raw), raw))
    if not targets:
        targets = list(DEFAULT_FEEDS)

    items: list[dict[str, Any]] = []
    for name, url in targets:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                raw = resp.read(400_000)
        except (urllib.error.URLError, OSError, TimeoutError):
            continue
        items.extend(_parse_feed(name, raw))

    _CACHE["at"] = now
    _CACHE["items"] = items[: limit * 2]
    return items[:limit]


def _host(url: str) -> str:
    for prefix in ("https://", "http://"):
        if url.startswith(prefix):
            rest = url[len(prefix) :]
            return rest.split("/")[0]
    return url
