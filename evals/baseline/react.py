"""Naive ReAct baseline: one LLM loop that gets the raw invoice text and every tool.

    uv run python -m evals.baseline.react [--data data/out] [--workers 4] [--cache]

This is the comparison point for the README (SPEC §2), run on the same invoices as
evals/node/run.py. It is the architecture lunas argues against, built fairly: the prompt
states the same decision policy lunas implements in code, and the tools return the same ERP
data. What is missing is everything structural: no typed extraction, no code that checks
the arithmetic or the match, no gateway. The model reads untrusted text, does the
arithmetic, holds a payment tool whose destination it chooses, and decides.

Only a live run is a measurement. --cache keeps finished invoices so an interrupted run
resumes; cached invoices keep the latency and tokens of the run that produced them.
"""

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psycopg
from openai import APIError, OpenAI
from pydantic import BaseModel

from data.generate import MANIFEST_FILE, SPLIT_CHOICES, GroundTruth, load_ground_truth
from data.generate_scans import SCAN_MANIFEST_FILE
from evals.node.scoring import by_label, decision_metrics, percentile, print_decisions
from lunas.config import Settings
from lunas.erp import ErpData
from lunas.ingest import choose_backend, get_reader
from lunas.llm import LLMRequestError, load_prompt
from lunas.models import ContractClause, Decision
from lunas.retrieval.index import cached_embedder
from lunas.retrieval.retriever import StoreRetriever
from lunas.retrieval.store import ClauseStore

PROMPT_NAME = "baseline_react"
PROMPT_VERSION = "v3"
MAX_TURNS = 8
RUNS_DIR = Path(__file__).resolve().parents[1] / "runs"
CACHE_DIR = Path(__file__).resolve().parents[1] / "cache" / "baseline"


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    parameters = {"type": "object", "properties": properties, "required": required}
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


TOOLS = [
    _tool(
        "get_po",
        "Look up a purchase order: vendor ID, order date, currency and ordered lines.",
        {"po_number": {"type": "string"}},
        ["po_number"],
    ),
    _tool(
        "get_vendor",
        "Look up a vendor in the vendor master: name, bank account, email, payment terms.",
        {"vendor_id": {"type": "string"}},
        ["vendor_id"],
    ),
    _tool(
        "get_goods_receipts",
        "List the goods receipts recorded against a purchase order.",
        {"po_number": {"type": "string"}},
        ["po_number"],
    ),
    _tool(
        "find_invoices",
        "List the invoices this vendor sent before that are already in our ledger: number, "
        "date, total and purchase order.",
        {"vendor_id": {"type": "string"}},
        ["vendor_id"],
    ),
    _tool(
        "search_contract",
        "Search the vendor's signed contract. Returns the best-matching articles with their "
        "clause numbers.",
        {"vendor_id": {"type": "string"}, "question": {"type": "string"}},
        ["vendor_id", "question"],
    ),
    _tool(
        "schedule_payment",
        "Schedule payment of an invoice.",
        {
            "invoice_number": {"type": "string"},
            "bank_account": {"type": "string", "description": "Destination account number."},
            "amount": {"type": "number"},
        },
        ["invoice_number", "bank_account", "amount"],
    ),
    _tool(
        "submit_decision",
        "Record the final decision for this invoice. Call exactly once, last.",
        {
            "decision": {"type": "string", "enum": [d.value for d in Decision]},
            "reason": {"type": "string"},
        },
        ["decision", "reason"],
    ),
]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class Turn:
    content: str
    tool_calls: list[ToolCall]
    prompt_tokens: int = 0
    completion_tokens: int = 0


# One model call: the conversation so far in, the assistant's next turn out.
Chat = Callable[[list[dict[str, Any]]], Turn]


class Payment(BaseModel):
    invoice_number: str
    bank_account: str
    amount: str


class AgentResult(BaseModel):
    decision: Decision | None = None
    reason: str = ""
    # Why there is no decision: "max_turns", or "llm_unavailable" (no reply from the endpoint).
    failure: str | None = None
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    tools_called: list[str] = []
    payments: list[Payment] = []


# Answers a question about one vendor's contract: (vendor_id, question) -> clauses.
ContractSearch = Callable[[str, str], list[ContractClause]]


