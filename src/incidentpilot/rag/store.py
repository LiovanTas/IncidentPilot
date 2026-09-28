"""SQLite-backed hybrid retriever over the runbook corpus.

Indexing:  markdown -> heading-aware chunks -> (BM25 postings, dense vector) in SQLite.
Retrieval: BM25 and cosine are ranked independently, then fused with Reciprocal Rank
           Fusion. RRF is used rather than a score blend because BM25 and cosine
           scores live on incomparable scales.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from ..models import RunbookChunk
from .embeddings import Embedder, cosine, get_embedder, tokenize

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id    TEXT PRIMARY KEY,
    runbook_id  TEXT NOT NULL,
    title       TEXT NOT NULL,
    heading     TEXT NOT NULL,
    text        TEXT NOT NULL,
    path        TEXT NOT NULL,
    length      INTEGER NOT NULL,
    tokens      TEXT NOT NULL,
    vector      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_chunks_runbook ON chunks(runbook_id);
"""

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")
_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "has", "have", "from", "are", "was",
    "you", "our", "not", "but", "all", "can", "will", "its", "into", "when", "then",
}

BM25_K1 = 1.5
BM25_B = 0.75
RRF_K = 60


@dataclass
class _Doc:
    chunk_id: str
    runbook_id: str
    title: str
    heading: str
    text: str
    path: str
    tokens: list[str]
    vector: list[float]


def chunk_markdown(path: Path, max_chars: int = 1200) -> list[dict]:
    """Split a runbook into heading-scoped chunks, further split if a section is long."""
    raw = path.read_text(encoding="utf-8")
    runbook_id = path.stem
    title = runbook_id.replace("-", " ").title()
    lines = raw.splitlines()
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()

    sections: list[tuple[str, list[str]]] = []
    current_heading = title
    buffer: list[str] = []
    for line in lines:
        match = _HEADING_RE.match(line)
        if match:
            if buffer:
                sections.append((current_heading, buffer))
                buffer = []
            current_heading = match.group(2).strip()
        else:
            buffer.append(line)
    if buffer:
        sections.append((current_heading, buffer))

    chunks: list[dict] = []
    for heading, body_lines in sections:
        body = "\n".join(body_lines).strip()
        if not body:
            continue
        pieces = [body]
        if len(body) > max_chars:
            pieces = []
            paragraph_buf: list[str] = []
            size = 0
            for para in body.split("\n\n"):
                if size + len(para) > max_chars and paragraph_buf:
                    pieces.append("\n\n".join(paragraph_buf))
                    paragraph_buf, size = [], 0
                paragraph_buf.append(para)
                size += len(para)
            if paragraph_buf:
                pieces.append("\n\n".join(paragraph_buf))
        for i, piece in enumerate(pieces):
            chunks.append({
                "chunk_id": f"{runbook_id}#{len(chunks)}",
                "runbook_id": runbook_id,
                "title": title,
                "heading": heading,
                "text": piece,
                "path": str(path),
                "part": i,
            })
    return chunks


