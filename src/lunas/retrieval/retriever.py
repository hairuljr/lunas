"""What the contract check asks of the index: the articles that could set an item's price.

`RecordingRetriever` stores answers next to the recorded LLM replies, so the PR gate can
replay the contract check with no database and no embedding model.
"""

import hashlib
import json
import sys
import threading
from pathlib import Path
from typing import Protocol

import psycopg

from lunas.config import Settings
from lunas.models import ContractClause
from lunas.retrieval.embed import Embedder
from lunas.retrieval.questions import PRICE_QUESTION
from lunas.retrieval.search import search
from lunas.retrieval.store import ClauseStore
from lunas.telemetry import set_attributes, span

# Parent articles passed to the model, at most (SPEC §6).
MAX_ARTICLES = 5


class ContractRetriever(Protocol):
    def price_clauses(self, vendor_id: str, vendor_name: str, item: str) -> list[ContractClause]:
        """The articles of the vendor's own contract that best answer a question about
        the item's unit price, every clause of each article included."""
        ...


class StoreRetriever:
    def __init__(self, store: ClauseStore, embedder: Embedder) -> None:
        self._store = store
        self._embedder = embedder

    def price_clauses(self, vendor_id: str, vendor_name: str, item: str) -> list[ContractClause]:
        with span("retrieve", {"app.vendor_id": vendor_id}) as current:
            language = self._store.contract_language(vendor_id)
            if language is None:
                return []
            query = PRICE_QUESTION[language].format(vendor=vendor_name, item=item)
            hits = search(self._store, self._embedder, query, vendor_id=vendor_id)
            articles = list(dict.fromkeys((h.clause.contract_id, h.clause.article) for h in hits))
            clauses = self._store.article_clauses(articles[:MAX_ARTICLES])
            set_attributes(
                current,
                {
                    "app.retrieval.hits": [hit.clause.clause_id for hit in hits],
                    "app.retrieval.articles": len(articles[:MAX_ARTICLES]),
                },
            )
            return clauses


class RecordingMissingError(RuntimeError):
    pass


class RecordingRetriever:
    """Wraps a retriever; with `inner=None` it only replays."""

    def __init__(self, directory: Path, inner: ContractRetriever | None) -> None:
        self._directory = directory
        self._inner = inner
        self.used: set[Path] = set()
        self._lock = threading.Lock()

    def _path(self, *key: str) -> Path:
        return self._directory / f"{hashlib.sha256(json.dumps(key).encode()).hexdigest()}.json"

    def price_clauses(self, vendor_id: str, vendor_name: str, item: str) -> list[ContractClause]:
        path = self._path("price", vendor_id, vendor_name, item)
        with self._lock:
            self.used.add(path)
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            return [ContractClause.model_validate(item) for item in raw]
        if self._inner is None:
            raise RecordingMissingError(f"No recorded retrieval for {vendor_id} / {item!r}")
        clauses = self._inner.price_clauses(vendor_id, vendor_name, item)
        self._directory.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([c.model_dump() for c in clauses], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        return clauses


def build_retriever(
    settings: Settings, recordings: Path | None = None, replay_only: bool = False
) -> ContractRetriever | None:
    """The contract index, optionally recorded. None, with a warning, when the database is
    not reachable: the pipeline then runs without a contract check."""
    if replay_only:
        return RecordingRetriever(recordings, None) if recordings else None
    from lunas.retrieval.index import cached_embedder

    try:
        store = ClauseStore(settings.database_url)
    except psycopg.OperationalError as error:
        print(f"Contract check off: no database ({error.__class__.__name__}).", file=sys.stderr)
        return None
    live = StoreRetriever(store, cached_embedder(settings))
    return RecordingRetriever(recordings, live) if recordings else live
