"""Node evals for the week-1 slice, on the dataset written by data/generate.py.

    uv run python -m evals.node.run [--split dev|test|all] [--skip-llm] [--workers 4]
    uv run python -m evals.node.run --record   # live; refresh evals/recordings/
    uv run python -m evals.node.run --gate     # no LLM; replay recordings, exit 1 on regression

1. Match + decide (steps 5 and 9) on ground-truth fields. Deterministic, no LLM.
2. Extraction (step 3): per-field exact match of the LLM output against ground truth.
3. Decision accuracy end to end, from PDF through extraction to the decision.

Runs on the dev split unless told otherwise: iterate on dev, and run test only for the
numbers that get reported. Parts 2 and 3 call the configured LLM; --skip-llm runs part 1
only. Every run is saved to evals/runs/ with the model ID and prompt version, so a number
can be traced to its run.

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
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from data.generate import (
    CONTRACTS_FILE,
    MANIFEST_FILE,
    SPLIT_CHOICES,
    GroundTruth,
    load_ground_truth,
)
from evals.node.scoring import (
    FIELDS,
    KEY_FIELDS,
    by_label,
    decision_metrics,
    extraction_metrics,
    percent,
    percentile,
    print_decisions,
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
from lunas.models import Invoice, Issue
from lunas.pipeline.nodes.contract import PROMPT_NAME as CONTRACT_PROMPT
from lunas.pipeline.nodes.contract import PROMPT_VERSION as CONTRACT_VERSION
from lunas.pipeline.nodes.decide import decide
from lunas.pipeline.nodes.extract import PROMPT_NAME, PROMPT_VERSION
from lunas.pipeline.nodes.investigate import PROMPT_NAME as INVESTIGATE_PROMPT
from lunas.pipeline.nodes.investigate import PROMPT_VERSION as INVESTIGATE_VERSION
from lunas.pipeline.nodes.route import Route
from lunas.pipeline.run import (
    EXTRACTION_FAILED,
    LLM_UNAVAILABLE,
    PipelineResult,
    check_invoice,
    process_invoices,
)
from lunas.retrieval.retriever import ContractRetriever, build_retriever
from lunas.retrieval.retriever import RecordingMissingError as RetrievalMissingError
from lunas.telemetry import configure_tracing, shutdown_tracing

RUNS_DIR = Path(__file__).resolve().parents[1] / "runs"
CACHE_DIR = Path(__file__).resolve().parents[1] / "cache"
RECORDINGS_DIR = Path(__file__).resolve().parents[1] / "recordings"
RECORDING_FILE = "recording.json"
MAX_MISSES_SHOWN = 20
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
        "contract_prompt": sha(load_prompt(CONTRACT_PROMPT, CONTRACT_VERSION)),
        "investigate_prompt": sha(load_prompt(INVESTIGATE_PROMPT, INVESTIGATE_VERSION)),
        "contracts": sha((paths[0].parents[1] / CONTRACTS_FILE).read_text(encoding="utf-8")),
    }


_STALE_REASON = {
    "contract_prompt": f"prompts/{CONTRACT_PROMPT}.{CONTRACT_VERSION}.md changed",
    "investigate_prompt": f"prompts/{INVESTIGATE_PROMPT}.{INVESTIGATE_VERSION}.md changed",
    "contracts": "the contracts changed (generator)",
    "prompt": f"prompts/{PROMPT_NAME}.{PROMPT_VERSION}.md changed",
    "schema": "the Invoice schema changed",
    "documents": "the text read from the invoice PDFs changed (generator, PDF reader, or "
    "a platform difference in rendering)",
}


def stale_reasons(recorded: dict[str, str], current: dict[str, str]) -> list[str]:
    return [reason for key, reason in _STALE_REASON.items() if recorded.get(key) != current[key]]


def gate_metrics(
    extraction: dict, decisions: dict, investigation: dict | None = None
) -> dict[str, float]:
    metrics = {
        "key_field_accuracy": extraction["key_field_accuracy"],
        "decision_accuracy": decisions["accuracy"],
        "false_approve_rate": decisions["false_approve_rate"] or 0.0,
    }
    if investigation and investigation["diagnoses"]:
        metrics["cause_accuracy"] = investigation["cause_accuracy"]
    return metrics


def gate_failures(baseline: dict[str, float], current: dict[str, float]) -> list[str]:
    failures = []
    for name in ("decision_accuracy", "key_field_accuracy", "cause_accuracy"):
        if name not in baseline:
            continue
        if current.get(name, 0.0) < baseline[name] - GATE_MAX_DROP:
            failures.append(
                f"{name} {current.get(name, 0.0):.1%} is more than {GATE_MAX_DROP:.0%} below the "
                f"recorded {baseline[name]:.1%}"
            )
    if current["false_approve_rate"] > baseline["false_approve_rate"]:
        failures.append(
            f"false_approve_rate rose from {baseline['false_approve_rate']:.1%} to "
            f"{current['false_approve_rate']:.1%}"
        )
    return failures


def eval_match_decide(records: list[GroundTruth], erp: ErpData, settings: Settings) -> dict:
    """Steps 4, 5 and 9 given perfect extraction and no contract check: isolates the
    deterministic nodes. Expected outcomes are those before the contract check."""
    expected, decisions, codes_correct, wrong = [], [], 0, []
    for record in records:
        issues = check_invoice(record.invoice, erp, settings)
        decision = decide(record.invoice, issues, settings.approval_threshold).decision
        codes = sorted({issue.code for issue in issues})
        wanted = decide(
            record.invoice,
            [Issue(code=code, message=code) for code in record.expected_check_codes],
            settings.approval_threshold,
        ).decision
        expected.append(wanted)
        decisions.append(decision)
        codes_correct += codes == record.expected_check_codes
        if decision != wanted or codes != record.expected_check_codes:
            wrong.append({"id": record.id, "decision": decision.value, "issue_codes": codes})
    return {
        **decision_metrics(expected, decisions),
        "issue_code_accuracy": codes_correct / len(records),
        "by_label": by_label(records, decisions),
        "wrong": wrong,
    }


def eval_investigation(records: list[GroundTruth], results: list[PipelineResult]) -> dict:
    """The investigation on the invoices it should diagnose: was each expected cause found,
    was every cited record one the tools returned, and what did it take."""
    rows = []
    for record, result in zip(records, results, strict=True):
        if record.expected_route != Route.INVESTIGATE:
            continue
        report = result.investigation
        expected = list(zip(record.expected_issue_codes, record.expected_causes, strict=True))
        diagnoses = []
        for code, cause in expected:
            found = (
                next((d for d in report.diagnoses if d.issue_code == code), None)
                if report
                else None
            )
            diagnoses.append(
                {
                    "issue_code": code,
                    "expected": cause.value,
                    "found": found.cause.value if found else None,
                    "correct": bool(found and found.cause == cause),
                    "evidence_valid": bool(found and found.evidence_valid),
                    "rejected_cause": found.rejected_cause if found else None,
                }
            )
        rows.append(
            {
                "id": record.id,
                "label": record.label,
                "investigated": report is not None,
                "finished": bool(report and report.finished),
                "turns": report.turns if report else 0,
                "seconds": report.seconds if report else None,
                "tool_calls": report.tool_calls if report else [],
                "diagnoses": diagnoses,
            }
        )
    all_diagnoses = [d for r in rows for d in r["diagnoses"]]
    by_label: dict[str, list[bool]] = {}
    for row in rows:
        by_label.setdefault(row["label"], []).extend(d["correct"] for d in row["diagnoses"])
    seconds = [r["seconds"] for r in rows if r["seconds"] is not None]
    turns = [r["turns"] for r in rows if r["investigated"]]
    return {
        "invoices": len(rows),
        "investigated": sum(r["investigated"] for r in rows),
        "finished": sum(r["finished"] for r in rows),
        "diagnoses": len(all_diagnoses),
        "cause_correct": sum(d["correct"] for d in all_diagnoses),
        "cause_accuracy": (
            sum(d["correct"] for d in all_diagnoses) / len(all_diagnoses) if all_diagnoses else 0.0
        ),
        "evidence_valid": sum(d["evidence_valid"] for d in all_diagnoses),
        "causes_rejected_by_code": sum(bool(d["rejected_cause"]) for d in all_diagnoses),
        "by_label": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_label.items())},
        "turns_mean": sum(turns) / len(turns) if turns else None,
        "turns_max": max(turns) if turns else None,
        "seconds_p50": percentile(seconds, 50),
        "seconds_p95": percentile(seconds, 95),
        "rows": rows,
    }


def eval_contract(records: list[GroundTruth], results: list[PipelineResult]) -> dict:
    """The contract check on the invoices where the contract decides: was the price
    mismatch cleared exactly when it should be, and was the right clause cited?"""
    rows = []
    for record, result in zip(records, results, strict=True):
        looked = result.contract_findings
        cleared = any(f.verdict == "explained" for f in looked)
        if record.expected_citation is None and not cleared:
            continue
        rows.append(
            {
                "id": record.id,
                "label": record.label,
                "variant": record.variant,
                "should_clear": record.expected_check_codes != record.expected_issue_codes,
                "cleared": cleared,
                "cited_expected": record.expected_citation in {f.clause_id for f in looked},
                "verdicts": [f.verdict for f in looked],
            }
        )
    should = [r for r in rows if r["should_clear"]]
    should_not = [r for r in rows if not r["should_clear"]]
    cited = [r for r in rows if r["label"] == "price_amendment"]
    return {
        "cases": len(rows),
        "cleared_when_it_should": sum(r["cleared"] for r in should),
        "should_clear": len(should),
        "cleared_when_it_should_not": sum(r["cleared"] for r in should_not),
        "should_not_clear": len(should_not),
        "cited_the_addendum": sum(r["cited_expected"] for r in cited),
        "addendum_cases": len(cited),
        "rows": rows,
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

    actual = [result.decision for result in results]
    decisions = decision_metrics([record.expected_decision for record in records], actual)
    decisions["by_label"] = by_label(records, actual)
    routes = Counter(
        f"{record.expected_route.value}->{result.route.value if result.route else 'none'}"
        for record, result in zip(records, results, strict=True)
    )
    decisions["route"] = {
        "correct": sum(
            r.expected_route == res.route for r, res in zip(records, results, strict=True)
        ),
        "documents": len(records),
        "confusion": dict(sorted(routes.items())),
    }

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
                    "label": record.label,
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
    parser.add_argument(
        "--no-investigate",
        action="store_true",
        help="skip the investigation loop (it costs several model calls per held invoice)",
    )
    parser.add_argument(
        "--split",
        choices=SPLIT_CHOICES,
        help="which invoices (default: dev; --gate uses the split it was recorded on)",
    )
    args = parser.parse_args()
    if args.gate and args.split:
        parser.error("--gate replays the split the recordings were made on")
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
    erp = ErpData.load(data_dir)
    recording_path = RECORDINGS_DIR / RECORDING_FILE
    recording: dict = {}
    if args.gate:
        if not recording_path.exists():
            print(f"No recordings in {RECORDINGS_DIR}. Create them with --record.")
            return 1
        recording = json.loads(recording_path.read_text(encoding="utf-8"))
    split = recording.get("split", "dev") if args.gate else args.split or "dev"
    records = load_ground_truth(data_dir, split)
    print(f"Dataset: {data_dir}  {manifest}  split: {split} ({len(records)} invoices)")
    paths = [data_dir / record.file for record in records]

    retrieval_recordings = RECORDINGS_DIR / "retrieval"
    # The gate replays what was recorded, investigation included or not.
    investigate = recording.get("investigate", False) if args.gate else not args.no_investigate
    current_fingerprint = fingerprint(paths, settings) if args.record or args.gate else {}
    client: ChatClient | None = None
    retriever: ContractRetriever | None = None
    try:
        if args.gate:
            if stale := stale_reasons(recording["fingerprint"], current_fingerprint):
                print("\nGATE FAILED: the recordings are stale because " + "; ".join(stale))
                print("Re-record with --record --split dev (calls the LLM).")
                return 1
            client = RecordingClient(
                RECORDINGS_DIR, None, model=recording["model"], host=recording["provider"]
            )
            retriever = build_retriever(settings, retrieval_recordings, replay_only=True)
        elif args.record:
            client = build_client(settings, RECORDINGS_DIR)
            retriever = build_retriever(settings, retrieval_recordings)
            if retriever is None:
                print("Recording needs the contract index (podman compose up -d).")
                return 2
        elif not args.skip_llm:
            client = build_client(settings, args.cache, args.replay_only)
            retriever = build_retriever(
                settings, args.cache / "retrieval" if args.cache else None, args.replay_only
            )
    except LLMNotConfiguredError as error:
        print(f"{error}\nOr pass --skip-llm for the deterministic evals.", file=sys.stderr)
        return 2

    report: dict = {
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dataset": manifest,
        "split": split,
        "match_decide_on_ground_truth": eval_match_decide(records, erp, settings),
    }
    print_decisions(
        "Match + decide on ground-truth fields (no LLM)", report["match_decide_on_ground_truth"]
    )
    accuracy = report["match_decide_on_ground_truth"]["issue_code_accuracy"]
    print(f"  issue-code accuracy   {percent(accuracy)}")

    if client is None:
        print("\nExtraction and end-to-end decision: skipped (--skip-llm), not measured.")
    else:
        started = time.monotonic()
        configure_tracing(settings)
        try:
            results = process_invoices(
                paths,
                erp,
                client,
                settings,
                args.workers or settings.llm_workers,
                retriever,
                investigate,
            )
        except (RecordingMissingError, RetrievalMissingError) as error:
            print(f"\nGATE FAILED: {error}. Re-record with --record (calls the LLM).")
            return 1
        finally:
            shutdown_tracing()
        elapsed = time.monotonic() - started
        extraction, decisions, misses = eval_pipeline(records, results)
        seconds = [result.seconds for result in results if result.invoice is not None]
        report |= {
            "model": client.model,
            # Which endpoint served the model: the same model ID can differ by provider.
            "provider": getattr(client, "host", None),
            "workers": args.workers or settings.llm_workers,
            "elapsed_seconds": round(elapsed, 1),
            "usage": getattr(client, "usage", None),
            # Per-invoice wall time, parse to decision. Not a measurement when replayed.
            "seconds_p50": percentile(seconds, 50),
            "seconds_p95": percentile(seconds, 95),
            # With --cache: how many replies were fresh and how many came from disk.
            "replies": getattr(client, "counts", None),
            "prompt": f"{PROMPT_NAME}.{PROMPT_VERSION}",
            "structured_output": settings.llm_structured_output,
            "system_role": settings.llm_system_role,
            "temperature": settings.llm_temperature,
            "extraction": extraction,
            "decision_end_to_end": decisions,
            "misses": misses,
            "contract_check": eval_contract(records, results) if retriever else None,
            "investigation": eval_investigation(records, results) if investigate else None,
        }

        print(
            f"\nExtraction: model {client.model}, prompt {PROMPT_NAME}.{PROMPT_VERSION}, "
            f"structured output {settings.llm_structured_output}"
        )
        print(f"  provider              {report['provider']}")
        print(f"  elapsed / usage       {elapsed:.1f}s  {report['usage']}")
        if seconds:
            print(
                f"  per invoice           p50 {report['seconds_p50']:.1f}s, "
                f"p95 {report['seconds_p95']:.1f}s"
            )
        print(f"  key-field accuracy    {percent(extraction['key_field_accuracy'])}")
        print(f"  all key fields right  {percent(extraction['documents_with_all_key_fields'])}")
        print(f"  whole invoice right   {percent(extraction['documents_fully_correct'])}")
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
            print(f"    {name:<20}{percent(extraction['field_accuracy'][name])}  {marker}")
        print("  key-field accuracy by layout/language")
        for group, value in extraction["key_field_accuracy_by_layout"].items():
            print(f"    {group:<18}{percent(value)}")
        print_decisions("Decision end to end (PDF -> extraction -> decision)", decisions)
        route = decisions["route"]
        print(
            f"  route                 {route['correct']}/{route['documents']}  {route['confusion']}"
        )
        investigation = report["investigation"]
        if investigation is None:
            print("\nInvestigation: off (--no-investigate).")
        else:
            print(
                f"\nInvestigation: {investigation['investigated']}/{investigation['invoices']} "
                f"investigated, {investigation['finished']} finished; cause "
                f"{investigation['cause_correct']}/{investigation['diagnoses']}, evidence valid "
                f"{investigation['evidence_valid']}/{investigation['diagnoses']}, causes "
                f"rejected by code {investigation['causes_rejected_by_code']}"
            )
            print(f"  by class              {investigation['by_label']}")
            if investigation["turns_mean"] is not None:
                print(
                    f"  turns mean/max        {investigation['turns_mean']:.1f}/"
                    f"{investigation['turns_max']}, seconds p50/p95 "
                    f"{investigation['seconds_p50']}/{investigation['seconds_p95']}"
                )
        contract = report["contract_check"]
        if contract is None:
            print("\nContract check: off (no contract index), price mismatches stay held.")
        else:
            print(
                f"\nContract check: cleared {contract['cleared_when_it_should']}/"
                f"{contract['should_clear']} it should, "
                f"{contract['cleared_when_it_should_not']}/{contract['should_not_clear']} it "
                f"should not; cited the addendum in {contract['cited_the_addendum']}/"
                f"{contract['addendum_cases']}"
            )
        for miss in misses[:MAX_MISSES_SHOWN]:
            print(
                f"  miss {miss['id']} {miss['label']} ({miss['layout']}): {miss['decision']}",
                end="",
            )
            print(f", expected {miss['expected_decision']}; wrong: {list(miss['wrong_fields'])}")
        if len(misses) > MAX_MISSES_SHOWN:
            print(f"  ... {len(misses) - MAX_MISSES_SHOWN} more misses in the saved run")

    exit_code = 0
    if args.record:
        assert isinstance(client, RecordingClient)
        if report["extraction"]["unanswered"]:
            print("\nNOT RECORDED: some invoices got no reply. Run --record again to fill them.")
            exit_code = 1
        else:
            for stale_file in set(RECORDINGS_DIR.glob("*.json")) - client.used - {recording_path}:
                stale_file.unlink()
            for stale_file in set(retrieval_recordings.glob("*.json")) - retriever.used:
                stale_file.unlink()
            recorded = {
                "recorded_at": report["started_at"],
                "provider": client.host,
                "model": client.model,
                "prompt": report["prompt"],
                "structured_output": settings.llm_structured_output,
                "system_role": settings.llm_system_role,
                "dataset": manifest,
                "split": split,
                "replies": len(client.used),
                "fingerprint": current_fingerprint,
                "investigate": investigate,
                "baseline": gate_metrics(
                    report["extraction"], report["decision_end_to_end"], report["investigation"]
                ),
            }
            recording_path.write_text(json.dumps(recorded, indent=2) + "\n", encoding="utf-8")
            print(
                f"\nRecorded {len(client.used)} replies and the gate baseline to {RECORDINGS_DIR}"
            )
    if args.gate:
        current = gate_metrics(
            report["extraction"], report["decision_end_to_end"], report["investigation"]
        )
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
