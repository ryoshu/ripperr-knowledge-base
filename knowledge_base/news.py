"""News for named players: SearXNG `news` search, limited to allowlisted
domains, minus pages already in the index. The crawler fetches what is left.

A page's usable-from date is the time the ingest endpoint received it, so
late-crawled old news is never backdated.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from .discover import Fetch, fetch
from .ingest import canonical_url
from .store import IndexStore


# Pages with no news in them: video pages, live blogs, betting picks, player profiles and stats.
SKIP_PATHS = ("/video", "/live-blog", "/betting", "/odds", "/player/", "/players/", "/stats/", "/athletic/live")


# Fantasy sites whose headlines the broader searches rank below bigger outlets.
FANTASY = ("rotowire.com", "fantasypros.com", "rotoballer.com", "fantasylife.com", "footballguys.com", "4for4.com")


def load_domains(path: Path) -> list[str]:
    lines = (line.split("#")[0].strip().lower() for line in path.read_text().splitlines())
    return [line for line in lines if line]


def allowed(url: str, domains: list[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    path = urlsplit(url).path.lower()
    return (any(host == d or host.endswith("." + d) for d in domains)
            and not any(skip in path for skip in SKIP_PATHS))


def find_urls(
    searxng_url: str, names: list[str], domains: list[str], store: IndexStore,
    per_player: int = 5, get: Fetch = fetch, pause: float = 3,
) -> list[str]:
    """New allowlisted news URLs from the last week, at most `per_player` per name and search."""
    urls: list[str] = []
    # The news category misses fantasy sites; a site: search of the general category finds them.
    sites = " OR ".join("site:" + d for d in domains)
    fantasy = " OR ".join("site:" + d for d in domains if d in FANTASY)
    for name in names:
        searches = [("news", f'"{name}" NFL'), ("general", f'"{name}" ({sites})')]
        if fantasy:
            searches.append(("general", f'"{name}" ({fantasy})'))
        for category, q in searches:
            time.sleep(pause)  # SearXNG's upstream engines suspend us when queried quickly
            query = urlencode({"q": q, "categories": category, "time_range": "week", "format": "json"})
            body, _ = get(searxng_url.rstrip("/") + "/search?" + query)
            taken = 0
            for result in json.loads(body).get("results", []):
                if taken >= per_player:
                    break
                try:
                    url = canonical_url(result.get("url") or "")
                except ValueError:
                    continue
                if url in urls or not allowed(url, domains) or store.web_document(url):
                    continue
                urls.append(url)
                taken += 1
    return urls


def crawl(ssh_host: str, urls: list[str], batch: int = 20) -> int:
    """Run the VPS crawler over `urls`; it crawls one page at a time and
    posts each to the ingest endpoint. Returns the number of failed batches."""
    failed = 0
    for i in range(0, len(urls), batch):
        run = subprocess.run(
            ["ssh", ssh_host, "~/crawl4ai-env/bin/python", "~/ripperr-crawl/crawl_push.py", *urls[i:i + batch]]
        )
        failed += run.returncode != 0
    return failed
