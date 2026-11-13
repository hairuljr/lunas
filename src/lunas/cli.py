"""`uv run lunas run`: every invoice PDF in the data directory, from PDF to decision."""

import argparse
import sys
from collections import Counter
from pathlib import Path

from lunas.config import Settings
from lunas.erp import ErpData
from lunas.llm import LLMNotConfiguredError, build_client
from lunas.pipeline.run import process_invoices

INVOICE_DIR = "invoices"


def find_invoices(data_dir: Path) -> list[Path]:
    paths = sorted((data_dir / INVOICE_DIR).glob("*.pdf"))
    if not paths:
        raise SystemExit(
            f"No invoices in {data_dir / INVOICE_DIR}. Run: uv run python data/generate.py"
        )
    return paths


def run(args: argparse.Namespace) -> int:
    settings = Settings()
    data_dir = args.data or settings.data_dir
    paths = find_invoices(data_dir)
    try:
        client = build_client(settings, args.cache)
    except LLMNotConfiguredError as error:
        print(error, file=sys.stderr)
        return 2

    results = process_invoices(
        paths, ErpData.load(data_dir), client, settings, args.workers or settings.llm_workers
    )
    for result in results:
        codes = ", ".join(sorted({issue.code for issue in result.issues}))
        print(f"{result.file:<16}{result.backend:<10}{result.decision:<10}{codes}")
    counts = Counter(result.decision.value for result in results)
    summary = ", ".join(f"{count} {decision}" for decision, count in sorted(counts.items()))
    print(f"\n{len(results)} invoices via {client.model}: {summary}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            "".join(result.model_dump_json() + "\n" for result in results), encoding="utf-8"
        )
        print(f"Results written to {args.json}")
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
    run_parser.add_argument("--json", type=Path, help="also write results as JSON lines")
    run_parser.set_defaults(handler=run)

    args = parser.parse_args()
    raise SystemExit(args.handler(args))
