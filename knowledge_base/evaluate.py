"""Repeatable passage-level retrieval checks."""

from __future__ import annotations

import json
from pathlib import Path

from .embeddings import LocalEmbedder
from .store import IndexStore


def evaluate(store: IndexStore, embedder: LocalEmbedder, case_file: Path) -> dict:
    cases = [json.loads(line) for line in case_file.read_text().splitlines() if line.strip()]
    vectors = embedder.embed([case["query"] for case in cases])
    ranks: list[int | None] = []
    misses = []
    for case, vector in zip(cases, vectors, strict=True):
        hits = store.search(case["query"], vector, 10)
        target = float(case["time_s"])
        tolerance = float(case.get("tolerance_s", 60))
        rank = next((
            position for position, hit in enumerate(hits, 1)
            if hit["source_id"] == case["guid"]
            and hit["start_s"] - tolerance <= target <= hit["end_s"] + tolerance
        ), None)
        ranks.append(rank)
        if rank is None:
            misses.append({"query": case["query"], "guid": case["guid"]})
    found = [rank for rank in ranks if rank is not None]
    count = len(cases)
    return {
        "questions": count,
        "hit_at_5": sum(rank is not None and rank <= 5 for rank in ranks) / count if count else 0,
        "hit_at_10": len(found) / count if count else 0,
        "mean_rank_of_hits": sum(found) / len(found) if found else None,
        "misses": misses,
    }
