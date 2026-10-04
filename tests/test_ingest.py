from starlette.testclient import TestClient

from knowledge_base.embeddings import DIMENSION, pack_vector
from knowledge_base.ingest import asgi_app, canonical_url
from knowledge_base.mcp_server import KnowledgeBase


class FixedEmbedder:
    def embed(self, texts):
        return [pack_vector([0.1] * DIMENSION) for _ in texts]


def test_canonical_url_drops_noise():
    assert canonical_url("HTTPS://Example.com/a/?utm_source=x&b=2&a=1#top") == "https://example.com/a?a=1&b=2"


def test_ingest_dedups_updates_and_is_searchable(tmp_path):
    db = tmp_path / "index.db"
    client = TestClient(asgi_app(db, FixedEmbedder(), "tok"))
    auth = {"Authorization": "Bearer tok"}
    page = {"url": "https://Example.com/post/?utm_x=1", "title": "Post", "markdown": "Hello waiver wire.\n\nSecond paragraph."}

    assert client.post("/v1/pages", json=page).status_code == 401
    assert client.post("/v1/pages", json=page, headers=auth).json() == {
        "status": "created", "url": "https://example.com/post", "chunks": 1}
    assert client.post("/v1/pages", json=page, headers=auth).json()["status"] == "unchanged"
    page["markdown"] = "Rewritten waiver wire advice."
    assert client.post("/v1/pages", json=page, headers=auth).json()["status"] == "updated"
    assert client.post("/v1/pages", json={"url": "ftp://x", "markdown": "a"}, headers=auth).status_code == 400
    assert client.post("/v1/pages", content=b"x" * 2_000_001, headers=auth).status_code == 413

    result = KnowledgeBase(db, FixedEmbedder()).search("waiver wire", source_type="web")
    assert [r["quote"] for r in result["results"]] == ["Rewritten waiver wire advice."]
    assert result["results"][0]["citation"]["url"] == "https://example.com/post"
