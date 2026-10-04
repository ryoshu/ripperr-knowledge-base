"""Deterministic, speaker-scoped transcript windows."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

CHUNK_WORDS = 350
OVERLAP_WORDS = 35
CHUNKER_VERSION = "speaker-words-v1-350-35"


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    ordinal: int
    start_s: float
    end_s: float
    speaker: str
    text: str


def chunk_turns(guid: str, revision: int, turns: list[dict]) -> list[Chunk]:
    """Split at speaker changes; estimate word times within long turns."""
    groups: list[tuple[str, list[tuple[str, float, float]]]] = []
    for turn in turns:
        words = str(turn["text"]).split()
        if not words:
            continue
        speaker = str(turn["speaker"])
        start = float(turn["start"])
        end = max(start, float(turn["end"]))
        if not groups or groups[-1][0] != speaker:
            groups.append((speaker, []))
        timed = groups[-1][1]
        duration = end - start
        count = len(words)
        for position, word in enumerate(words):
            timed.append((
                word,
                start + duration * position / count,
                start + duration * (position + 1) / count,
            ))

    chunks: list[Chunk] = []
    for speaker, words in groups:
        offset = 0
        while offset < len(words):
            window = words[offset : offset + CHUNK_WORDS]
            ordinal = len(chunks)
            identity = f"{guid}\0{revision}\0{ordinal}".encode()
            chunks.append(Chunk(
                chunk_id=hashlib.sha256(identity).hexdigest()[:32],
                ordinal=ordinal,
                start_s=window[0][1],
                end_s=window[-1][2],
                speaker=speaker,
                text=" ".join(word for word, _, _ in window),
            ))
            if offset + CHUNK_WORDS >= len(words):
                break
            offset += CHUNK_WORDS - OVERLAP_WORDS
    return chunks
