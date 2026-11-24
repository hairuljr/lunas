"""End-to-end eval on the scanned twins: from image-only PDF to decision.

    uv run python -m evals.node.scans [--split dev|test|all] [--ids inv-031,inv-158]
        [--vision] [--cache DIR] [--workers 2]
    uv run python -m evals.node.scans --record   # dev twins, with vision; fills the gate
    uv run python -m evals.node.scans --gate     # no model, no scans, no OCR; exit 1 on regression

The twins run on their own, never with their digital originals (they share invoice
numbers, so the duplicate check would flag them). Every twin goes through OCR; with
`--vision`, a doubtful OCR read is read again by LUNAS_VISION_MODEL (`nodes.read`).
Expected decisions and fields are the originals': a scan changes how an invoice is read,
not what it is. No contract index is needed when the retrieval cache holds the queries.

Replies are cached (default evals/cache/scans), so a rerun of the same twins costs nothing.

The gate replays evals/recordings/scans/: every read (OCR and vision transcripts, as
`ParsedDocument`s), the extraction replies and the retrievals of the dev twins. CI then
needs neither the scans (generated, not committed) nor the OCR engine (ONNX may read
differently on x86 than on the arm64 machine that recorded). It checks the code after the
read; a change in the scans or the OCR shows only when recorded again (`--record` reads
through the cache, so only uncached calls go live).
"""

import argparse
import json
import shutil
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from data.generate import load_ground_truth
from data.generate_scans import SCAN_MANIFEST_FILE
from evals.node.run import gate_failures, gate_metrics, model_inputs, sha256_text, stale_reasons
from evals.node.scoring import KEY_FIELDS, decision_metrics, extraction_metrics, score_fields
from lunas.config import Settings
from lunas.erp import ErpData
from lunas.ingest import vision
from lunas.ingest.recorded import DocumentRecorder
from lunas.llm import RecordingClient, build_client, load_prompt
from lunas.pipeline.run import process_invoices
from lunas.retrieval.retriever import build_retriever

ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "runs"
CACHE_DIR = ROOT / "cache" / "scans"
RECORDINGS_DIR = ROOT / "recordings" / "scans"
RECORDING_FILE = RECORDINGS_DIR / "recording.json"


def fingerprint(data_dir: Path) -> dict[str, str]:
    transcribe = load_prompt(vision.PROMPT_NAME, vision.PROMPT_VERSION)
    return {**model_inputs(data_dir), "transcribe_prompt": sha256_text(transcribe)}


def _keep(used: set[Path], target: Path) -> None:
    """Copy the files a recording run used into `target`, and drop the ones it did not."""
    target.mkdir(parents=True, exist_ok=True)
    names = {path.name for path in used}
    for stale in target.glob("*.json"):
        if stale.name not in names:
            stale.unlink()
    for path in used:
        if path.parent != target:
            shutil.copy2(path, target / path.name)


