"""Contract retrieval eval: Recall@5 against clause labels set by the generator.

    uv run python -m evals.node.retrieval [--split dev|test|all]

Needs the index (`uv run python -m lunas.retrieval.index`) and the embedding model for the
queries; query embeddings are cached, so a rerun calls no model. Every combination of
ranking (keyword, vector, hybrid) and scope (the vendor's contract only, as the pipeline
searches, or all contracts) is reported, so a weak leg is visible, not hidden by fusion.
"""

import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter

from data.generate import RETRIEVAL_CASES_FILE, SPLIT_CHOICES, RetrievalCase
from lunas.config import Settings
from lunas.retrieval.index import cached_embedder, load_contracts
from lunas.retrieval.search import search
from lunas.retrieval.store import ClauseStore

RUNS_DIR = Path(__file__).resolve().parents[1] / "runs"
K = 5
MODES = ("keyword", "vector", "hybrid")
# The success criterion in SPEC §2.
TARGET_RECALL = 0.90


def score(relevant: list[str], ranked: list[str]) -> dict[str, float]:
    found = [key for key in relevant if key in ranked[:K]]
    first = next((rank for rank, key in enumerate(ranked, start=1) if key in relevant), None)
    return {
        "recall": len(found) / len(relevant),
        "all_found": float(len(found) == len(relevant)),
        "reciprocal_rank": 1 / first if first else 0.0,
    }


def mean(rows: list[dict[str, float]]) -> dict[str, float]:
    return {name: sum(row[name] for row in rows) / len(rows) for name in rows[0]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, help="dataset directory (default: data/out)")
    parser.add_argument("--split", choices=SPLIT_CHOICES, default="dev")
    args = parser.parse_args()

    settings = Settings()
    data_dir: Path = args.data or settings.data_dir
    raw = json.loads((data_dir / RETRIEVAL_CASES_FILE).read_text(encoding="utf-8"))
    cases = [
        case
        for case in TypeAdapter(list[RetrievalCase]).validate_python(raw)
        if args.split in ("all", case.split)
    ]
    language = {c.vendor_id: c.language for c in load_contracts(data_dir)}
    embedder = cached_embedder(settings)
    vectors = embedder.embed([case.query for case in cases])
    store = ClauseStore(settings.database_url)

    results: dict[str, list[dict]] = defaultdict(list)
    try:
        for case, vector in zip(cases, vectors, strict=True):
            for scope in ("vendor", "all"):
                for mode in MODES:
                    hits = search(
                        store,
                        embedder,
                        case.query,
                        vendor_id=case.vendor_id if scope == "vendor" else None,
                        limit=K,
                        mode=mode,
                        query_vector=vector,
                    )
                    ranked = [hit.clause.clause_id for hit in hits]
                    results[f"{mode}/{scope}"].append(
                        {
                            "id": case.id,
                            "kind": case.kind,
                            "cross_language": case.language != language[case.vendor_id],
                            "ranked": ranked,
                            "missed": [key for key in case.relevant if key not in ranked],
                            **score(case.relevant, ranked),
                        }
                    )
    finally:
        store.close()

    report: dict = {
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "split": args.split,
        "cases": len(cases),
        "embedding_model": embedder.model,
        "query_embeddings": embedder.counts,
        "k": K,
        "configurations": {},
    }
    print(f"Retrieval eval: {len(cases)} questions ({args.split}), {embedder.model}, k={K}")
    print(f"  {'ranking/scope':<18}{'recall@5':>9}{'all@5':>8}{'MRR':>7}")
    for name, rows in results.items():
        overall = mean(
            [{k: r[k] for k in ("recall", "all_found", "reciprocal_rank")} for r in rows]
        )
        by_kind = {
            kind: mean([{"recall": r["recall"]} for r in rows if r["kind"] == kind])["recall"]
            for kind in sorted({r["kind"] for r in rows})
        }
        by_language = {
            label: mean([{"recall": r["recall"]} for r in rows if r["cross_language"] == cross])[
                "recall"
            ]
            for label, cross in (("same_language", False), ("cross_language", True))
        }
        report["configurations"][name] = {
            **overall,
            "by_kind": by_kind,
            "by_language": by_language,
            "misses": [r for r in rows if r["missed"]],
        }
        print(
            f"  {name:<18}{overall['recall']:>9.3f}{overall['all_found']:>8.3f}"
            f"{overall['reciprocal_rank']:>7.3f}"
        )

    production = report["configurations"]["hybrid/vendor"]
    print("\nhybrid/vendor (how the pipeline searches) by question kind:")
    for kind, value in production["by_kind"].items():
        print(f"    {kind:<20}{value:.3f}")
    print(f"    same language       {production['by_language']['same_language']:.3f}")
    print(f"    cross language      {production['by_language']['cross_language']:.3f}")
    verdict = "meets" if production["recall"] >= TARGET_RECALL else "is below"
    print(f"\nRecall@5 {production['recall']:.3f} {verdict} the target {TARGET_RECALL}")

    RUNS_DIR.mkdir(exist_ok=True)
    path = RUNS_DIR / f"retrieval-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Run saved to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
