"""End-to-end eval on the scanned twins: from image-only PDF to decision.

    uv run python -m evals.node.scans [--split dev|test|all] [--ids inv-031,inv-158]
        [--vision] [--cache DIR] [--workers 2]

The twins run on their own, never with their digital originals (they share invoice
numbers, so the duplicate check would flag them). Every twin goes through OCR; with
`--vision`, a doubtful OCR read is read again by LUNAS_VISION_MODEL (`nodes.read`).
Expected decisions and fields are the originals': a scan changes how an invoice is read,
not what it is. No contract index is needed when the retrieval cache holds the queries.

Replies are cached (default evals/cache/scans), so a rerun of the same twins costs nothing.
"""

import argparse
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from data.generate import load_ground_truth
from data.generate_scans import SCAN_MANIFEST_FILE
from evals.node.scoring import KEY_FIELDS, decision_metrics, extraction_metrics, score_fields
from lunas.config import Settings
from lunas.erp import ErpData
from lunas.llm import build_client
from lunas.pipeline.run import process_invoices
from lunas.retrieval.retriever import build_retriever

ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "runs"
CACHE_DIR = ROOT / "cache" / "scans"


def summarize(rows: list[dict]) -> dict:
    return {
        "decisions": decision_metrics(
            [r["expected_decision"] for r in rows], [r["decision"] for r in rows]
        ),
        "key_fields": extraction_metrics([r["fields"] for r in rows])["key_field_accuracy"],
        "second_reads": sum(r["backend"] == "vision" for r in rows),
        "disagreements": sum("read_disagreement" in r["codes"] for r in rows),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    parser.add_argument("--ids", help="only these twins, comma-separated")
    parser.add_argument("--vision", action="store_true", help="second read for doubtful OCR")
    parser.add_argument("--cache", type=Path, default=CACHE_DIR)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()

    settings = Settings()
    data_dir = settings.data_dir
    truth = {record.id: record for record in load_ground_truth(data_dir)}
    scans = json.loads((data_dir / SCAN_MANIFEST_FILE).read_text(encoding="utf-8"))["scans"]
    wanted = set(args.ids.split(",")) if args.ids else None
    scans = [
        s
        for s in scans
        if (wanted is None or s["id"] in wanted) and args.split in ("all", s["split"])
    ]

    client = build_client(settings, args.cache)
    vision_client = (
        build_client(settings.model_copy(update={"llm_model": settings.vision_model}), args.cache)
        if args.vision
        else None
    )
    results = process_invoices(
        [data_dir / s["file"] for s in scans],
        ErpData.load(data_dir),
        client,
        settings,
        args.workers,
        build_retriever(settings, args.cache / "retrieval"),
        vision_client=vision_client,
    )

    rows = []
    for scan, result in zip(scans, results, strict=True):
        record = truth[scan["id"]]
        fields = score_fields(record.invoice, result.invoice)
        rows.append(
            {
                **scan,
                "expected_decision": record.expected_decision,
                "decision": result.decision,
                "backend": result.backend,
                "codes": sorted({issue.code for issue in result.issues}),
                "fields": fields,
                "wrong_key_fields": [f for f in KEY_FIELDS if not fields[f]],
            }
        )
        print(
            f"{scan['id']}  {scan['tier']:<8}{'stamp' if scan['stamp'] else '':<6}"
            f"{record.expected_decision.value:>8} -> {result.decision.value:<9}"
            f"{result.backend:<7}{','.join(rows[-1]['codes'])}"
            + (
                f"  wrong: {','.join(rows[-1]['wrong_key_fields'])}"
                if rows[-1]["wrong_key_fields"]
                else ""
            )
        )

    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[f"tier={row['tier']}"].append(row)
        groups[f"stamp={row['stamp']}"].append(row)
    report = {
        "split": args.split,
        "vision": args.vision,
        "model": client.model,
        "vision_model": vision_client.model if vision_client else None,
        "overall": summarize(rows),
        "by": {name: summarize(group) for name, group in sorted(groups.items())},
        "codes": dict(Counter(code for row in rows for code in row["codes"])),
        "rows": rows,
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out = RUNS_DIR / f"scans-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    overall = report["overall"]
    print(
        f"\n{len(rows)} twins: decisions {overall['decisions']['correct']}/{len(rows)}, "
        f"false-approve {overall['decisions']['false_approve_rate']}, key fields "
        f"{overall['key_fields']:.3f}, second reads {overall['second_reads']}, "
        f"disagreements {overall['disagreements']}"
    )
    print(f"Written to {out}")


if __name__ == "__main__":
    main()
