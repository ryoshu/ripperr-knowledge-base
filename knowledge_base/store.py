"""Transactional document, keyword, vector, and change cursor storage."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import sqlite_vec

from .chunking import CHUNKER_VERSION, OVERLAP_WORDS, Chunk
from .embeddings import DIMENSION, MODEL_NAME


class IndexStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        try:
            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
        finally:
            self.conn.enable_load_extension(False)
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        self.conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS index_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sync_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cursor INTEGER NOT NULL DEFAULT 0,
                initialized INTEGER NOT NULL DEFAULT 0,
                reset_cutoff INTEGER
            );
            INSERT OR IGNORE INTO sync_state(id) VALUES (1);
            CREATE TABLE IF NOT EXISTS reset_seen (
                guid TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS documents (
                doc_id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                url TEXT,
                title TEXT,
                published TEXT,
                fetched_at TEXT,
                revision INTEGER NOT NULL,
                feed_id INTEGER,
                feed_title TEXT,
                etag TEXT,
                meta_json TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS documents_source
                ON documents(source_type, source_id);
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY,
                chunk_id TEXT NOT NULL UNIQUE,
                doc_id TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
                revision INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                start_s REAL,
                end_s REAL,
                speaker TEXT,
                text TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id, ordinal);
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
                USING fts5(text, chunk_id UNINDEXED);
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks
                USING vec0(embedding float[{DIMENSION}]);
        """)
        expected = {
            "embedding_model": MODEL_NAME,
            "embedding_dimension": str(DIMENSION),
            "chunker_version": CHUNKER_VERSION,
        }
        with self.conn:
            for key, value in expected.items():
                existing = self.conn.execute(
                    "SELECT value FROM index_meta WHERE key=?", (key,)
                ).fetchone()
                if existing and existing["value"] != value:
                    raise RuntimeError(
                        f"index {key} is {existing['value']!r}, expected {value!r}; rebuild"
                    )
                self.conn.execute(
                    "INSERT OR IGNORE INTO index_meta(key,value) VALUES (?,?)", (key, value)
                )

    def state(self) -> tuple[int, bool, int | None]:
        row = self.conn.execute(
            "SELECT cursor,initialized,reset_cutoff FROM sync_state WHERE id=1"
        ).fetchone()
        return int(row["cursor"]), bool(row["initialized"]), row["reset_cutoff"]

    def begin_reset(self, after: int, cutoff: int) -> None:
        if cutoff < after:
            raise ValueError("bootstrap cutoff precedes after cursor")
        with self.conn:
            self.conn.execute("DELETE FROM reset_seen")
            self.conn.execute(
                "UPDATE sync_state SET cursor=?, initialized=1, reset_cutoff=? WHERE id=1",
                (after, cutoff),
            )

    def complete_reset(self) -> None:
        with self.conn:
            rows = self.conn.execute("""
                SELECT doc_id FROM documents
                WHERE source_type='podcast' AND source_id NOT IN
                    (SELECT guid FROM reset_seen)
            """).fetchall()
            for row in rows:
                self._delete_document(row["doc_id"])
            self.conn.execute("DELETE FROM reset_seen")
            self.conn.execute("UPDATE sync_state SET reset_cutoff=NULL WHERE id=1")

    def document(self, guid: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT doc_id,revision,etag FROM documents WHERE source_type='podcast' AND source_id=?",
            (guid,),
        ).fetchone()

    def _delete_document(self, doc_id: str) -> None:
        rows = self.conn.execute(
            "SELECT id FROM chunks WHERE doc_id=?", (doc_id,)
        ).fetchall()
        for row in rows:
            self.conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (row["id"],))
            self.conn.execute("DELETE FROM vec_chunks WHERE rowid=?", (row["id"],))
        self.conn.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))

    def apply_event(
        self,
        event: dict,
        episode: dict | None,
        etag: str | None,
        chunks: list[Chunk] | None = None,
        vectors: list[bytes] | None = None,
    ) -> None:
        """Commit all index changes and the event cursor as one transaction."""
        guid = event["episode_guid"]
        doc_id = "podcast:" + guid
        sequence = int(event["seq"])
        if chunks is not None and (vectors is None or len(chunks) != len(vectors)):
            raise ValueError("each chunk must have exactly one vector")
        with self.conn:
            current = self.conn.execute(
                "SELECT cursor FROM sync_state WHERE id=1"
            ).fetchone()["cursor"]
            if sequence <= current:
                return
            if event["kind"] == "deleted" or (episode is None and etag is None):
                self._delete_document(doc_id)
                self.conn.execute("DELETE FROM reset_seen WHERE guid=?", (guid,))
            elif episode is None:  # 304; stored content is still current.
                if self.document(guid) is None:
                    raise RuntimeError("304 received for an episode absent from the index")
                self.conn.execute("INSERT OR IGNORE INTO reset_seen(guid) VALUES (?)", (guid,))
            elif episode["status"] != "done":
                self._delete_document(doc_id)
                self.conn.execute("DELETE FROM reset_seen WHERE guid=?", (guid,))
            else:
                existing = self.document(guid)
                revision = int(episode["revision"])
                if existing is None or existing["revision"] != revision:
                    if chunks is None or vectors is None:
                        raise RuntimeError("missing chunks for a new transcript revision")
                    self._delete_document(doc_id)
                feed = episode["feed"]
                metadata = {
                    "speaker_names": {
                        item["speaker"]: item["name"]
                        for item in episode.get("speaker_names", [])
                    },
                    "summary": episode.get("summary"),
                    "duration": episode.get("duration"),
                    "audio_url": episode.get("audio_url"),
                }
                self.conn.execute("""
                    INSERT INTO documents
                        (doc_id,source_type,source_id,url,title,published,fetched_at,
                         revision,feed_id,feed_title,etag,meta_json)
                    VALUES (?,'podcast',?,?,?,?,NULL,?,?,?,?,?)
                    ON CONFLICT(doc_id) DO UPDATE SET
                        url=excluded.url,title=excluded.title,
                        published=excluded.published,revision=excluded.revision,
                        feed_id=excluded.feed_id,feed_title=excluded.feed_title,
                        etag=excluded.etag,meta_json=excluded.meta_json
                """, (
                    doc_id, guid, episode.get("source_url"), episode.get("title"),
                    episode.get("published"), revision, feed["id"], feed.get("title"),
                    etag, json.dumps(metadata, separators=(",", ":")),
                ))
                if chunks is not None:
                    for chunk, vector in zip(chunks, vectors, strict=True):
                        cursor = self.conn.execute("""
                            INSERT INTO chunks
                                (chunk_id,doc_id,revision,ordinal,start_s,end_s,speaker,text)
                            VALUES (?,?,?,?,?,?,?,?)
                        """, (
                            chunk.chunk_id, doc_id, revision, chunk.ordinal,
                            chunk.start_s, chunk.end_s, chunk.speaker, chunk.text,
                        ))
                        rowid = cursor.lastrowid
                        self.conn.execute(
                            "INSERT INTO chunks_fts(rowid,text,chunk_id) VALUES (?,?,?)",
                            (rowid, chunk.text, chunk.chunk_id),
                        )
                        self.conn.execute(
                            "INSERT INTO vec_chunks(rowid,embedding) VALUES (?,?)",
                            (rowid, vector),
                        )
                self.conn.execute("INSERT OR IGNORE INTO reset_seen(guid) VALUES (?)", (guid,))
            self.conn.execute("UPDATE sync_state SET cursor=? WHERE id=1", (sequence,))

    def counts(self) -> dict[str, int]:
        return {
            "documents": self.conn.execute("SELECT count(*) FROM documents").fetchone()[0],
            "chunks": self.conn.execute("SELECT count(*) FROM chunks").fetchone()[0],
            "vectors": self.conn.execute("SELECT count(*) FROM vec_chunks").fetchone()[0],
        }

    def span(self, guid: str, start_s: float, end_s: float) -> dict | None:
        """The podcast episode's chunks overlapping [start_s, end_s], in order,
        with the words a split long turn repeats removed. None if not indexed."""
        doc = self.conn.execute(
            "SELECT * FROM documents WHERE source_type='podcast' AND source_id=?", (guid,)
        ).fetchone()
        if doc is None:
            return None
        rows = self.conn.execute("""
            SELECT ordinal,start_s,end_s,speaker,text FROM chunks
            WHERE doc_id=? AND end_s>=? AND start_s<=? ORDER BY ordinal
        """, (doc["doc_id"], start_s, end_s)).fetchall()
        names = json.loads(doc["meta_json"])["speaker_names"]
        passages: list[dict] = []
        for row in rows:
            text = row["text"]
            previous = passages[-1] if passages else None
            if (previous and previous["ordinal"] == row["ordinal"] - 1
                    and previous["speaker"] == row["speaker"] and row["start_s"] < previous["end_s"]):
                text = " ".join(text.split()[OVERLAP_WORDS:])
            passages.append({**dict(row), "text": text,
                             "speaker_name": names.get(row["speaker"], row["speaker"])})
        document = dict(doc)
        document.pop("meta_json")
        return {"document": document, "passages": passages}

    def sources(self) -> list[dict]:
        """What is indexed, per source type and feed."""
        return [dict(row) for row in self.conn.execute("""
            SELECT source_type, feed_id, feed_title, count(*) AS documents,
                   min(published) AS earliest, max(published) AS latest
            FROM documents GROUP BY source_type, feed_id ORDER BY source_type, feed_title
        """)]

    def search(self, query: str, vector: bytes, k: int = 10) -> list[dict]:
        if not 1 <= k <= 50:
            raise ValueError("k must be between 1 and 50")
        pool = min(200, max(k * 5, 30))
        terms = re.findall(r"\w+", query, flags=re.UNICODE)[:12]
        keyword_ids: list[int] = []
        if terms:
            expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
            keyword_ids = [row[0] for row in self.conn.execute("""
                SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?
                ORDER BY bm25(chunks_fts) LIMIT ?
            """, (expression, pool))]
        vector_ids = [row[0] for row in self.conn.execute("""
            SELECT rowid FROM vec_chunks
            WHERE embedding MATCH ? AND k = ? ORDER BY distance
        """, (vector, pool))]
        scores: dict[int, float] = {}
        for ids in (keyword_ids, vector_ids):
            for rank, rowid in enumerate(ids, 1):
                scores[rowid] = scores.get(rowid, 0.0) + 1.0 / (60 + rank)
        ranked = sorted(scores, key=lambda rowid: (-scores[rowid], rowid))[:k]
        results = []
        for rowid in ranked:
            row = self.conn.execute("""
                SELECT c.chunk_id,c.ordinal,c.start_s,c.end_s,c.speaker,c.text,
                       d.source_type,d.source_id,d.url,d.title,d.published,d.revision,
                       d.feed_id,d.feed_title,d.meta_json
                FROM chunks c JOIN documents d ON d.doc_id=c.doc_id WHERE c.id=?
            """, (rowid,)).fetchone()
            if row is None:
                continue
            item = dict(row)
            names = json.loads(item.pop("meta_json"))["speaker_names"]
            item["speaker_name"] = names.get(item["speaker"], item["speaker"])
            item["score"] = scores[rowid]
            results.append(item)
        return results
