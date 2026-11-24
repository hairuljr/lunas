"""The `lunas` command.

    uv run lunas run        every invoice PDF in the data directory, from PDF to decision
    uv run lunas pending    invoices stopped at the approval checkpoint
    uv run lunas approve    a person's answer for one of them; the invoice then resumes

Checkpoints go to Postgres when it is reachable, so an invoice waiting for approval
survives a restart; without it they live in memory and `pending` has nothing to show.
"""

import argparse
import sys
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from lunas.config import Settings
from lunas.erp import ErpData
from lunas.llm import LLMNotConfiguredError, build_client
from lunas.models import Decision
from lunas.pipeline.graph import (
    Approval,
    build_graph,
    memory_checkpointer,
    pending_approvals,
    postgres_checkpointer,
    resume_approval,
)
from lunas.pipeline.run import process_invoices
from lunas.retrieval.retriever import build_retriever
from lunas.telemetry import configure_tracing, shutdown_tracing

INVOICE_DIR = "invoices"


def find_invoices(data_dir: Path) -> list[Path]:
    paths = sorted((data_dir / INVOICE_DIR).glob("*.pdf"))
    if not paths:
        raise SystemExit(
            f"No invoices in {data_dir / INVOICE_DIR}. Run: uv run python data/generate.py"
        )
    return paths


@contextmanager
def checkpoints(settings: Settings, required: bool = False) -> Iterator:
    try:
        psycopg.connect(settings.database_url, connect_timeout=3).close()
    except psycopg.OperationalError as error:
        if required:
            raise SystemExit(f"Approvals need Postgres (podman compose up -d): {error}") from None
        print("Checkpoints in memory only: no database.", file=sys.stderr)
        yield memory_checkpointer()
        return
    with postgres_checkpointer(settings.database_url) as saver:
        yield saver


def run(args: argparse.Namespace) -> int:
    settings = Settings()
    data_dir = args.data or settings.data_dir
    paths = find_invoices(data_dir)[: args.limit]
    try:
        client = build_client(settings, args.cache)
    except LLMNotConfiguredError as error:
        print(error, file=sys.stderr)
        return 2

    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    tracing = configure_tracing(settings)
    try:
        retriever = build_retriever(settings, args.cache / "retrieval" if args.cache else None)
        vision_client = (
            build_client(
                settings.model_copy(update={"llm_model": settings.vision_model}), args.cache
            )
            if args.vision
            else None
        )
        with checkpoints(settings) as saver:
            results = process_invoices(
                paths,
                ErpData.load(data_dir),
                client,
                settings,
                args.workers or settings.llm_workers,
                retriever,
                args.investigate,
                saver,
                run_id,
                vision_client,
            )
    finally:
        shutdown_tracing()  # flushes: a CLI that just exits loses its last spans
    for result in results:
        codes = ", ".join(sorted({issue.code for issue in result.issues}))
        status = "waits for approval" if result.status == "awaiting_approval" else ""
        print(
            f"{result.file:<16}{result.decision:<10}{(result.route or ''):<13}{status:<20}{codes}"
        )
    counts = Counter(result.decision.value for result in results)
    summary = ", ".join(f"{count} {decision}" for decision, count in sorted(counts.items()))
    waiting = sum(result.status == "awaiting_approval" for result in results)
    print(f"\n{len(results)} invoices via {client.model}: {summary}; {waiting} wait for approval")
    print(f"Run {run_id}. Answer with: uv run lunas approve {run_id}/<file> --decision ...")
    if tracing:
        # Not a delivery receipt: a rejected export is reported by the exporter on stderr.
        print(f"Tracing to {settings.otlp_endpoint or settings.trace_file}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            "".join(result.model_dump_json() + "\n" for result in results), encoding="utf-8"
        )
        print(f"Results written to {args.json}")
    return 0


def pending(args: argparse.Namespace) -> int:
    settings = Settings()
    erp = ErpData.load(args.data or settings.data_dir)
    with checkpoints(settings, required=True) as saver:
        waiting = pending_approvals(build_graph(erp, None, settings, saver), saver)
    for thread_id, request in waiting.items():
        print(
            f"{thread_id:<36}{request['decision']:<10}{request['route']:<8}"
            f"{request['vendor_name'] or '-'}  {request['total'] or '-'}  "
            + "; ".join(request["reasons"])
        )
    print(f"{len(waiting)} invoices wait for approval")
    return 0


def approve(args: argparse.Namespace) -> int:
    settings = Settings()
    erp = ErpData.load(args.data or settings.data_dir)
    answer = Approval(
        decision=Decision(args.decision),
        approver=args.approver,
        note=args.note,
        decided_at=datetime.now(UTC),
    )
    with checkpoints(settings, required=True) as saver:
        graph = build_graph(erp, None, settings, saver)
        try:
            state = resume_approval(graph, args.thread_id, answer)
        except ValueError as error:
            print(error, file=sys.stderr)
            return 1
    print(
        f"{args.thread_id}: code decided {state.decision.value}, {answer.approver} decided "
        f"{state.approval.decision.value}"
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="lunas", description="Accounts-payable agent.")
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser("run", help="process every invoice PDF in the data directory")
    run_parser.add_argument("--data", type=Path, help="dataset directory (default: data/out)")
    run_parser.add_argument(
        "--workers", type=int, help="parallel LLM calls (default: LUNAS_LLM_WORKERS)"
    )
    run_parser.add_argument(
        "--cache", type=Path, help="record LLM replies in this directory and replay them"
    )
    run_parser.add_argument("--limit", type=int, help="only the first N invoices")
    run_parser.add_argument(
        "--investigate", action="store_true", help="run the investigation loop on held invoices"
    )
    run_parser.add_argument(
        "--vision",
        action="store_true",
        help="read doubtful OCR results again with LUNAS_VISION_MODEL (paid per page)",
    )
    run_parser.add_argument("--json", type=Path, help="also write results as JSON lines")
    run_parser.set_defaults(handler=run)

    pending_parser = commands.add_parser("pending", help="invoices waiting for approval")
    pending_parser.add_argument("--data", type=Path, help="dataset directory (default: data/out)")
    pending_parser.set_defaults(handler=pending)

    approve_parser = commands.add_parser("approve", help="answer an invoice waiting for approval")
    approve_parser.add_argument("thread_id", help="as `lunas pending` shows it")
    approve_parser.add_argument("--decision", required=True, choices=["approve", "reject", "hold"])
    approve_parser.add_argument("--approver", required=True)
    approve_parser.add_argument("--note", default="")
    approve_parser.add_argument("--data", type=Path, help="dataset directory (default: data/out)")
    approve_parser.set_defaults(handler=approve)

    args = parser.parse_args()
    raise SystemExit(args.handler(args))
