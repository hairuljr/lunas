"""Node evals for the week-1 slice, on the dataset written by data/generate.py.

    uv run python -m evals.node.run [--data data/out] [--skip-llm] [--workers 4]
    uv run python -m evals.node.run --record   # live; refresh evals/recordings/
    uv run python -m evals.node.run --gate     # no LLM; replay recordings, exit 1 on regression

1. Match + decide (steps 5 and 9) on ground-truth fields. Deterministic, no LLM.
2. Extraction (step 3): per-field exact match of the LLM output against ground truth.
3. Decision accuracy end to end, from PDF through extraction to the decision.

Parts 2 and 3 call the configured LLM; --skip-llm runs part 1 only. Every run is saved to
evals/runs/ with the model ID and prompt version, so a number can be traced to its run.

The PR gate (SPEC §9) replays the committed recordings in evals/recordings/. It checks the
code around the model (parsing, validation, matching, decision) against replies a real
model once gave. It does not re-measure the model: a new prompt, schema or dataset has no
recordings, fails the gate, and needs --record.
"""

import argparse
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from data.generate import MANIFEST_FILE, GroundTruth, load_ground_truth
from evals.node.scoring import (
    FIELDS,
    KEY_FIELDS,
    decision_metrics,
    extraction_metrics,
    score_fields,
)
from lunas.config import Settings
from lunas.erp import ErpData
from lunas.ingest import choose_backend, get_reader
from lunas.llm import (
    ChatClient,
    LLMNotConfiguredError,
    RecordingClient,
    RecordingMissingError,
    build_client,
    load_prompt,
    strict_schema,
)
from lunas.models import Invoice
from lunas.pipeline.nodes.decide import decide
from lunas.pipeline.nodes.extract import PROMPT_NAME, PROMPT_VERSION
from lunas.pipeline.nodes.match import match_invoice
from lunas.pipeline.nodes.validate import validate_invoice
from lunas.pipeline.run import (
    EXTRACTION_FAILED,
    LLM_UNAVAILABLE,
    PipelineResult,
    process_invoices,
)

RUNS_DIR = Path(__file__).resolve().parents[1] / "runs"
CACHE_DIR = Path(__file__).resolve().parents[1] / "cache"
RECORDINGS_DIR = Path(__file__).resolve().parents[1] / "recordings"
RECORDING_FILE = "recording.json"
# SPEC §9: the PR gate fails when decision accuracy drops by more than 2 points. The same
# margin is applied to key-field extraction, and false approvals may not rise at all.
GATE_MAX_DROP = 0.02


