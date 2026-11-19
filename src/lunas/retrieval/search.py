"""Hybrid search: keyword and vector rankings fused with reciprocal rank fusion."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from lunas.models import ContractClause
from lunas.retrieval.embed import Embedder
from lunas.retrieval.store import ClauseStore

Mode = Literal["keyword", "vector", "hybrid"]
# How many candidates each ranking contributes before fusion.
CANDIDATES = 20
# The usual RRF constant: damps the difference between the first few ranks.
RRF_K = 60
# The keyword ranking counts half as much as the vector ranking. Chosen on the dev split
# (evals/node/retrieval.py, weights 1.0, 0.7, 0.5, 0.3, 0.0): equal weights let keyword
# matches on common words push out the clauses that answer cross-language questions, and
# no keyword weight at all loses questions that cite a clause by number.
KEYWORD_WEIGHT = 0.5


@dataclass(frozen=True)
class Hit:
    clause: ContractClause
    score: float


def rrf(
    rankings: Sequence[Sequence[str]], weights: Sequence[float] | None = None, k: int = RRF_K
) -> list[tuple[str, float]]:
    """Weighted reciprocal rank fusion: each list adds weight / (k + rank) to its ids."""
    scores: dict[str, float] = {}
    for ranking, weight in zip(rankings, weights or [1.0] * len(rankings), strict=True):
        for rank, key in enumerate(ranking, start=1):
            scores[key] = scores.get(key, 0.0) + weight / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def search(
    store: ClauseStore,
    embedder: Embedder,
    query: str,
    vendor_id: str | None = None,
    limit: int = 5,
    mode: Mode = "hybrid",
    query_vector: Sequence[float] | None = None,
) -> list[Hit]:
    """`vendor_id` restricts the search to that vendor's contract. The pipeline always knows
    the vendor (from the PO) and must never cite another vendor's contract."""
    keyword = store.keyword_search(query, vendor_id, CANDIDATES) if mode != "vector" else []
    vector: list[ContractClause] = []
    if mode != "keyword":
        query_vector = query_vector or embedder.embed([query])[0]
        vector = store.vector_search(query_vector, vendor_id, CANDIDATES)

    clauses = {clause.clause_id: clause for clause in keyword + vector}
    rankings = [[c.clause_id for c in keyword], [c.clause_id for c in vector]]
    fused = rrf(rankings, [KEYWORD_WEIGHT if mode == "hybrid" else 1.0, 1.0])
    return [Hit(clauses[key], score) for key, score in fused[:limit]]
