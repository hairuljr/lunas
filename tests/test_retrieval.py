import hashlib
import math
import re
import uuid
from pathlib import Path

import psycopg
import pytest

from data.generate import build_dataset
from lunas.config import Settings
from lunas.retrieval.embed import CachedEmbedder
from lunas.retrieval.index import build_index
from lunas.retrieval.search import rrf, search
from lunas.retrieval.store import ClauseStore, chunk_text


def test_rrf_adds_the_reciprocal_ranks_of_every_list():
    fused = dict(rrf([["a", "b"], ["b", "c"]], k=0))

    assert fused == {"a": 1.0, "b": 0.5 + 1.0, "c": 0.5}


def test_rrf_weights_scale_each_list():
    fused = rrf([["a"], ["b"]], weights=[0.5, 1.0], k=0)

    assert fused == [("b", 1.0), ("a", 0.5)]


def test_rrf_breaks_ties_by_id_so_results_are_stable():
    assert [key for key, _ in rrf([["b"], ["a"]])] == ["a", "b"]


def test_chunk_header_names_the_vendor_the_clause_and_its_article():
    contract = build_dataset(1, 3).contracts[0]
    clause = contract.clauses[4]

    text = chunk_text(contract, clause)

    assert text.startswith(f"{contract.vendor_name}. ")
    assert f" {clause.number} {clause.article_title}. " in text
    assert text.endswith(clause.text)


class HashEmbedder:
    """Deterministic bag-of-words vectors: shared words make texts similar."""

    model = "hash"

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        vectors = []
        for text in texts:
            vector = [0.0] * 32
            for word in re.findall(r"\w+", text.lower()):
                vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % 32] += 1.0
            norm = math.sqrt(sum(v * v for v in vector)) or 1.0
            vectors.append([v / norm for v in vector])
        return vectors


def test_cached_embedder_pays_for_each_text_once(tmp_path: Path):
    inner = HashEmbedder()
    first = CachedEmbedder(inner, tmp_path).embed(["a b", "c"])

    cache = CachedEmbedder(inner, tmp_path)
    again = cache.embed(["c", "a b", "new"])

    assert again[:2] == [first[1], first[0]]
    assert cache.counts == {"cached": 2, "embedded": 1}
    assert inner.calls == 2


def database_url() -> str:
    url = Settings().database_url
    try:
        psycopg.connect(url, connect_timeout=2).close()
    except psycopg.OperationalError:
        pytest.skip("Postgres with pgvector is not running (podman compose up -d)")
    return url


@pytest.fixture
def store():
    store = ClauseStore(database_url(), table=f"test_clauses_{uuid.uuid4().hex[:8]}")
    yield store
    with store._connection.cursor() as cursor:
        cursor.execute(f"DROP TABLE IF EXISTS {store.table}")
    store.close()


def test_index_and_search_stay_inside_the_vendor_when_asked(store: ClauseStore):
    contracts = build_dataset(1, 3).contracts[:3]
    embedder = HashEmbedder()
    assert build_index(contracts, store, embedder) == sum(len(c.clauses) for c in contracts)

    vendor = contracts[1]
    payment = next(c for c in vendor.clauses if vendor.vendor_name in c.text)
    hits = search(store, embedder, f"rekening {vendor.vendor_name}", vendor_id=vendor.vendor_id)

    assert 0 < len(hits) <= 5
    assert {hit.clause.vendor_id for hit in hits} == {vendor.vendor_id}
    assert payment.clause_id in [hit.clause.clause_id for hit in hits]


def test_keyword_search_finds_a_clause_by_its_number(store: ClauseStore):
    contracts = build_dataset(1, 3).contracts[:2]
    build_index(contracts, store, HashEmbedder())
    contract = contracts[0]
    word = "Pasal" if contract.language == "id" else "Clause"

    found = store.keyword_search(f"{word} 5.1", vendor_id=contract.vendor_id, limit=1)

    assert [clause.clause_id for clause in found] == [f"{contract.contract_id}#5.1"]


def test_price_question_returns_whole_parent_articles_of_the_vendors_contract(store: ClauseStore):
    from lunas.retrieval.retriever import MAX_ARTICLES, StoreRetriever

    contracts = build_dataset(1, 3).contracts[:2]
    build_index(contracts, store, HashEmbedder())
    contract = contracts[0]
    item = "Kertas" if contract.language == "id" else "Paper"

    clauses = StoreRetriever(store, HashEmbedder()).price_clauses(
        contract.vendor_id, contract.vendor_name, item
    )

    articles = {(c.contract_id, c.article) for c in clauses}
    assert 0 < len(articles) <= MAX_ARTICLES
    assert {c.vendor_id for c in clauses} == {contract.vendor_id}
    for _, article in articles:
        expected = [c for c in contract.clauses if c.article == article]
        assert [c for c in clauses if c.article == article] == expected
    assert store.contract_language(contract.vendor_id) == contract.language
    assert store.contract_language("V-999") is None
