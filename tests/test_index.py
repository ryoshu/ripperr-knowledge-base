from __future__ import annotations

import sqlite3

import pytest

from knowledge_base.chunking import chunk_turns
from knowledge_base.client import ApiError
from knowledge_base.embeddings import DIMENSION, pack_vector
from knowledge_base.store import IndexStore
from knowledge_base.sync import Indexer


def episode(guid: str, revision: int, text: str, name: str = "Host") -> dict:
    return {
        "guid": guid,
        "revision": revision,
        "status": "done",
        "feed": {"id": 7, "title": "Example Feed"},
        "title": "Example Episode",
        "source_url": "https://example.com/episode",
        "published": "2026-09-01T00:00:00Z",
        "speaker_names": [{"speaker": "SPEAKER_00", "name": name}],
        "turns": [{
            "idx": 0, "start": 12, "end": 27,
            "speaker": "SPEAKER_00", "text": text,
        }],
    }


def event(seq: int, guid: str = "abc", kind: str = "transcript") -> dict:
    return {"seq": seq, "episode_guid": guid, "kind": kind, "revision": seq}


def insert(store: IndexStore, seq: int, data: dict) -> None:
    chunks = chunk_turns(data["guid"], data["revision"], data["turns"])
    vectors = [pack_vector([0.1] * DIMENSION) for _ in chunks]
    store.apply_event(event(seq, data["guid"]), data, '"etag"', chunks, vectors)


def test_revision_metadata_search_and_delete(tmp_path):
    store = IndexStore(tmp_path / "index.db")
    try:
        store.begin_reset(0, 1)
        insert(store, 1, episode("abc", 1, "The waiver wire has several choices."))
        store.complete_reset()
        assert store.counts() == {"documents": 1, "chunks": 1, "vectors": 1}
        hits = store.search("waiver wire", pack_vector([0.1] * DIMENSION))
        assert hits[0]["speaker_name"] == "Host"
        assert hits[0]["source_id"] == "abc"

        store.apply_event(event(2, kind="metadata"), episode(
            "abc", 1, "The waiver wire has several choices.", "Ricky"
        ), '"new-etag"')
        assert store.counts()["chunks"] == 1
        assert store.search("waiver", pack_vector([0.1] * DIMENSION))[0]["speaker_name"] == "Ricky"

        insert(store, 3, episode("abc", 2, "A different outlook this week."))
        assert store.counts() == {"documents": 1, "chunks": 1, "vectors": 1}
        assert store.search("different outlook", pack_vector([0.1] * DIMENSION))[0]["revision"] == 2
        store.apply_event(event(4, kind="deleted"), None, None)
        assert store.counts() == {"documents": 0, "chunks": 0, "vectors": 0}
        assert store.state()[0] == 4
    finally:
        store.close()


def test_failed_vector_write_keeps_old_revision_and_cursor(tmp_path):
    store = IndexStore(tmp_path / "index.db")
    try:
        insert(store, 1, episode("abc", 1, "Old words remain."))
        new = episode("abc", 2, "New words would replace them.")
        chunks = chunk_turns("abc", 2, new["turns"])
        with pytest.raises(sqlite3.Error):
            store.apply_event(event(2), new, '"new"', chunks, [b"bad"])
        assert store.state()[0] == 1
        assert store.counts() == {"documents": 1, "chunks": 1, "vectors": 1}
        assert store.search("Old words", pack_vector([0.1] * DIMENSION))[0]["revision"] == 1
    finally:
        store.close()


def test_reset_removes_only_podcast_documents_not_reemitted(tmp_path):
    store = IndexStore(tmp_path / "index.db")
    try:
        insert(store, 1, episode("abc", 1, "First episode"))
        insert(store, 2, episode("def", 1, "Second episode"))
        store.begin_reset(10, 11)
        store.apply_event(event(11, "abc"), episode("abc", 1, "First episode"), '"same"')
        store.complete_reset()
        assert store.counts() == {"documents": 1, "chunks": 1, "vectors": 1}
        assert store.document("abc") is not None
        assert store.document("def") is None
    finally:
        store.close()


