"""Local indexer control and query commands."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from .client import ApiError, RipperrClient
from .embeddings import LocalEmbedder
from .evaluate import evaluate
from .store import IndexStore
from .sync import Indexer, default_paths


def main(argv: list[str] | None = None) -> int:
    token_default, db_default, cache_default = default_paths()
    searxng_default = os.environ.get("KB_SEARXNG_URL", "https://search.ryoshu.com")
    parser = argparse.ArgumentParser(description="Index ripperr transcripts over HTTP")
    parser.add_argument("--api", default=os.environ.get(
        "RIPPERR_API_URL", "http://127.0.0.1:8876"
    ))
    parser.add_argument("--token-file", type=Path, default=Path(os.environ.get(
        "RIPPERR_TOKEN_FILE", token_default
    )))
    parser.add_argument("--db", type=Path, default=Path(os.environ.get(
        "KB_INDEX_DB", db_default
    )))
    parser.add_argument("--cache", type=Path, default=Path(os.environ.get(
        "KB_MODEL_CACHE", cache_default
    )))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sync", help="catch up with the change feed and exit")
    sub.add_parser("serve-indexer", help="poll the change feed continuously")
    sub.add_parser("status", help="show index counts and cursor")
    query = sub.add_parser("search", help="run local hybrid retrieval")
    query.add_argument("query")
    query.add_argument("-k", type=int, default=10)
    evaluation = sub.add_parser("eval", help="measure passage retrieval on labeled questions")
    evaluation.add_argument("cases", type=Path)
    find = sub.add_parser("discover", help="find podcast feeds with SearXNG and subscribe ripperr to chosen ones")
    find.add_argument("query")
    find.add_argument("--limit", type=int, default=10)
    find.add_argument("--backfill", type=int, default=None,
                      help="also take each chosen feed's N newest episodes (1-100), not just the latest")
    find.add_argument("--searxng", default=searxng_default)
    mcp = sub.add_parser("serve-mcp", help="serve the MCP retrieval tools over streamable HTTP")
    mcp.add_argument("--port", type=int, default=int(os.environ.get("KB_MCP_PORT", "8877")))
    mcp.add_argument("--searxng", default=searxng_default)
    mcp.add_argument("--mcp-token-file", type=Path, default=Path(os.environ.get(
        "KB_MCP_TOKEN_FILE", token_default.with_name("mcp-token")
    )))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = IndexStore(args.db)
    try:
        if args.command == "status":
            print(json.dumps({"cursor": store.state()[0], **store.counts()}))
        elif args.command == "search":
            vector = LocalEmbedder(args.cache).embed([args.query])[0]
            print(json.dumps(store.search(args.query, vector, args.k), indent=2))
        elif args.command == "eval":
            print(json.dumps(evaluate(store, LocalEmbedder(args.cache), args.cases), indent=2))
        elif args.command == "discover":
            store.close()
            return discover_command(args)
        elif args.command == "serve-mcp":
            from .mcp_server import Discovery, KnowledgeBase, serve

            token = args.mcp_token_file.read_text().strip()
            # Hostnames the server is reached by besides localhost, e.g. the
            # `tailscale serve` name; both the bare and host:port forms are allowed.
            hosts = [h.strip() for h in os.environ.get("KB_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
            store.close()
            serve(KnowledgeBase(args.db, LocalEmbedder(args.cache)), token,
                  [form for host in hosts for form in (host, f"{host}:*")], args.port,
                  Discovery(RipperrClient(args.api, args.token_file), args.searxng))
            return 0
        else:
            client = RipperrClient(args.api, args.token_file)
            indexer = Indexer(store, client, LocalEmbedder(args.cache))
            if args.command == "sync":
                count = indexer.sync_until_idle()
                print(json.dumps({"applied": count, "cursor": store.state()[0], **store.counts()}))
            else:
                indexer.run_forever()
    finally:
        store.close()
    return 0


def discover_command(args) -> int:
    import sys

    from .discover import discover, parse_choice

    client = RipperrClient(args.api, args.token_file)
    subscribed = [feed["url"] for feed in client.feeds()]
    candidates = discover(args.searxng, args.query, subscribed, args.limit)
    if not candidates:
        print("no podcast results")
        return 0
    for number, c in enumerate(candidates, 1):
        mark = "  [subscribed]" if c.subscribed else ""
        print(f"{number:>2}. {c.title}{mark}")
        if c.error:
            print(f"    ! {c.error}  ({c.source_url})")
            continue
        print(f"    {c.feed_url}")
        latest = " · ".join(x for x in (c.latest_published, c.latest_title) if x)
        print(f"    {c.about}" + (f"  —  newest: {latest}" if latest else ""))
    choosable = [i for i, c in enumerate(candidates) if c.feed_url and not c.subscribed]
    if not choosable or not sys.stdin.isatty():
        return 0  # subscribing always needs a person at the prompt
    while True:
        answer = input("\nSubscribe to which? Numbers, comma-separated; blank for none: ").strip()
        try:
            picks = parse_choice(answer, len(candidates))
        except ValueError as error:
            print(f"  {error}")
            continue
        if any(i not in choosable for i in picks):
            print("  pick only feeds that resolved and are not already subscribed")
            continue
        break
    for i in picks:
        c = candidates[i]
        try:
            result = client.add_feed(c.feed_url, c.title, args.backfill)
        except ApiError as error:
            print(f"  ! {c.title}: {error}")
            continue
        extra = f", {result['backfilled']} episode(s) recorded" if "backfilled" in result else ""
        print(f"  subscribed: {c.title} (feed {result['feed']['id']}{extra})")
    if picks:
        print("ripperr prepares new episodes on its next hourly run; a worker transcribes them after that.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