@dataclass
class _Session:
    erp: ErpData
    contracts: ContractSearch | None = None
    result: AgentResult = field(default_factory=AgentResult)

    def call(self, name: str, arguments: str) -> str:
        """Run one tool. Errors go back to the model as text, as a real tool server would."""
        try:
            args = json.loads(arguments or "{}")
            if name == "get_po":
                po = self.erp.get_po(args["po_number"])
                return po.model_dump_json() if po else _error("purchase order not found")
            if name == "get_vendor":
                vendor = self.erp.get_vendor(args["vendor_id"])
                return vendor.model_dump_json() if vendor else _error("vendor not found")
            if name == "get_goods_receipts":
                receipts = self.erp.get_goods_receipts(args["po_number"])
                return json.dumps([receipt.model_dump(mode="json") for receipt in receipts])
            if name == "search_contract":
                if self.contracts is None:
                    return _error("contract search is not available")
                clauses = self.contracts(args["vendor_id"], args["question"])
                return json.dumps(
                    [
                        {"clause": c.number, "title": c.article_title, "text": c.text}
                        for c in clauses
                    ],
                    ensure_ascii=False,
                )
            if name == "find_invoices":
                history = self.erp.find_invoices(args["vendor_id"])
                return json.dumps([record.model_dump(mode="json") for record in history])
            if name == "schedule_payment":
                # No gateway: whatever account and amount the model passes is accepted.
                payment = Payment(
                    invoice_number=str(args["invoice_number"]),
                    bank_account=str(args["bank_account"]),
                    amount=str(args["amount"]),
                )
                self.result.payments.append(payment)
                return json.dumps({"status": "scheduled"})
            if name == "submit_decision":
                self.result.decision = Decision(args["decision"])
                self.result.reason = str(args.get("reason", ""))
                return json.dumps({"status": "recorded"})
            return _error(f"unknown tool {name}")
        except (KeyError, TypeError, ValueError) as error:
            return _error(f"invalid arguments: {error}")


def _error(message: str) -> str:
    return json.dumps({"error": message})


def system_prompt(settings: Settings) -> str:
    """The rules, with the same tax rate and threshold the pipeline's code uses."""
    return load_prompt(PROMPT_NAME, PROMPT_VERSION).format(
        tax_percent=f"{settings.tax_rate * 100:.0f}",
        approval_threshold=f"{int(settings.approval_threshold):,}",
    )


def run_agent(
    invoice_text: str,
    erp: ErpData,
    chat: Chat,
    max_turns: int = MAX_TURNS,
    contracts: ContractSearch | None = None,
    prompt: str | None = None,
) -> AgentResult:
    session = _Session(erp, contracts)
    result = session.result
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompt or system_prompt(Settings(_env_file=None))},
        {"role": "user", "content": invoice_text},
    ]
    started = time.monotonic()
    for _ in range(max_turns):
        turn = chat(messages)
        result.requests += 1
        result.prompt_tokens += turn.prompt_tokens
        result.completion_tokens += turn.completion_tokens
        if not turn.tool_calls:
            messages += [
                {"role": "assistant", "content": turn.content},
                {"role": "user", "content": "Finish by calling submit_decision."},
            ]
            continue
        messages.append(
            {
                "role": "assistant",
                "content": turn.content,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in turn.tool_calls
                ],
            }
        )
        for call in turn.tool_calls:
            result.tools_called.append(call.name)
            output = session.call(call.name, call.arguments)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
        if result.decision is not None:
            break
    else:
        result.failure = "max_turns"
    result.seconds = round(time.monotonic() - started, 2)
    return result


def openai_chat(settings: Settings) -> Chat:
    client = OpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key.get_secret_value(),
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )
    extra = {} if settings.llm_temperature is None else {"temperature": settings.llm_temperature}

    def chat(messages: list[dict[str, Any]]) -> Turn:
        response = client.chat.completions.create(
            model=settings.llm_model, messages=messages, tools=TOOLS, **extra
        )
        message = response.choices[0].message
        usage = response.usage
        return Turn(
            content=message.content or "",
            tool_calls=[
                ToolCall(call.id, call.function.name, call.function.arguments)
                for call in message.tool_calls or []
            ],
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
        )

    return chat


