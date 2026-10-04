from __future__ import annotations

import asyncio
import json

import pytest

from knowledge_base import mcp_server
from knowledge_base.embeddings import DIMENSION, pack_vector
from knowledge_base.mcp_server import BearerAuth, KnowledgeBase
from knowledge_base.store import IndexStore

from test_index import episode, insert


class FixedEmbedder:
    def embed(self, texts):
        return [pack_vector([0.1] * DIMENSION) for _ in texts]


def make(tmp_path, *episodes) -> KnowledgeBase:
    db = tmp_path / "index.db"
    store = IndexStore(db)
    for seq, data in enumerate(episodes, 1):
        insert(store, seq, data)
    store.close()
    return KnowledgeBase(db, FixedEmbedder())


def test_search_cites_marks_quotes_untrusted_and_filters(tmp_path):
    youtube = {**episode("yt", 1, "dynasty rebuild strategy", name="Bloom"),
               "source_url": "https://youtube.com/watch?v=abc"}
    kb = make(tmp_path, youtube, episode("rss", 1, "dynasty rebuild talk", name="Host"))

    found = kb.search("dynasty rebuild")
    assert "untrusted" in found["notice"]
    [first, second] = found["results"]
    assert {r["citation"]["guid"] for r in found["results"]} == {"yt", "rss"}
    yt = next(r for r in found["results"] if r["citation"]["guid"] == "yt")["citation"]
    assert yt["url"] == "https://youtube.com/watch?v=abc&t=12"
    assert (yt["timestamp"], yt["speaker"], yt["feed"], yt["revision"]) == ("0:00:12", "Bloom", "Example Feed", 1)

    assert [r["citation"]["guid"] for r in kb.search("dynasty", speaker="bloom")["results"]] == ["yt"]
    assert kb.search("dynasty", feed="nope")["results"] == []
    assert kb.search("dynasty", published_after="2027")["results"] == []
    with pytest.raises(ValueError):
        kb.search("  ")


def test_span_drops_words_a_split_turn_repeats(tmp_path):
    words = [f"w{n}" for n in range(500)]  # one long turn: two chunks sharing 35 words
    long_turn = episode("long", 1, " ".join(words))
    long_turn["turns"][0].update(start=0, end=500)
    kb = make(tmp_path, long_turn)

    span = kb.get_span("long", 0, 500)
    assert len(span["passages"]) == 2
    assert " ".join(p["quote"] for p in span["passages"]).split() == words
    assert "untrusted" in span["notice"]
    with pytest.raises(ValueError):
        kb.get_span("long", 0, 5000)
    with pytest.raises(ValueError):
        kb.get_span("missing", 0, 10)


def test_responses_are_capped(tmp_path, monkeypatch):
    kb = make(tmp_path, episode("a", 1, "alpha " * 50), episode("b", 1, "alpha " * 50))
    monkeypatch.setattr(mcp_server, "MAX_QUOTE_CHARS", 400)
    found = kb.search("alpha")
    assert len(found["results"]) == 1 and found["truncated"] is True


def test_list_sources_counts_feeds(tmp_path):
    kb = make(tmp_path, episode("a", 1, "x"), episode("b", 1, "y"))
    listed = kb.list_sources()
    assert listed["sources"][0]["feed_title"] == "Example Feed"
    assert listed["sources"][0]["documents"] == 2


def test_bearer_auth_rejects_missing_or_wrong_token():
    reached = []

    async def app(scope, receive, send):
        reached.append(scope["path"])

    async def call(headers):
        sent = []

        async def send(message):
            sent.append(message)

        await BearerAuth(app, "secret")({"type": "http", "path": "/mcp", "headers": headers}, None, send)
        return sent[0]["status"] if sent else 200

    assert asyncio.run(call([])) == 401
    assert asyncio.run(call([(b"authorization", b"Bearer wrong")])) == 401
    assert asyncio.run(call([(b"authorization", b"Bearer secret")])) == 200
    assert reached == ["/mcp"]


class FakeRipperr:
    def __init__(self):
        self.added = []

    def feeds(self):
        return [{"id": 1, "url": "https://known.example/rss"}]

    def add_feed(self, url, title=None, backfill=None):
        self.added.append((url, title, backfill))
        return {"feed": {"id": 2, "url": url, "title": title}, **({"backfilled": backfill} if backfill else {})}


def test_discover_and_subscribe_tools(tmp_path):
    from test_discover import RSS, fake

    results = {"results": [{"url": "https://known.example/rss", "title": "Known"},
                           {"url": "https://new.example/rss", "title": "New"}]}
    get = fake({
        "https://search/search?q=dynasty&categories=podcasts&format=json": json.dumps(results).encode(),
        "https://known.example/rss": RSS, "https://new.example/rss": RSS,
        "https://not-a-feed.example/": b"<html></html>",
    })
    ripperr = FakeRipperr()
    discovery = mcp_server.Discovery(ripperr, "https://search", get)

    found = discovery.discover("dynasty")
    assert "untrusted" in found["notice"]
    assert [(c["feed_url"], c["subscribed"]) for c in found["candidates"]] == [
        ("https://known.example/rss", True), ("https://new.example/rss", False)]

    done = discovery.subscribe("https://new.example/rss", backfill=3)
    assert ripperr.added == [("https://new.example/rss", "Dynasty Nerds", 3)]
    assert done["backfilled"] == 3
    with pytest.raises(ValueError):
        discovery.subscribe("https://not-a-feed.example/")
    with pytest.raises(ValueError):
        discovery.subscribe("https://new.example/rss", backfill=500)
    assert len(ripperr.added) == 1


def test_subscribe_is_the_only_tool_marked_as_a_write(tmp_path):
    server = mcp_server.build_server(make(tmp_path), mcp_server.Discovery(FakeRipperr(), "https://search"))
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert set(tools) == {"kb_search", "kb_get_span", "kb_list_sources", "kb_discover", "kb_subscribe"}
    assert tools["kb_discover"].annotations.read_only_hint is True
    assert tools["kb_subscribe"].annotations.read_only_hint is False
