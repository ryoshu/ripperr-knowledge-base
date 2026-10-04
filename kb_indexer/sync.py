"""Crash-safe change-feed synchronization."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from urllib.error import URLError

from .chunking import chunk_turns
from .client import ApiError, RipperrClient
from .embeddings import LocalEmbedder
from .store import IndexStore

LOG = logging.getLogger(__name__)


class Indexer:
    def __init__(self, store: IndexStore, client: RipperrClient, embedder: LocalEmbedder):
        self.store = store
        self.client = client
        self.embedder = embedder

    def _bootstrap(self) -> None:
        response = self.client.bootstrap()
        self.store.begin_reset(int(response["after"]), int(response["next_cursor"]))
        LOG.info("bootstrap queued %s episodes", response["emitted"])

    def sync_page(self, limit: int = 100) -> int:
        cursor, initialized, cutoff = self.store.state()
        if not initialized:
            self._bootstrap()
            cursor, _, cutoff = self.store.state()
        if cutoff is not None and cursor >= cutoff:
            self.store.complete_reset()
            return 0
        try:
            page = self.client.changes(cursor, limit)
        except ApiError as error:
            if error.status == 410 and error.body.get("reset") is True:
                LOG.warning("change cursor %s was pruned; bootstrapping", cursor)
                self._bootstrap()
                return 0
            raise
        events = page["changes"]
        if cutoff is not None:
            events = [event for event in events if int(event["seq"]) <= cutoff]
            if not events and cursor < cutoff:
                raise RuntimeError("bootstrap range disappeared before replay finished")
        for event in events:
            guid = event["episode_guid"]
            episode = None
            etag = None
            chunks = None
            vectors = None
            if event["kind"] != "deleted":
                existing = self.store.document(guid)
                episode, etag = self.client.episode(
                    guid, existing["etag"] if existing else None
                )
                if episode is not None and episode["status"] == "done":
                    if existing is None or int(episode["revision"]) != existing["revision"]:
                        chunks = chunk_turns(guid, int(episode["revision"]), episode["turns"])
                        vectors = self.embedder.embed([chunk.text for chunk in chunks])
            self.store.apply_event(event, episode, etag, chunks, vectors)
        if cutoff is not None and self.store.state()[0] >= cutoff:
            self.store.complete_reset()
        return len(events)

    def sync_until_idle(self, max_pages: int = 100) -> int:
        total = 0
        for _ in range(max_pages):
            before = self.store.state()
            count = self.sync_page()
            total += count
            after = self.store.state()
            if count == 0 and after == before:
                return total
        raise RuntimeError(f"change feed did not settle within {max_pages} pages")

    def run_forever(self, poll_seconds: float = 30) -> None:
        retry_seconds = 5.0
        while True:
            try:
                count = self.sync_until_idle()
                if count:
                    LOG.info("applied %s changes; cursor=%s", count, self.store.state()[0])
                retry_seconds = 5.0
                time.sleep(poll_seconds)
            except (URLError, TimeoutError, OSError, ApiError) as error:
                LOG.warning("ripperr unavailable: %s; retry in %.0fs", error, retry_seconds)
                time.sleep(retry_seconds)
                retry_seconds = min(300.0, retry_seconds * 2)


def default_paths() -> tuple[Path, Path, Path]:
    home = Path.home()
    return (
        home / ".config/ripperr-kb/api-token",
        home / ".local/share/ripperr-kb/index.db",
        home / ".cache/ripperr-kb/models",
    )
