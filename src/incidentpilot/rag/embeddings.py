"""Embedding backends for the runbook index.

Two backends, selected by config:

* ``voyage``  -- Voyage AI embeddings (Anthropic's recommended embedding partner).
                 Used when VOYAGE_API_KEY is set and the SDK is installed.
* ``hashed``  -- a deterministic hashed bag-of-ngrams projection. No network, no
                 model download, stable across runs. This is what keeps the eval
                 reproducible on a laptop and in CI.

Both return L2-normalized float vectors, so cosine similarity is a plain dot product.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Iterable, Protocol

_TOKEN_RE = re.compile(r"[a-z0-9]+")
HASHED_DIM = 384


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _ngrams(tokens: list[str], n: int) -> Iterable[str]:
    for i in range(len(tokens) - n + 1):
        yield " ".join(tokens[i:i + n])


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]: ...


class HashedEmbedder:
    """Feature hashing over unigrams + bigrams with sublinear TF and L2 norm.

    Not a semantic model -- it captures lexical overlap and shared phrasing, which is
    most of what runbook retrieval needs when it is fused with BM25. It exists so the
    system (and its eval) runs with zero external dependencies.
    """

    name = "hashed"

    def __init__(self, dim: int = HASHED_DIM):
        self.dim = dim

    def _bucket(self, feature: str) -> tuple[int, float]:
        digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
        idx = int.from_bytes(digest[:4], "big") % self.dim
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        return idx, sign

    def embed_one(self, text: str) -> list[float]:
        tokens = tokenize(text)
        counts: dict[str, int] = {}
        for feature in [*tokens, *_ngrams(tokens, 2)]:
            counts[feature] = counts.get(feature, 0) + 1

        vec = [0.0] * self.dim
        for feature, count in counts.items():
            idx, sign = self._bucket(feature)
            weight = 1.0 + math.log(count)          # sublinear TF
            if " " in feature:
                weight *= 1.4                        # bigrams carry more signal
            vec[idx] += sign * weight

        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return vec
        return [v / norm for v in vec]

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        return [self.embed_one(t) for t in texts]


class VoyageEmbedder:
    """voyage-3.5 embeddings. Requires ``pip install voyageai`` and VOYAGE_API_KEY."""

    name = "voyage"

    def __init__(self, model: str = "voyage-3.5", batch_size: int = 64):
        import voyageai  # imported lazily so the package stays optional

        self.client = voyageai.Client()
        self.model = model
        self.batch_size = batch_size
        self.dim = 1024

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start:start + self.batch_size]
            result = self.client.embed(batch, model=self.model, input_type=input_type)
            vectors.extend(result.embeddings)
        return [_l2(v) for v in vectors]


def _l2(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    return vec if norm == 0.0 else [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def get_embedder(backend: str = "auto") -> Embedder:
    """Resolve the configured backend, falling back to `hashed` when Voyage is unusable."""
    if backend in ("auto", "voyage") and os.environ.get("VOYAGE_API_KEY"):
        try:
            return VoyageEmbedder()
        except Exception:
            if backend == "voyage":
                raise
    return HashedEmbedder()
