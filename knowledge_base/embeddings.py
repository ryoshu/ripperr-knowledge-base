"""Local CPU embeddings and sqlite-vec's float32 wire format."""

from __future__ import annotations

import struct
import os
from pathlib import Path

MODEL_NAME = "minishlab/potion-retrieval-32M"
DIMENSION = 512


def pack_vector(values) -> bytes:
    numbers = [float(value) for value in values]
    if len(numbers) != DIMENSION:
        raise ValueError(f"expected {DIMENSION} embedding values, got {len(numbers)}")
    return struct.pack(f"<{DIMENSION}f", *numbers)


class LocalEmbedder:
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self._model = None

    @property
    def model(self):
        if self._model is None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            os.environ.setdefault("HF_HOME", str(self.cache_dir))
            from model2vec import StaticModel

            self._model = StaticModel.from_pretrained(MODEL_NAME, force_download=False)
        return self._model

    def embed(self, texts: list[str]) -> list[bytes]:
        if not texts:
            return []
        return [pack_vector(vector) for vector in self.model.encode(texts)]
