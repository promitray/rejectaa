"""Search inside one paper: which paragraphs speak to a question ("how were the hyperparameters tuned?").

Two readings of relevance, fused:
- keywords (BM25 over the paragraph's words), which finds exact terms ("Friedman", "5-fold", "scaffold");
- meaning (cosine similarity of embeddings), which finds the same idea in other words ("searched over 50
  configurations" for "tuning budget").
Their ranks are combined (reciprocal rank fusion). Without embeddings (another provider, or the embedding call
failed) the keyword ranking is used alone.

Deterministic: scores are rounded and ties go to the earlier paragraph, so the same paper and question always
return the same passages in the same order. Embeddings are cached by text (services.local_store).
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from config import settings
from services.local_store import digest, get_store
from services.paper_index import PaperIndex, sort_key
from services.textfold import fold

logger = logging.getLogger(__name__)

BM25_K1 = 1.4
BM25_B = 0.75
RRF_K = 60
SCORE_DIGITS = 6
MAX_EMBED_CHARS = 6000

_STOP = frozenset(
    "a an and are as at be been but by can could did do does for from had has have how if in into is it its may "
    "might more most no not of on or our over should so such than that the their them then there these they this "
    "those through to under up was we were what when where which while who why will with within would you your "
    "also using used use based between both each other only same all any some very".split()
)


def tokens(text: str) -> list[str]:
    """Words for keyword search: lower case, Greek letters folded, small words dropped, a light stem."""
    words = re.findall(r"[a-z0-9]+", fold(text.lower()))
    return [_stem(w) for w in words if w not in _STOP and (len(w) > 1 or w.isdigit())]


def _stem(word: str) -> str:
    for suffix in ("ations", "ation", "ings", "ing", "ies", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


@dataclass
class PaperSearch:
    index: PaperIndex
    vectors: dict[str, list[float]] = field(default_factory=dict)  # paragraph id -> embedding
    model: str = ""
    _docs: dict[str, Counter[str]] = field(init=False, default_factory=dict)
    _lengths: dict[str, int] = field(init=False, default_factory=dict)
    _df: Counter[str] = field(init=False, default_factory=Counter)
    _avg: float = field(init=False, default=1.0)

    def __post_init__(self) -> None:
        for pid, para in self.index.paragraphs.items():
            words = tokens(para.text)
            self._docs[pid] = Counter(words)
            self._lengths[pid] = len(words)
            self._df.update(set(words))
        self._avg = (sum(self._lengths.values()) / len(self._lengths)) if self._lengths else 1.0

    # ---- keyword ranking
    def bm25(self, query: str, candidates: list[str]) -> list[tuple[str, float]]:
        terms = list(dict.fromkeys(tokens(query)))
        n = max(1, len(self._docs))
        scored: list[tuple[str, float]] = []
        for pid in candidates:
            counts = self._docs.get(pid)
            if not counts:
                continue
            score = 0.0
            for term in terms:
                tf = counts.get(term, 0)
                if not tf:
                    continue
                idf = math.log(1 + (n - self._df[term] + 0.5) / (self._df[term] + 0.5))
                norm = tf + BM25_K1 * (1 - BM25_B + BM25_B * self._lengths[pid] / self._avg)
                score += idf * tf * (BM25_K1 + 1) / norm
            if score > 0:
                scored.append((pid, round(score, SCORE_DIGITS)))
        return sorted(scored, key=lambda item: (-item[1], sort_key(item[0])))

    # ---- meaning ranking
    def dense(self, query_vector: list[float] | None, candidates: list[str]) -> list[tuple[str, float]]:
        if not query_vector or not self.vectors:
            return []
        scored = [
            (pid, round(_cosine(query_vector, self.vectors[pid]), SCORE_DIGITS)) for pid in candidates if pid in self.vectors
        ]
        return sorted(scored, key=lambda item: (-item[1], sort_key(item[0])))

    def search(
        self,
        query: str,
        k: int = 4,
        query_vector: list[float] | None = None,
        sections: set[str] | None = None,
        exclude_sections: set[str] | None = None,
        exclude: set[str] | None = None,
        min_keyword_score: float = 0.0,
    ) -> list[str]:
        """The `k` paragraph ids most relevant to `query`, in rank order."""
        candidates = [
            pid for pid, para in self.index.paragraphs.items()
            if (sections is None or para.section in sections)
            and (not exclude_sections or para.section not in exclude_sections)
            and (not exclude or pid not in exclude)
        ]
        keyword = [item for item in self.bm25(query, candidates) if item[1] >= min_keyword_score]
        meaning = self.dense(query_vector, candidates)
        if not meaning:
            return [pid for pid, _ in keyword[:k]]
        fused: dict[str, float] = {}
        for ranking in (keyword, meaning[: max(k * 4, 12)]):
            for rank, (pid, _) in enumerate(ranking, start=1):
                fused[pid] = fused.get(pid, 0.0) + 1.0 / (RRF_K + rank)
        ordered = sorted(fused.items(), key=lambda item: (-round(item[1], SCORE_DIGITS), sort_key(item[0])))
        return [pid for pid, _ in ordered[:k]]

    def best_similarity(self, query_vector: list[float] | None, candidates: list[str]) -> float:
        ranked = self.dense(query_vector, candidates)
        return ranked[0][1] if ranked else 0.0


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


async def embed_texts(provider: Any, texts: list[str]) -> list[list[float] | None]:
    """Vectors for `texts` (cached by text and model). None for each when the provider cannot embed."""
    if not texts or not hasattr(provider, "embed"):
        return [None] * len(texts)
    model = settings.EMBEDDING_MODEL
    store = get_store()
    clipped = [t[:MAX_EMBED_CHARS] for t in texts]
    hashes = [digest(model, t) for t in clipped]
    found = await store.get_vectors(model, list(dict.fromkeys(hashes)))
    missing = list(dict.fromkeys(h for h in hashes if h not in found))
    if missing:
        by_hash = {h: t for h, t in zip(hashes, clipped)}
        try:
            vectors = await provider.embed([by_hash[h] for h in missing], model)
        except Exception:  # noqa: BLE001 - keyword search still works
            logger.warning("Embedding call failed; searching by keywords only", exc_info=True)
            return [found.get(h) for h in hashes]
        fresh = {h: [round(float(x), 7) for x in vec] for h, vec in zip(missing, vectors)}
        await store.put_vectors(model, fresh)
        found.update(fresh)
    return [found.get(h) for h in hashes]


async def build_search(index: PaperIndex, provider: Any, paper_key: str | None = None) -> PaperSearch:
    """The paper's search, with embeddings when the provider has them. The chunks and vectors are saved under the
    paper's key (the file's hash) in the local store: the vectorised paper."""
    chunks = index.chunks()
    vectors_list = await embed_texts(provider, [str(c["text"]) for c in chunks])
    vectors = {str(c["id"]): v for c, v in zip(chunks, vectors_list) if v is not None}
    if paper_key:
        model = settings.EMBEDDING_MODEL if vectors else None
        rows = [
            {**c, "text_hash": digest(str(c["text"])), "model": model, "vector": vectors.get(str(c["id"]))}
            for c in chunks
        ]
        await get_store().put_paper(paper_key, rows)
    return PaperSearch(index, vectors, settings.EMBEDDING_MODEL if vectors else "")
