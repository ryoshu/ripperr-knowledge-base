"""Podcast discovery: SearXNG search, resolved to RSS feeds, for a person to approve.

Nothing here subscribes on its own. Requests are sequential and read only the
start of each feed, enough for its title and newest episode.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
from typing import Callable
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen
from xml.etree.ElementTree import ParseError, iterparse

USER_AGENT = "ripperr-knowledge-base/0.1 (podcast discovery; personal use)"
MAX_FEED_BYTES = 512 * 1024
APPLE_ID = re.compile(r"podcasts\.apple\.com/.*?/id(\d+)")

Fetch = Callable[[str], tuple[bytes, str]]  # url -> (body prefix, final url)


@dataclass
class Candidate:
    title: str
    feed_url: str | None
    source_url: str
    engine: str
    about: str  # SearXNG's metadata, e.g. "184 episodes | Sports, ..."
    latest_title: str | None = None
    latest_published: str | None = None
    subscribed: bool = False
    error: str | None = None


def fetch(url: str, max_bytes: int = MAX_FEED_BYTES, timeout: float = 15) -> tuple[bytes, str]:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urlopen(request, timeout=timeout) as response:
        return response.read(max_bytes), response.geturl()


def search(searxng_url: str, query: str, get: Fetch = fetch) -> list[dict]:
    url = searxng_url.rstrip("/") + "/search?" + urlencode(
        {"q": query, "categories": "podcasts", "format": "json"}
    )
    body, _ = get(url)
    return json.loads(body).get("results", [])


def feed_info(body: bytes) -> dict | None:
    """Title and newest episode of an RSS or Atom feed, or None if it is not
    one. Reads only until the first item, so a truncated body is fine."""
    info: dict = {"title": None, "latest_title": None, "latest_published": None}
    is_feed = False
    path: list[str] = []
    try:
        for event, element in iterparse(BytesIO(body), events=("start", "end")):
            tag = element.tag.rsplit("}", 1)[-1].lower()
            if event == "start":
                if not path and tag in ("rss", "feed", "rdf"):
                    is_feed = True
                path.append(tag)
                continue
            path.pop()
            text = (element.text or "").strip() or None
            parent = path[-1] if path else None
            if tag == "title" and parent in ("channel", "feed") and info["title"] is None:
                info["title"] = text
            elif parent in ("item", "entry"):
                if tag == "title" and info["latest_title"] is None:
                    info["latest_title"] = text
                elif tag in ("pubdate", "published", "updated") and info["latest_published"] is None:
                    info["latest_published"] = text
            elif tag in ("item", "entry"):
                break  # the newest episode is enough
    except ParseError:
        pass  # truncated or not XML; keep whatever was read
    return info if is_feed else None


class _AlternateFeeds(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if (tag == "link" and "alternate" in a.get("rel", "").lower()
                and a.get("type", "").lower() in ("application/rss+xml", "application/atom+xml")
                and a.get("href")):
            self.links.append(a["href"])


def resolve_feed(url: str, get: Fetch = fetch) -> tuple[str, dict]:
    """The feed URL behind a search result, and that feed's info. Tries the
    URL itself, then an Apple Podcasts lookup, then the page's feed links."""
    match = APPLE_ID.search(url)
    if match:
        body, _ = get("https://itunes.apple.com/lookup?" + urlencode({"id": match.group(1)}))
        results = json.loads(body).get("results") or [{}]
        feed_url = results[0].get("feedUrl")
        if not feed_url:
            raise ValueError("Apple Podcasts has no public feed for this show")
        return _as_feed(feed_url, get)
    body, final_url = get(url)
    info = feed_info(body)
    if info is not None:
        return url, info
    parser = _AlternateFeeds()
    parser.feed(body.decode("utf-8", "replace"))
    for href in parser.links:
        try:
            return _as_feed(urljoin(final_url, href), get)
        except ValueError:
            continue
    raise ValueError("no RSS feed found at this address")


def _as_feed(url: str, get: Fetch) -> tuple[str, dict]:
    body, _ = get(url)
    info = feed_info(body)
    if info is None:
        raise ValueError(f"{url} is not an RSS or Atom feed")
    return url, info


def same_feed(a: str, b: str) -> bool:
    """URLs equal apart from scheme, host case and a trailing slash."""
    def key(url: str) -> tuple[str, str, str]:
        parts = urlsplit(url.strip())
        return parts.netloc.lower(), parts.path.rstrip("/"), parts.query
    return key(a) == key(b)


def discover(
    searxng_url: str, query: str, subscribed: list[str], limit: int = 10, get: Fetch = fetch,
) -> list[Candidate]:
    """Search results resolved to feeds, deduplicated, newest-episode info
    attached, and those ripperr already follows marked."""
    candidates: list[Candidate] = []
    for result in search(searxng_url, query, get):
        if len(candidates) >= limit:
            break
        url = result.get("url") or ""
        if not url or any(c.source_url == url for c in candidates):
            continue
        candidate = Candidate(
            title=result.get("title") or url, feed_url=None, source_url=url,
            engine=result.get("engine") or "?", about=result.get("metadata") or "",
        )
        try:
            candidate.feed_url, info = resolve_feed(url, get)
        except (OSError, ValueError) as error:  # unreachable, refused, or not a podcast
            candidate.error = str(error)[:200]
        else:
            if any(c.feed_url and same_feed(c.feed_url, candidate.feed_url) for c in candidates):
                continue
            candidate.title = info["title"] or candidate.title
            candidate.latest_title = info["latest_title"]
            candidate.latest_published = info["latest_published"]
            candidate.subscribed = any(same_feed(candidate.feed_url, s) for s in subscribed)
        candidates.append(candidate)
    return candidates


def parse_choice(answer: str, count: int) -> list[int]:
    """'1, 3' -> [0, 2]. Raises ValueError for anything outside 1..count."""
    picks = []
    for part in answer.replace(",", " ").split():
        number = int(part)
        if not 1 <= number <= count:
            raise ValueError(f"{number} is not between 1 and {count}")
        if number - 1 not in picks:
            picks.append(number - 1)
    return picks