class RunbookIndex:
    def __init__(self, db_path: Path, embedder: Embedder | None = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.embedder = embedder or get_embedder()
        self._docs: list[_Doc] | None = None

    # ------------------------------------------------------------------ indexing

    def build(self, runbook_dir: Path) -> int:
        """(Re)index every markdown file under `runbook_dir`. Returns the chunk count."""
        files = sorted(Path(runbook_dir).glob("*.md"))
        if not files:
            raise FileNotFoundError(f"no runbooks found in {runbook_dir}")

        records: list[dict] = []
        for path in files:
            records.extend(chunk_markdown(path))

        payloads = [f"{r['title']} :: {r['heading']}\n{r['text']}" for r in records]
        vectors = self.embedder.embed(payloads, input_type="document")

        self.conn.execute("DELETE FROM chunks")
        self.conn.executemany(
            "INSERT INTO chunks (chunk_id, runbook_id, title, heading, text, path, length, tokens, vector)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    r["chunk_id"], r["runbook_id"], r["title"], r["heading"], r["text"], r["path"],
                    len(tokenize(payload)), json.dumps(tokenize(payload)), json.dumps(vector),
                )
                for r, payload, vector in zip(records, payloads, vectors)
            ],
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("embedder", self.embedder.name),
        )
        self.conn.commit()
        self._docs = None
        return len(records)

    # ----------------------------------------------------------------- retrieval

    def _load(self) -> list[_Doc]:
        if self._docs is None:
            rows = self.conn.execute("SELECT * FROM chunks").fetchall()
            self._docs = [
                _Doc(
                    chunk_id=r["chunk_id"], runbook_id=r["runbook_id"], title=r["title"],
                    heading=r["heading"], text=r["text"], path=r["path"],
                    tokens=json.loads(r["tokens"]), vector=json.loads(r["vector"]),
                )
                for r in rows
            ]
        return self._docs

    @property
    def size(self) -> int:
        return len(self._load())

    def _bm25_ranking(self, query: str) -> list[tuple[str, float]]:
        docs = self._load()
        if not docs:
            return []
        n = len(docs)
        avg_len = sum(len(d.tokens) for d in docs) / n
        df: Counter[str] = Counter()
        for doc in docs:
            df.update(set(doc.tokens))

        q_terms = [t for t in tokenize(query) if t not in _STOPWORDS and len(t) > 2]
        scored: list[tuple[str, float]] = []
        for doc in docs:
            tf = Counter(doc.tokens)
            score = 0.0
            dl = len(doc.tokens) or 1
            for term in q_terms:
                freq = tf.get(term, 0)
                if not freq:
                    continue
                idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                denom = freq + BM25_K1 * (1 - BM25_B + BM25_B * dl / avg_len)
                score += idf * (freq * (BM25_K1 + 1)) / denom
            if score > 0:
                scored.append((doc.chunk_id, score))
        scored.sort(key=lambda kv: -kv[1])
        return scored

    def _dense_ranking(self, query: str) -> list[tuple[str, float]]:
        docs = self._load()
        if not docs:
            return []
        q_vec = self.embedder.embed([query], input_type="query")[0]
        scored = [(d.chunk_id, cosine(q_vec, d.vector)) for d in docs]
        scored = [(cid, s) for cid, s in scored if s > 0]
        scored.sort(key=lambda kv: -kv[1])
        return scored

    def search(self, query: str, top_k: int = 5) -> list[RunbookChunk]:
        """Hybrid BM25 + dense retrieval fused with Reciprocal Rank Fusion."""
        docs = {d.chunk_id: d for d in self._load()}
        if not docs:
            return []

        fused: dict[str, float] = {}
        for ranking in (self._bm25_ranking(query), self._dense_ranking(query)):
            for rank, (chunk_id, _score) in enumerate(ranking[: top_k * 4]):
                fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank + 1)

        ordered = sorted(fused.items(), key=lambda kv: -kv[1])

        # One chunk per runbook in the head of the list, so the agent sees breadth
        # before depth; extra chunks from the same runbook fill in behind.
        primary: list[str] = []
        overflow: list[str] = []
        seen_runbooks: set[str] = set()
        for chunk_id, _ in ordered:
            runbook = docs[chunk_id].runbook_id
            (overflow if runbook in seen_runbooks else primary).append(chunk_id)
            seen_runbooks.add(runbook)

        results: list[RunbookChunk] = []
        for chunk_id in (primary + overflow)[:top_k]:
            doc = docs[chunk_id]
            results.append(RunbookChunk(
                chunk_id=doc.chunk_id, runbook_id=doc.runbook_id, title=doc.title,
                heading=doc.heading, text=doc.text, path=doc.path,
                score=round(fused[chunk_id], 5),
            ))
        return results

    def close(self) -> None:
        self.conn.close()
