"""Crawl URLs one at a time with Crawl4AI and post the markdown to the ingest endpoint.

Runs on the VPS inside its crawl4ai virtualenv:
    ~/crawl4ai-env/bin/python crawl_push.py URL [URL ...]

KB_INGEST_URL   default https://rickys-mac-pro.taile4827e.ts.net:8444/v1/pages
KB_INGEST_IP    default 100.71.175.87 (the VPS does not resolve .ts.net names;
                it connects to this address and verifies TLS for the hostname)
KB_INGEST_TOKEN_FILE   default ~/.config/ripperr-knowledge-base/ingest-token
"""

import asyncio
import http.client
import json
import os
import socket
import ssl
import sys
from pathlib import Path
from urllib.parse import urlsplit

from crawl4ai import AsyncWebCrawler

URL = os.environ.get("KB_INGEST_URL", "https://rickys-mac-pro.taile4827e.ts.net:8444/v1/pages")
IP = os.environ.get("KB_INGEST_IP", "100.71.175.87")
TOKEN = Path(os.environ.get(
    "KB_INGEST_TOKEN_FILE", Path.home() / ".config/ripperr-knowledge-base/ingest-token"
)).read_text().strip()


def post(page: dict) -> dict:
    target = urlsplit(URL)
    host, port = target.hostname, target.port or 443
    sock = ssl.create_default_context().wrap_socket(
        socket.create_connection((IP, port), timeout=30), server_hostname=host
    )
    conn = http.client.HTTPSConnection(host, port, timeout=60)
    conn.sock = sock
    conn.request("POST", target.path, json.dumps(page), {
        "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    response = conn.getresponse()
    body = response.read().decode()
    if response.status != 200:
        raise RuntimeError(f"ingest {response.status}: {body}")
    return json.loads(body)


async def main(urls: list[str]) -> int:
    failures = 0
    async with AsyncWebCrawler() as crawler:
        for url in urls:  # one page at a time: the VPS has 6 GB
            try:
                result = await crawler.arun(url)
                if not result.success:
                    raise RuntimeError(result.error_message)
                markdown = str(result.markdown)
                title = (result.metadata or {}).get("title")
                print(url, post({"url": result.url or url, "title": title, "markdown": markdown}))
            except Exception as error:  # keep going; report at the end
                failures += 1
                print(url, "FAILED:", error, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