def fingerprint(paths: list[Path], settings: Settings) -> dict[str, str]:
    """What the recorded replies depend on. If any of these changed, they are stale."""

    def sha(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    texts = []
    for path in paths:
        backend = choose_backend(path, settings.ingest_mode, settings.min_chars_per_page)
        texts.append(f"{path.name}\n{get_reader(backend).read(path).text}")
    return {
        "prompt": sha(load_prompt(PROMPT_NAME, PROMPT_VERSION)),
        "schema": sha(json.dumps(strict_schema(Invoice).schema, sort_keys=True)),
        "documents": sha("\n\n".join(texts)),
    }


_STALE_REASON = {
    "prompt": f"prompts/{PROMPT_NAME}.{PROMPT_VERSION}.md changed",
    "schema": "the Invoice schema changed",
    "documents": "the text read from the invoice PDFs changed (generator, PDF reader, or "
    "a platform difference in rendering)",
}


def stale_reasons(recorded: dict[str, str], current: dict[str, str]) -> list[str]:
    return [reason for key, reason in _STALE_REASON.items() if recorded.get(key) != current[key]]


def gate_metrics(extraction: dict, decisions: dict) -> dict[str, float]:
    return {
        "key_field_accuracy": extraction["key_field_accuracy"],
        "decision_accuracy": decisions["accuracy"],
        "false_approve_rate": decisions["false_approve_rate"] or 0.0,
    }


def gate_failures(baseline: dict[str, float], current: dict[str, float]) -> list[str]:
    failures = []
    for name in ("decision_accuracy", "key_field_accuracy"):
        if current[name] < baseline[name] - GATE_MAX_DROP:
            failures.append(
                f"{name} {current[name]:.1%} is more than {GATE_MAX_DROP:.0%} below the "
                f"recorded {baseline[name]:.1%}"
            )
    if current["false_approve_rate"] > baseline["false_approve_rate"]:
        failures.append(
            f"false_approve_rate rose from {baseline['false_approve_rate']:.1%} to "
            f"{current['false_approve_rate']:.1%}"
        )
    return failures


def eval_match_decide(records: list[GroundTruth], erp: ErpData, settings: Settings) -> dict:
    """Steps 4, 5 and 9 given perfect extraction: isolates the deterministic nodes."""
    decisions, codes_correct, wrong = [], 0, []
    for record in records:
        issues = validate_invoice(record.invoice, settings.amount_tolerance)
        issues += match_invoice(record.invoice, erp, settings.amount_tolerance)
        decision = decide(record.invoice, issues, settings.approval_threshold).decision
        codes = sorted({issue.code for issue in issues})
        decisions.append(decision)
        codes_correct += codes == record.expected_issue_codes
        if decision != record.expected_decision or codes != record.expected_issue_codes:
            wrong.append({"id": record.id, "decision": decision.value, "issue_codes": codes})
    return {
        **decision_metrics([record.expected_decision for record in records], decisions),
        "issue_code_accuracy": codes_correct / len(records),
        "wrong": wrong,
    }


def eval_pipeline(
    records: list[GroundTruth], results: list[PipelineResult]
) -> tuple[dict, dict, list[dict]]:
    """Extraction metrics, end-to-end decision metrics and the per-invoice misses."""
    scores = [
        score_fields(record.invoice, result.invoice)
        for record, result in zip(records, results, strict=True)
    ]
    extraction = extraction_metrics(scores)
    for name, code in (("extraction_failures", EXTRACTION_FAILED), ("unanswered", LLM_UNAVAILABLE)):
        extraction[name] = sum(
            any(issue.code == code for issue in result.issues) for result in results
        )
    groups: dict[str, list[dict[str, bool]]] = {}
    for record, score in zip(records, scores, strict=True):
        groups.setdefault(f"{record.layout}/{record.language}", []).append(score)
    extraction["key_field_accuracy_by_layout"] = {
        group: extraction_metrics(group_scores)["key_field_accuracy"]
        for group, group_scores in sorted(groups.items())
    }

    decisions = decision_metrics(
        [record.expected_decision for record in records],
        [result.decision for result in results],
    )

    misses = []
    for record, result, score in zip(records, results, scores, strict=True):
        wrong_fields = {
            name: {
                "expected": _show(getattr(record.invoice, name)),
                "actual": _show(getattr(result.invoice, name, None)),
            }
            for name in FIELDS
            if not score[name]
        }
        if wrong_fields or result.decision != record.expected_decision:
            misses.append(
                {
                    "id": record.id,
                    "layout": f"{record.layout}/{record.language}",
                    "expected_decision": record.expected_decision.value,
                    "decision": result.decision.value,
                    "issue_codes": sorted({issue.code for issue in result.issues}),
                    "wrong_fields": wrong_fields,
                }
            )
    return extraction, decisions, misses


def _show(value: object) -> object:
    if isinstance(value, list):
        return [item.model_dump(mode="json") for item in value]
    return None if value is None else str(value)


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:6.1%}"


