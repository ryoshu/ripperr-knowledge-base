"""Local indexer control and query commands."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from .client import RipperrClient
from .embeddings import LocalEmbedder
from .evaluate import evaluate
from .store import IndexStore
from .sync import Indexer, default_paths


def main(argv: list[str] | None = None) -> int:
    token_default, db_default, cache_default = default_paths()
    parser = argparse.ArgumentParser(description="Index ripperr transcripts over HTTP")
    parser.add_argument("--api", default=os.environ.get(
        "RIPPERR_API_URL", "https://nyarlathotep.taile4827e.ts.net"
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


if __name__ == "__main__":
    raise SystemExit(main())