def summarize(rows: list[dict]) -> dict:
    return {
        "decisions": decision_metrics(
            [r["expected_decision"] for r in rows], [r["decision"] for r in rows]
        ),
        "key_fields": extraction_metrics([r["fields"] for r in rows])["key_field_accuracy"],
        "second_reads": sum(r["backend"] == "vision" for r in rows),
        "disagreements": sum("read_disagreement" in r["codes"] for r in rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    parser.add_argument("--ids", help="only these twins, comma-separated")
    parser.add_argument("--vision", action="store_true", help="second read for doubtful OCR")
    parser.add_argument("--cache", type=Path, default=CACHE_DIR)
    parser.add_argument("--workers", type=int, default=2)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--record", action="store_true", help="record the dev twins for the gate")
    mode.add_argument("--gate", action="store_true", help="replay the recordings, no model")
    args = parser.parse_args()
    if (args.record or args.gate) and (args.ids or args.split != "dev"):
        parser.error("--record and --gate run on all dev twins")

    settings = Settings()
    data_dir = settings.data_dir
    truth = {record.id: record for record in load_ground_truth(data_dir)}
    recording: dict = {}
    if args.gate:
        if not RECORDING_FILE.exists():
            print(f"No recordings in {RECORDINGS_DIR}. Create them with --record.")
            return 1
        recording = json.loads(RECORDING_FILE.read_text(encoding="utf-8"))
        if stale := stale_reasons(recording["fingerprint"], fingerprint(data_dir)):
            print("GATE FAILED: the scan recordings are stale because " + "; ".join(stale))
            print("Re-record with: uv run python -m evals.node.scans --record")
            return 1
        scans = recording["scans"]
    else:
        manifest = data_dir / SCAN_MANIFEST_FILE
        if not manifest.exists():
            print(f"No scans in {data_dir}. Run: uv run python -m data.generate_scans")
            return 2
        scans = json.loads(manifest.read_text(encoding="utf-8"))["scans"]
        wanted = set(args.ids.split(",")) if args.ids else None
        scans = [
            s
            for s in scans
            if (wanted is None or s["id"] in wanted) and args.split in ("all", s["split"])
        ]

    documents = None
    if args.gate:
        replies = RECORDINGS_DIR / "replies"
        client = RecordingClient(
            replies, None, model=recording["model"], host=recording["provider"]
        )
        # Never called: the vision reads are replayed as documents.
        vision_client = RecordingClient(
            replies, None, model=recording["vision_model"], host=recording["provider"]
        )
        retriever = build_retriever(settings, RECORDINGS_DIR / "retrieval", replay_only=True)
        documents = DocumentRecorder(RECORDINGS_DIR / "documents", replay_only=True)
    else:
        client = build_client(settings, args.cache)
        vision_client = (
            build_client(
                settings.model_copy(update={"llm_model": settings.vision_model}), args.cache
            )
            if args.vision or args.record
            else None
        )
        retriever = build_retriever(settings, args.cache / "retrieval")
        if args.record:
            if retriever is None:
                print("Recording needs the contract index (podman compose up -d).")
                return 2
            documents = DocumentRecorder(args.cache / "documents")
            shutil.rmtree(documents.directory, ignore_errors=True)
    results = process_invoices(
        [data_dir / s["file"] for s in scans],
        ErpData.load(data_dir),
        client,
        settings,
        args.workers,
        retriever,
        vision_client=vision_client,
        documents=documents,
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
        "vision": vision_client is not None,
        "model": client.model,
        "vision_model": vision_client.model if vision_client else None,
        "overall": summarize(rows),
        "by": {name: summarize(group) for name, group in sorted(groups.items())},
        "codes": dict(Counter(code for row in rows for code in row["codes"])),
        "rows": rows,
    }
    overall = report["overall"]
    print(
        f"\n{len(rows)} twins: decisions {overall['decisions']['correct']}/{len(rows)}, "
        f"false-approve {overall['decisions']['false_approve_rate']}, key fields "
        f"{overall['key_fields']:.3f}, second reads {overall['second_reads']}, "
        f"disagreements {overall['disagreements']}"
    )
    exit_code = 0
    current = gate_metrics(
        {"key_field_accuracy": report["overall"]["key_fields"]}, report["overall"]["decisions"]
    )
    if args.record:
        unanswered = [
            r["id"] for r in rows if {"llm_unavailable", "extraction_failed"} & set(r["codes"])
        ]
        if unanswered:
            print(f"\nNOT RECORDED: no reply for {', '.join(unanswered)}. Run --record again.")
            exit_code = 1
        else:
            # The vision replies are not kept: the gate replays their transcripts as
            # documents and never asks the vision model.
            _keep(client.used, RECORDINGS_DIR / "replies")
            _keep(retriever.used, RECORDINGS_DIR / "retrieval")
            _keep(documents.used, RECORDINGS_DIR / "documents")
            recorded = {
                "recorded_at": f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%S}",
                "provider": client.host,
                "model": client.model,
                "vision_model": vision_client.model,
                "fingerprint": fingerprint(data_dir),
                "scans": scans,
                "baseline": current,
            }
            RECORDING_FILE.write_text(json.dumps(recorded, indent=2) + "\n", encoding="utf-8")
            print(f"\nRecorded {len(rows)} twins and the gate baseline to {RECORDINGS_DIR}")
    if args.gate:
        report["gate"] = {"baseline": recording["baseline"], "current": current}
        if failures := gate_failures(recording["baseline"], current):
            print("\nGATE FAILED:\n  " + "\n  ".join(failures))
            exit_code = 1
        else:
            print(f"\nGATE PASSED against the recording of {recording['recorded_at']}: {current}")

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out = RUNS_DIR / f"scans-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"Written to {out}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