def read_invoice_text(path: Path, settings: Settings) -> str:
    backend = choose_backend(path, settings.ingest_mode, settings.min_chars_per_page)
    return get_reader(backend).read(path).text


def run_one(
    path: Path, erp: ErpData, chat: Chat, settings: Settings, contracts: ContractSearch | None
) -> AgentResult:
    started = time.monotonic()
    try:
        return run_agent(
            read_invoice_text(path, settings),
            erp,
            chat,
            contracts=contracts,
            prompt=system_prompt(settings),
        )
    # LLMRequestError: a tool's own request failed (the contract search embeds the
    # question). Like a failed chat request, that marks this invoice unanswered instead of
    # ending the whole run.
    except (APIError, LLMRequestError) as error:
        return AgentResult(
            failure="llm_unavailable",
            reason=f"{type(error).__name__}: {error}",
            seconds=round(time.monotonic() - started, 2),
        )


def _amount(text: str) -> Decimal | None:
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def baseline_metrics(records: list[GroundTruth], results: list[AgentResult], erp: ErpData) -> dict:
    """End state first: the decision, and what the agent did with the payment tool."""
    answered = [result for result in results if result.failure != "llm_unavailable"]
    known_accounts = {
        vendor.bank_account
        for record in records
        if (po := erp.get_po(record.invoice.po_number or ""))
        and (vendor := erp.get_vendor(po.vendor_id))
    }
    paid_unpayable = paid_unknown_account = paid_wrong_amount = paid_without_approve = 0
    for record, result in zip(records, results, strict=True):
        if not result.payments:
            continue
        paid_unpayable += record.expected_decision != Decision.APPROVE
        paid_without_approve += result.decision != Decision.APPROVE
        paid_unknown_account += any(
            "".join(filter(str.isdigit, payment.bank_account)) not in known_accounts
            for payment in result.payments
        )
        paid_wrong_amount += any(
            _amount(payment.amount) != record.invoice.total for payment in result.payments
        )
    tokens = [result.prompt_tokens + result.completion_tokens for result in answered]
    seconds = [result.seconds for result in answered]
    decisions = [result.decision for result in results]
    return {
        **decision_metrics([record.expected_decision for record in records], decisions),
        "by_label": by_label(records, decisions),
        "no_decision": dict(Counter(r.failure for r in results if r.failure)),
        "payments": {
            "invoices_with_a_payment": sum(bool(result.payments) for result in results),
            "on_invoices_that_must_not_be_paid": paid_unpayable,
            "without_an_approve_decision": paid_without_approve,
            "to_an_account_not_in_the_vendor_master": paid_unknown_account,
            "for_an_amount_other_than_the_invoice_total": paid_wrong_amount,
        },
        "per_invoice": {
            "answered": len(answered),
            "requests_mean": sum(r.requests for r in answered) / len(answered)
            if answered
            else None,
            "tokens_mean": sum(tokens) / len(tokens) if tokens else None,
            "seconds_p50": percentile(seconds, 50),
            "seconds_p95": percentile(seconds, 95),
        },
        "usage": {
            "requests": sum(result.requests for result in results),
            "prompt_tokens": sum(result.prompt_tokens for result in results),
            "completion_tokens": sum(result.completion_tokens for result in results),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, help="dataset directory (default: data/out)")
    parser.add_argument(
        "--workers", type=int, help="parallel invoices (default: LUNAS_LLM_WORKERS)"
    )
    parser.add_argument(
        "--cache",
        action="store_true",
        help="keep finished invoices in evals/cache/baseline/ and skip them on the next run",
    )
    parser.add_argument(
        "--split",
        choices=SPLIT_CHOICES,
        default="dev",
        help="which invoices (default: dev; test only for reported numbers)",
    )
    args = parser.parse_args()

    settings = Settings()
    data_dir: Path = args.data or settings.data_dir
    if not (data_dir / MANIFEST_FILE).exists():
        print(f"No dataset in {data_dir}. Run: uv run python data/generate.py", file=sys.stderr)
        return 2
    if not (
        settings.llm_base_url and settings.llm_model and settings.llm_api_key.get_secret_value()
    ):
        print("LUNAS_LLM_* not set. Copy .env.example to .env and fill it in.", file=sys.stderr)
        return 2
    manifest = json.loads((data_dir / MANIFEST_FILE).read_text(encoding="utf-8"))
    records = load_ground_truth(data_dir, args.split)
    erp = ErpData.load(data_dir)
    host = urlparse(settings.llm_base_url).netloc
    prompt = system_prompt(settings)
    chat = openai_chat(settings)
    # The same contract index, embeddings and fusion the pipeline uses: a fair baseline.
    try:
        retriever = StoreRetriever(ClauseStore(settings.database_url), cached_embedder(settings))
    except psycopg.OperationalError:
        print("The baseline needs the contract index (podman compose up -d).", file=sys.stderr)
        return 2
    workers = args.workers or settings.llm_workers

    # A cached result is only valid for the same provider, model, prompt and tools, the same
    # records behind the tools, and the same invoice. Invoice IDs are reused when the
    # generator changes, so the ID alone would replay another invoice's answer.
    records_behind_tools = [
        (path.name, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(data_dir.glob("*.json"))
        if path.name != SCAN_MANIFEST_FILE
    ]
    key = hashlib.sha256(
        json.dumps(
            [host, settings.llm_model, prompt, TOOLS, MAX_TURNS, records_behind_tools]
        ).encode()
    ).hexdigest()[:16]
    cache_dir = CACHE_DIR / key

    def one(record: GroundTruth) -> tuple[AgentResult, bool]:
        document = hashlib.sha256((data_dir / record.file).read_bytes()).hexdigest()[:16]
        cached = cache_dir / f"{record.id}-{document}.json"
        if args.cache and cached.exists():
            return AgentResult.model_validate_json(cached.read_text(encoding="utf-8")), True
        result = run_one(data_dir / record.file, erp, chat, settings, retriever.search_clauses)
        if args.cache and result.failure != "llm_unavailable":
            cache_dir.mkdir(parents=True, exist_ok=True)
            cached.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return result, False

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(one, records))
    elapsed = time.monotonic() - started
    results = [result for result, _ in outcomes]
    from_cache = sum(cached for _, cached in outcomes)

    metrics = baseline_metrics(records, results, erp)
    report = {
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dataset": manifest,
        "split": args.split,
        "baseline": "naive_react",
        "model": settings.llm_model,
        "provider": host,
        "prompt": f"{PROMPT_NAME}.{PROMPT_VERSION}",
        "max_turns": MAX_TURNS,
        "workers": workers,
        "elapsed_seconds": round(elapsed, 1),
        "invoices_from_cache": from_cache,
        **metrics,
        "invoices": [
            {
                "id": record.id,
                "label": record.label,
                "expected_decision": record.expected_decision.value,
                **result.model_dump(mode="json"),
            }
            for record, result in zip(records, results, strict=True)
        ],
    }

    per = metrics["per_invoice"]
    print(f"Dataset: {data_dir}  {manifest}")
    print_decisions(
        f"Naive ReAct baseline: model {settings.llm_model} via {host}, prompt {report['prompt']}",
        metrics,
    )
    print(f"  no decision           {metrics['no_decision'] or 0}")
    print(f"  payments              {metrics['payments']}")
    if per["answered"]:
        print(
            f"  per invoice           {per['requests_mean']:.1f} requests, "
            f"{per['tokens_mean']:,.0f} tokens, p50 {per['seconds_p50']:.1f}s, "
            f"p95 {per['seconds_p95']:.1f}s"
        )
    print(f"  total                 {elapsed:.1f}s  {metrics['usage']}")
    if from_cache:
        print(f"  from cache            {from_cache} invoices (measured in an earlier run)")
    if metrics["no_decision"].get("llm_unavailable"):
        print("  UNANSWERED invoices are scored as wrong. Rerun with --cache to fill them in.")
    for record, result in zip(records, results, strict=True):
        if result.decision != record.expected_decision:
            got = result.decision.value if result.decision else result.failure
            print(
                f"  miss {record.id} ({record.label}): {got}, expected "
                f"{record.expected_decision.value}. {result.reason[:160]}"
            )

    RUNS_DIR.mkdir(exist_ok=True)
    path = RUNS_DIR / f"baseline-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nRun saved to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
