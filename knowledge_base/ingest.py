"""Authenticated endpoint that takes cleaned web pages from the crawler (W5).

`POST /v1/pages` with {url, markdown, title?, published?}. The crawler fetches;
this side only indexes, so a page can never make the server fetch anything.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .chunking import chunk_markdown
from .embeddings import LocalEmbedder
from .mcp_server import BearerAuth
from .store import IndexStore

MAX_BODY_BYTES = 2_000_000
TRACKING = ("utm_", "fbclid", "gclid", "mc_", "ref")


def canonical_url(url: str) -> str:
    """Lowercase scheme and host, drop fragment, tracking params and a trailing slash."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("url must be http(s)")
    query = urlencode(sorted(
        (k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith(TRACKING)
    ))
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, host, parts.path.rstrip("/") or "/", query, ""))


def ingest_page(store: IndexStore, embedder: LocalEmbedder, body: dict) -> dict:
    markdown = body.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        raise ValueError("markdown is required")
    url = canonical_url(str(body.get("url", "")))
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    content_hash = "sha256:" + hashlib.sha256(markdown.encode()).hexdigest()
    existing = store.web_document(url)
    if existing and existing["etag"] == content_hash:
        store.touch_page(url, fetched_at)
        return {"status": "unchanged", "url": url}
    revision = existing["revision"] + 1 if existing else 1
    chunks = chunk_markdown(url, revision, markdown)
    store.put_page(
        url, body.get("title") or None, body.get("published") or None, fetched_at,
        content_hash, revision, urlsplit(url).hostname, chunks,
        embedder.embed([chunk.text for chunk in chunks]),
    )
    return {"status": "updated" if existing else "created", "url": url, "chunks": len(chunks)}


def asgi_app(db: Path, embedder: LocalEmbedder, token: str):
    def pages(request: Request) -> JSONResponse:  # sync: runs in a worker thread
        try:
            body = json.loads(request.state.body)
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
            store = IndexStore(db)
            try:
                return JSONResponse(ingest_page(store, embedder, body))
            finally:
                store.close()
        except ValueError as error:  # includes bad JSON
            return JSONResponse({"error": str(error)}, status_code=400)

    class Limit:
        """Reject oversized bodies before parsing and hand the bytes to the route."""

        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                chunks, size = [], 0
                while True:
                    message = await receive()
                    chunks.append(message.get("body", b""))
                    size += len(chunks[-1])
                    if size > MAX_BODY_BYTES:
                        return await JSONResponse({"error": "body too large"}, status_code=413)(scope, receive, send)
                    if not message.get("more_body"):
                        break
                scope.setdefault("state", {})["body"] = b"".join(chunks)
            await self.app(scope, receive, send)

    app = Starlette(routes=[Route("/v1/pages", pages, methods=["POST"])])
    return BearerAuth(Limit(app), token)


def serve(db: Path, embedder: LocalEmbedder, token: str, port: int) -> None:
    import uvicorn

    uvicorn.run(asgi_app(db, embedder, token), host="127.0.0.1", port=port, log_level="warning")