def _print_decisions(title: str, metrics: dict) -> None:
    print(f"\n{title}")
    print(f"  decision accuracy     {_percent(metrics['accuracy'])}  (n={metrics['documents']})")
    print(f"  false-approve rate    {_percent(metrics['false_approve_rate'])}")
    print(f"  expected->actual      {metrics['confusion']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, help="dataset directory (default: data/out)")
    parser.add_argument("--skip-llm", action="store_true", help="deterministic evals only")
    parser.add_argument(
        "--workers", type=int, help="parallel LLM calls (default: LUNAS_LLM_WORKERS)"
    )
    parser.add_argument(
        "--cache",
        type=Path,
        nargs="?",
        const=CACHE_DIR,
        help="record LLM replies here and replay them on the next run (default dir: "
        "evals/cache). Resumes an interrupted run; replayed replies are not re-measured",
    )
    parser.add_argument(
        "--replay-only", action="store_true", help="with --cache: never call the LLM"
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="live run that refreshes the committed recordings in evals/recordings/ (calls "
        "the LLM only for requests not recorded yet) and sets the gate's baseline",
    )
    parser.add_argument(
        "--gate",
        action="store_true",
        help="PR gate: replay evals/recordings/ with no LLM; exit 1 on a regression or on "
        "stale recordings",
    )
    args = parser.parse_args()
    if args.replay_only and args.cache is None:
        parser.error("--replay-only needs --cache")
    if (args.record or args.gate) and (args.cache or args.skip_llm or args.record == args.gate):
        parser.error("--record and --gate stand alone")

    settings = Settings()
    data_dir: Path = args.data or settings.data_dir
    if not (data_dir / MANIFEST_FILE).exists():
        print(f"No dataset in {data_dir}. Run: uv run python data/generate.py", file=sys.stderr)
        return 2
    manifest = json.loads((data_dir / MANIFEST_FILE).read_text(encoding="utf-8"))
    records = load_ground_truth(data_dir)
    erp = ErpData.load(data_dir)
    print(f"Dataset: {data_dir}  {manifest}")
    paths = [data_dir / record.file for record in records]

    recording_path = RECORDINGS_DIR / RECORDING_FILE
    recording: dict = {}
    current_fingerprint = fingerprint(paths, settings) if args.record or args.gate else {}
    client: ChatClient | None = None
    try:
        if args.gate:
            if not recording_path.exists():
                print(f"No recordings in {RECORDINGS_DIR}. Create them with --record.")
                return 1
            recording = json.loads(recording_path.read_text(encoding="utf-8"))
            if stale := stale_reasons(recording["fingerprint"], current_fingerprint):
                print("\nGATE FAILED: the recordings are stale because " + "; ".join(stale))
                print("Re-record with: uv run python -m evals.node.run --record (calls the LLM)")
                return 1
            client = RecordingClient(
                RECORDINGS_DIR, None, model=recording["model"], host=recording["provider"]
            )
        elif args.record:
            client = build_client(settings, RECORDINGS_DIR)
        elif not args.skip_llm:
            client = build_client(settings, args.cache, args.replay_only)
    except LLMNotConfiguredError as error:
        print(f"{error}\nOr pass --skip-llm for the deterministic evals.", file=sys.stderr)
        return 2

    report: dict = {
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dataset": manifest,
        "match_decide_on_ground_truth": eval_match_decide(records, erp, settings),
    }
    _print_decisions(
        "Match + decide on ground-truth fields (no LLM)", report["match_decide_on_ground_truth"]
    )
    accuracy = report["match_decide_on_ground_truth"]["issue_code_accuracy"]
    print(f"  issue-code accuracy   {_percent(accuracy)}")

    if client is None:
        print("\nExtraction and end-to-end decision: skipped (--skip-llm), not measured.")
    else:
        started = time.monotonic()
        try:
            results = process_invoices(
                paths, erp, client, settings, args.workers or settings.llm_workers
            )
        except RecordingMissingError as error:
            print(f"\nGATE FAILED: {error}. Re-record with --record (calls the LLM).")
            return 1
        elapsed = time.monotonic() - started
        extraction, decisions, misses = eval_pipeline(records, results)
        report |= {
            "model": client.model,
            # Which endpoint served the model: the same model ID can differ by provider.
            "provider": getattr(client, "host", None),
            "workers": args.workers or settings.llm_workers,
            "elapsed_seconds": round(elapsed, 1),
            "usage": getattr(client, "usage", None),
            # With --cache: how many replies were fresh and how many came from disk.
            "replies": getattr(client, "counts", None),
            "prompt": f"{PROMPT_NAME}.{PROMPT_VERSION}",
            "structured_output": settings.llm_structured_output,
            "system_role": settings.llm_system_role,
            "temperature": settings.llm_temperature,
            "extraction": extraction,
            "decision_end_to_end": decisions,
            "misses": misses,
        }

        print(
            f"\nExtraction: model {client.model}, prompt {PROMPT_NAME}.{PROMPT_VERSION}, "
            f"structured output {settings.llm_structured_output}"
        )
        print(f"  provider              {report['provider']}")
        print(f"  elapsed / usage       {elapsed:.1f}s  {report['usage']}")
        print(f"  key-field accuracy    {_percent(extraction['key_field_accuracy'])}")
        print(f"  all key fields right  {_percent(extraction['documents_with_all_key_fields'])}")
        print(f"  whole invoice right   {_percent(extraction['documents_fully_correct'])}")
        print(f"  extraction failures   {extraction['extraction_failures']}")
        if report["replies"] and report["replies"]["replayed"]:
            print(f"  replies               {report['replies']} (replayed = not re-measured)")
        if extraction["unanswered"]:
            print(
                f"  UNANSWERED            {extraction['unanswered']} invoices got no reply from "
                "the endpoint and are scored as wrong. Rerun with --cache to fill them in; "
                "until then these numbers do not measure the model."
            )
        for name in FIELDS:
            marker = "key" if name in KEY_FIELDS else ""
            print(f"    {name:<18}{_percent(extraction['field_accuracy'][name])}  {marker}")
        print("  key-field accuracy by layout/language")
        for group, value in extraction["key_field_accuracy_by_layout"].items():
            print(f"    {group:<18}{_percent(value)}")
        _print_decisions("Decision end to end (PDF -> extraction -> decision)", decisions)
        for miss in misses:
            print(f"  miss {miss['id']} ({miss['layout']}): {miss['decision']}", end="")
            print(f", expected {miss['expected_decision']}; wrong: {list(miss['wrong_fields'])}")

    exit_code = 0
    if args.record:
        assert isinstance(client, RecordingClient)
        if report["extraction"]["unanswered"]:
            print("\nNOT RECORDED: some invoices got no reply. Run --record again to fill them.")
            exit_code = 1
        else:
            for stale_file in set(RECORDINGS_DIR.glob("*.json")) - client.used - {recording_path}:
                stale_file.unlink()
            recorded = {
                "recorded_at": report["started_at"],
                "provider": client.host,
                "model": client.model,
                "prompt": report["prompt"],
                "structured_output": settings.llm_structured_output,
                "system_role": settings.llm_system_role,
                "dataset": manifest,
                "replies": len(client.used),
                "fingerprint": current_fingerprint,
                "baseline": gate_metrics(report["extraction"], report["decision_end_to_end"]),
            }
            recording_path.write_text(json.dumps(recorded, indent=2) + "\n", encoding="utf-8")
            print(
                f"\nRecorded {len(client.used)} replies and the gate baseline to {RECORDINGS_DIR}"
            )
    if args.gate:
        current = gate_metrics(report["extraction"], report["decision_end_to_end"])
        report["gate"] = {"baseline": recording["baseline"], "current": current}
        if failures := gate_failures(recording["baseline"], current):
            print("\nGATE FAILED:\n  " + "\n  ".join(failures))
            exit_code = 1
        else:
            print(f"\nGATE PASSED against the recording of {recording['recorded_at']}: {current}")

    RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = RUNS_DIR / f"node-{stamp}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nRun saved to {path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