def test_chunk_ids_do_not_depend_on_turn_indices():
    turns = [{"idx": 1, "speaker": "A", "start": 0, "end": 10,
              "text": " ".join(f"word{i}" for i in range(500))}]
    first = chunk_turns("abc", 2, turns)
    turns[0]["idx"] = 999
    assert first == chunk_turns("abc", 2, turns)
    assert len(first) == 2
    assert first[0].chunk_id != first[1].chunk_id


def test_restart_mid_page_replays_only_uncommitted_event(tmp_path):
    class Client:
        def bootstrap(self):
            return {"after": 0, "next_cursor": 2, "emitted": 2}

        def changes(self, after, limit):
            return {"changes": [
                event(1, "abc"), event(2, "def")
            ][after:]}

        def episode(self, guid, etag):
            return episode(guid, 1, f"Transcript for {guid}"), f'"{guid}"'

    class Embedder:
        calls = 0
        fail_second = True

        def embed(self, texts):
            self.calls += 1
            if self.calls == 2 and self.fail_second:
                raise RuntimeError("simulated crash while embedding")
            return [pack_vector([0.1] * DIMENSION) for _ in texts]

    path = tmp_path / "index.db"
    store = IndexStore(path)
    embedder = Embedder()
    with pytest.raises(RuntimeError, match="simulated crash"):
        Indexer(store, Client(), embedder).sync_page()
    assert store.state() == (1, True, 2)
    assert store.counts()["chunks"] == 1
    store.close()

    store = IndexStore(path)
    try:
        embedder.fail_second = False
        assert Indexer(store, Client(), embedder).sync_until_idle() == 1
        assert store.state() == (2, True, None)
        assert store.counts() == {"documents": 2, "chunks": 2, "vectors": 2}
    finally:
        store.close()


def test_pruned_cursor_bootstrap_drops_missing_episode(tmp_path):
    class Client:
        def bootstrap(self):
            return {"after": 10, "next_cursor": 11, "emitted": 1}

        def changes(self, after, limit):
            if after == 2:
                raise ApiError(410, {"reset": True})
            return {"changes": [event(11, "abc")] if after == 10 else []}

        def episode(self, guid, etag):
            return episode(guid, 1, "First episode"), '"current"'

    class Embedder:
        def embed(self, texts):
            return [pack_vector([0.1] * DIMENSION) for _ in texts]

    store = IndexStore(tmp_path / "index.db")
    try:
        insert(store, 1, episode("abc", 1, "First episode"))
        insert(store, 2, episode("def", 1, "Second episode"))
        with store.conn:
            store.conn.execute("UPDATE sync_state SET cursor=2,initialized=1 WHERE id=1")
        assert Indexer(store, Client(), Embedder()).sync_until_idle() == 1
        assert store.state() == (11, True, None)
        assert store.document("abc") is not None
        assert store.document("def") is None
    finally:
        store.close()


def test_short_turns_merge_with_labels():
    turns = [
        {"idx": 0, "speaker": "A", "start": 0, "end": 1, "text": "Welcome in!"},
        {"idx": 1, "speaker": "B", "start": 1, "end": 2, "text": "Great to be here."},
        {"idx": 2, "speaker": "A", "start": 2, "end": 40, "text": " ".join(["long"] * 400)},
    ]
    chunks = chunk_turns("abc", 1, turns)
    assert chunks[0].text == "A: Welcome in! B: Great to be here."
    assert chunks[0].speaker == "A"
    assert len(chunks) == 3 and chunks[1].text.startswith("long")


def test_ads_are_flagged_and_hidden_unless_requested(tmp_path):
    store = IndexStore(tmp_path / "index.db")
    try:
        insert(store, 1, episode("abc", 1, "At T -Mobile you can get nothing up front."))
        vector = pack_vector([0.1] * DIMENSION)
        assert store.search("T-Mobile", vector) == []
        assert len(store.search("T-Mobile", vector, include_ads=True)) == 1
    finally:
        store.close()
