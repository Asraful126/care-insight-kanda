"""Policy search over the public standards PDFs: local embeddings + BM25, fused, with page citations.

Decisions (user-approved, Day 5):
- fastembed (ONNX runtime, no torch) with BAAI/bge-small-en-v1.5: small model and small Docker image.
- Page-aware chunks: ~1,000 characters with ~150 overlap, never across a page boundary, so every hit
  cites exactly one PDF page. Chunks are packed from whole lines.
- Running page headers/footers (lines on most pages, e.g. "NDIS Quality and Safeguards Commission 5")
  are stripped first; otherwise they dominate both the BM25 and the embedding of short chunks.
- Hybrid retrieval: cosine similarity and BM25 ranked separately, then merged with reciprocal rank
  fusion (RRF, k=60). Embeddings find paraphrases ("rights of older people"); BM25 finds exact codes
  ("Outcome 3.1", "subcutaneous"). RRF needs no score normalisation between the two.
- No vector database: a few hundred chunks fit in one .npz file and a matrix multiply.

Build:  python -m assistant.policy_search build
Query:  python -m assistant.policy_search "how often must care plans be reviewed?"
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
POLICY_DIR = ROOT / "data/raw/policy"
INDEX_DIR = ROOT / "data/index"
MODEL = "BAAI/bge-small-en-v1.5"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "  # bge v1.5 query instruction
CHUNK_CHARS, OVERLAP_CHARS = 1000, 150
RRF_K = 60

DOCUMENTS = {
    "ndis_practice_standards_v4.pdf": "NDIS Practice Standards and Quality Indicators (v4, NDIS Quality and Safeguards Commission)",
    "strengthened_aged_care_quality_standards.pdf": "Strengthened Aged Care Quality Standards (November 2025)",
}


@dataclass
class Chunk:
    chunk_id: int
    file: str
    title: str
    page: int  # 1-based PDF page (what a PDF viewer shows), not the printed page label
    text: str


# --- build ---------------------------------------------------------------------------------------

def _page_lines(path: Path) -> list[list[str]]:
    from pypdf import PdfReader

    pages = [(p.extract_text() or "").splitlines() for p in PdfReader(path).pages]
    pages = [[re.sub(r"\s+", " ", ln).strip() for ln in lines] for lines in pages]
    pages = [[ln for ln in lines if ln] for lines in pages]
    # Running headers/footers: the same line (digits ignored, so page numbers don't matter) on >50% of pages.
    norm = lambda ln: re.sub(r"\d+", "#", ln)  # noqa: E731
    counts = Counter(k for lines in pages for k in {norm(ln) for ln in lines})
    boiler = {k for k, n in counts.items() if n > len(pages) / 2}
    pages = [[ln for ln in lines if norm(ln) not in boiler] for lines in pages]
    # Table-of-contents pages name every outcome code but hold no content, so they win exact-code
    # queries ("Outcome 3.1") for the wrong reason. Drop pages where most lines end in a page number.
    toc = lambda lines: sum(bool(re.search(r"(\.{4,}|\s)\d{1,3}$", ln)) for ln in lines) > 0.5 * len(lines)  # noqa: E731
    return [[] if lines and toc(lines) else lines for lines in pages]


def _chunk_page(lines: list[str]) -> list[str]:
    chunks, start = [], 0
    while start < len(lines):
        end, size = start, 0
        while end < len(lines) and (size == 0 or size + len(lines[end]) <= CHUNK_CHARS):
            size += len(lines[end]) + 1
            end += 1
        chunks.append(" ".join(lines[start:end]))
        if end >= len(lines):
            break
        # Overlap: step back over trailing lines worth ~OVERLAP_CHARS, but always move forward.
        back, carried = end, 0
        while back - 1 > start and carried + len(lines[back - 1]) <= OVERLAP_CHARS:
            back -= 1
            carried += len(lines[back]) + 1
        start = back
    return chunks


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:\.[0-9]+)*", text.lower())  # keeps "3.1.5" as one token


def _embedder():
    from fastembed import TextEmbedding

    return TextEmbedding(MODEL)


def build() -> None:
    chunks: list[Chunk] = []
    for file, title in DOCUMENTS.items():
        for page_no, lines in enumerate(_page_lines(POLICY_DIR / file), start=1):
            for text in _chunk_page(lines):
                if len(text) >= 80:  # skip near-empty pages (covers, blank backs)
                    chunks.append(Chunk(len(chunks), file, title, page_no, text))
    vectors = np.array(list(_embedder().embed([f"{c.title}\n{c.text}" for c in chunks])), dtype=np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    np.save(INDEX_DIR / "policy_vectors.npy", vectors)
    (INDEX_DIR / "policy_chunks.json").write_text(json.dumps([asdict(c) for c in chunks], indent=0))
    per_doc = Counter(c.file for c in chunks)
    print(f"indexed {len(chunks)} chunks ({dict(per_doc)}), dim {vectors.shape[1]} -> {INDEX_DIR}")


# --- search --------------------------------------------------------------------------------------

class PolicyIndex:
    def __init__(self) -> None:
        from rank_bm25 import BM25Okapi

        path = INDEX_DIR / "policy_chunks.json"
        if not path.exists():
            raise FileNotFoundError(f"{path} missing: run `python -m assistant.policy_search build`")
        self.chunks = [Chunk(**c) for c in json.loads(path.read_text())]
        self.vectors = np.load(INDEX_DIR / "policy_vectors.npy")
        self.bm25 = BM25Okapi([_tokens(c.text) for c in self.chunks])
        self.embedder = _embedder()

    def search(self, query: str, k: int = 5) -> list[dict]:
        q = np.array(next(iter(self.embedder.embed([QUERY_PREFIX + query]))), dtype=np.float32)
        cosine = self.vectors @ (q / np.linalg.norm(q))
        bm25 = self.bm25.get_scores(_tokens(query))
        rrf = np.zeros(len(self.chunks))
        for scores in (cosine, bm25):
            ranks = np.empty(len(scores), dtype=int)
            ranks[np.argsort(-scores)] = np.arange(1, len(scores) + 1)
            rrf += 1.0 / (RRF_K + ranks)
        top = np.argsort(-rrf)[:k]
        return [
            {
                "citation": f"{self.chunks[i].title}, p. {self.chunks[i].page}",
                "file": self.chunks[i].file,
                "page": self.chunks[i].page,
                "score": round(float(rrf[i]), 5),
                "text": self.chunks[i].text,
            }
            for i in top
        ]


@lru_cache(maxsize=1)
def index() -> PolicyIndex:
    return PolicyIndex()


def search_policy(query: str, k: int = 5) -> list[dict]:
    return index().search(query, k)


if __name__ == "__main__":
    if sys.argv[1:] == ["build"]:
        build()
    else:
        for hit in search_policy(" ".join(sys.argv[1:])):
            print(f"[{hit['score']}] {hit['citation']}\n  {hit['text'][:220]}…\n")
