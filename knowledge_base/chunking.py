"""Deterministic, speaker-scoped transcript windows."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

CHUNK_WORDS = 350
OVERLAP_WORDS = 35
MIN_WORDS = 20
CHUNKER_VERSION = "speaker-words-v2-350-35-min20"


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    ordinal: int
    start_s: float | None
    end_s: float | None
    speaker: str | None
    text: str


def chunk_turns(guid: str, revision: int, turns: list[dict]) -> list[Chunk]:
    """Split at speaker changes, merge short neighbouring runs; estimate word times."""
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

    # Pack neighbouring runs into one chunk while either side is short (<MIN_WORDS)
    # and the total fits CHUNK_WORDS. ponytail: greedy, one pass; no look-ahead.
    packed: list[list[tuple[str, list]]] = []
    size = 0
    for group in groups:
        n = len(group[1])
        if packed and (size < MIN_WORDS or n < MIN_WORDS) and size + n <= CHUNK_WORDS:
            packed[-1].append(group)
            size += n
        else:
            packed.append([group])
            size = n

    chunks: list[Chunk] = []

    def add(speaker: str, window: list, text: str) -> None:
        ordinal = len(chunks)
        identity = f"{guid}\0{revision}\0{ordinal}".encode()
        chunks.append(Chunk(
            chunk_id=hashlib.sha256(identity).hexdigest()[:32],
            ordinal=ordinal,
            start_s=window[0][1],
            end_s=window[-1][2],
            speaker=speaker,
            text=text,
        ))

    for run in packed:
        if len(run) > 1:
            # Mixed chunk: speaker column holds the first speaker; text keeps every label.
            words = [w for _, ws in run for w in ws]
            text = " ".join(f"{sp}: " + " ".join(w for w, _, _ in ws) for sp, ws in run)
            add(run[0][0], words, text)
            continue
        speaker, words = run[0]
        offset = 0
        while offset < len(words):
            window = words[offset : offset + CHUNK_WORDS]
            add(speaker, window, " ".join(w for w, _, _ in window))
            if offset + CHUNK_WORDS >= len(words):
                break
            offset += CHUNK_WORDS - OVERLAP_WORDS
    return chunks


def chunk_markdown(source_id: str, revision: int, markdown: str) -> list[Chunk]:
    """Pack paragraphs into windows of up to CHUNK_WORDS; a longer paragraph is
    split by words with the same overlap as transcripts. No timings or speaker."""
    windows: list[list[str]] = []
    pending: list[str] = []
    size = 0
    for paragraph in re.split(r"\n\s*\n", markdown):
        n = len(paragraph.split())
        if not n:
            continue
        if pending and size + n > CHUNK_WORDS:
            windows.append(pending)
            pending, size = [], 0
        if n > CHUNK_WORDS:
            words = paragraph.split()
            for offset in range(0, n, CHUNK_WORDS - OVERLAP_WORDS):
                windows.append([" ".join(words[offset : offset + CHUNK_WORDS])])
                if offset + CHUNK_WORDS >= n:
                    break
            continue
        pending.append(paragraph.strip())
        size += n
    if pending:
        windows.append(pending)
    chunks = []
    for ordinal, window in enumerate(windows):
        identity = f"{source_id}\0{revision}\0{ordinal}".encode()
        chunks.append(Chunk(
            chunk_id=hashlib.sha256(identity).hexdigest()[:32], ordinal=ordinal,
            start_s=None, end_s=None, speaker=None, text="\n\n".join(window),
        ))
    return chunks
