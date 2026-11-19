"""Build the contract index: `uv run python -m lunas.retrieval.index [--data data/out]`."""

import argparse
import json
from pathlib import Path

from pydantic import TypeAdapter

from lunas.config import Settings
from lunas.models import Contract
from lunas.retrieval.embed import CachedEmbedder, Embedder, OpenAICompatEmbedder
from lunas.retrieval.store import ClauseStore, chunk_text

CONTRACTS_FILE = "contracts.json"
EMBEDDING_CACHE = Path(__file__).resolve().parents[3] / "evals" / "cache" / "embeddings"


def load_contracts(data_dir: Path) -> list[Contract]:
    raw = json.loads((data_dir / CONTRACTS_FILE).read_text(encoding="utf-8"))
    return TypeAdapter(list[Contract]).validate_python(raw)


def build_index(contracts: list[Contract], store: ClauseStore, embedder: Embedder) -> int:
    """Rebuild from scratch. Returns the number of clauses indexed."""
    texts = [[chunk_text(c, clause) for clause in c.clauses] for c in contracts]
    vectors = embedder.embed([text for batch in texts for text in batch])
    store.reset(len(vectors[0]))
    offset = 0
    for contract, batch in zip(contracts, texts, strict=True):
        store.add(contract, vectors[offset : offset + len(batch)])
        offset += len(batch)
    return offset


def cached_embedder(settings: Settings) -> CachedEmbedder:
    return CachedEmbedder(OpenAICompatEmbedder.from_settings(settings), EMBEDDING_CACHE)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, help="dataset directory (default: data/out)")
    args = parser.parse_args()
    settings = Settings()
    embedder = cached_embedder(settings)
    store = ClauseStore(settings.database_url)
    try:
        count = build_index(load_contracts(args.data or settings.data_dir), store, embedder)
    finally:
        store.close()
    print(f"Indexed {count} clauses with {embedder.model}: {embedder.counts}")


if __name__ == "__main__":
    main()
