"""The workflow as a LangGraph state graph (SPEC §4), so an invoice can stop for a person and
resume later, even in another process, from its last checkpoint.

    read -> check -> contract -> route -> [investigate] -> decide -> [approval] -> end
      \\________________________/  (an unreadable invoice goes straight to route)

`approval` interrupts when the route is `human` or the decision is `escalate`. The graph
then stops with the invoice's state saved; `resume_approval` continues it with a person's
answer. Nothing else in the graph waits for anyone, so a batch or an eval runs through and
records which invoices stopped, and where.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from lunas.config import Settings
from lunas.erp import ErpData
from lunas.ingest import choose_backend, get_reader
from lunas.llm import ChatClient, LLMRequestError, StructuredOutputError
from lunas.models import Backend, Decision, Invoice, Issue, LineItem
from lunas.pipeline.checks import EXTRACTION_FAILED, LLM_UNAVAILABLE, check_invoice, codes
from lunas.pipeline.nodes.contract import ContractFinding, check_contract
from lunas.pipeline.nodes.decide import decide
from lunas.pipeline.nodes.extract import extract_invoice
from lunas.pipeline.nodes.grounding import ground_invoice
from lunas.pipeline.nodes.investigate import (
    Action,
    Diagnosis,
    Evidence,
    EvidenceKind,
    ExceptionCause,
    InvestigationReport,
)
from lunas.pipeline.nodes.investigate import investigate as run_investigation
from lunas.pipeline.nodes.route import Route, route_invoice
from lunas.retrieval.retriever import ContractRetriever
from lunas.telemetry import set_attributes, span


class Approval(BaseModel):
    """A person's answer at the approval checkpoint."""

    decision: Literal[Decision.APPROVE, Decision.REJECT, Decision.HOLD]
    approver: str
    note: str = ""
    decided_at: datetime | None = None


class ApprovalRequest(BaseModel):
    """What the person is shown: the code's decision and why, never the invoice's text."""

    invoice_number: str | None
    vendor_name: str | None
    po_number: str | None
    total: str | None
    decision: Decision
    route: Route
    reasons: list[str]
    actions: list[Action] = []


class InvoiceState(BaseModel):
    path: str
    backend: Backend | None = None
    invoice: Invoice | None = None
    issues: list[Issue] = []
    # Extracted values not printed in the document (step 3b), found where the text is read.
    # The text itself is not kept: raw invoice text stays out of state and checkpoints.
    grounding: list[Issue] = []
    findings: list[ContractFinding] = []
    route: Route | None = None
    investigation: InvestigationReport | None = None
    decision: Decision | None = None
    reasons: list[Issue] = []
    approval: Approval | None = None


def needs_approval(state: InvoiceState) -> bool:
    return state.route == Route.HUMAN or state.decision == Decision.ESCALATE


# Checkpoints store these types. Listing them keeps deserialisation explicit, as LangGraph
# will require (it warns about unlisted types now and will refuse them later).
_STATE_TYPES = [
    InvoiceState, Approval, ApprovalRequest, Invoice, LineItem, Issue, Decision, Route,
    ContractFinding, InvestigationReport, Diagnosis, Evidence, EvidenceKind, ExceptionCause,
]  # fmt: skip
SERDE = JsonPlusSerializer(
    allowed_msgpack_modules=[(cls.__module__, cls.__name__) for cls in _STATE_TYPES]
)


def memory_checkpointer() -> InMemorySaver:
    """For evals, tests and the gate: state lives as long as the process."""
    return InMemorySaver(serde=SERDE)


@contextmanager
def postgres_checkpointer(database_url: str) -> Iterator[PostgresSaver]:
    """Checkpoints in Postgres: an invoice waiting for approval survives a restart."""
    pool = ConnectionPool(
        database_url,
        kwargs={"autocommit": True, "prepare_threshold": 0},
        max_size=8,
        open=True,
    )
    try:
        saver = PostgresSaver(pool, serde=SERDE)
        saver.setup()
        yield saver
    finally:
        pool.close()


def build_graph(
    erp: ErpData,
    client: ChatClient | None,
    settings: Settings,
    checkpointer,
    retriever: ContractRetriever | None = None,
    investigate: bool = False,
) -> CompiledStateGraph:
    """`client` may be None only to resume an approval: no node after it calls a model."""

    def read(state: InvoiceState) -> dict:
        path = Path(state.path)
        with span("parse") as current:
            backend = choose_backend(path, settings.ingest_mode, settings.min_chars_per_page)
            document = get_reader(backend).read(path)
            set_attributes(
                current,
                {
                    "app.ingest.backend": backend,
                    "app.ingest.pages": len(document.pages),
                    "app.ingest.characters": len(document.text),
                },
            )
        try:
            invoice = extract_invoice(document, client)
            with span("grounding") as current:
                grounding = ground_invoice(invoice, document.text)
                set_attributes(current, {"app.issue_codes": codes(grounding)})
            return {"backend": backend, "invoice": invoice, "grounding": grounding}
        except (StructuredOutputError, LLMRequestError) as error:
            # An invoice that could not be read is never paid and never silently dropped,
            # and one failed request does not take the rest of the batch down with it.
            code = (
                EXTRACTION_FAILED if isinstance(error, StructuredOutputError) else LLM_UNAVAILABLE
            )
            return {"backend": backend, "issues": [Issue(code=code, message=str(error))]}

    def check(state: InvoiceState) -> dict:
        return {"issues": state.grounding + check_invoice(state.invoice, erp, settings)}

    def contract(state: InvoiceState) -> dict:
        if retriever is None:
            return {}
        issues, findings = check_contract(
            state.invoice, state.issues, erp, retriever, client, settings.amount_tolerance
        )
        return {"issues": issues, "findings": findings}

    def route(state: InvoiceState) -> dict:
        with span("route") as current:
            routed = route_invoice(state.issues)
            set_attributes(
                current, {"app.route": routed.route.value, "app.route.because": routed.because}
            )
        return {"route": routed.route}

    def investigation(state: InvoiceState) -> dict:
        report = run_investigation(
            state.invoice,
            state.issues,
            erp,
            client,
            retriever.search_clauses if retriever else None,
            state.findings,
            tax_rate=settings.tax_rate,
        )
        return {"investigation": report}

    def decision(state: InvoiceState) -> dict:
        with span("decide") as current:
            if state.invoice is None:
                result_decision, reasons = Decision.HOLD, state.issues
            else:
                result = decide(state.invoice, state.issues, settings.approval_threshold)
                result_decision, reasons = result.decision, result.reasons
            set_attributes(current, {"app.decision": result_decision.value})
        return {"decision": result_decision, "reasons": reasons}

    def approval(state: InvoiceState) -> dict:
        with span("approval", {"app.route": state.route.value}) as current:
            invoice = state.invoice
            request = ApprovalRequest(
                invoice_number=invoice.invoice_number if invoice else None,
                vendor_name=invoice.vendor_name if invoice else None,
                po_number=invoice.po_number if invoice else None,
                total=str(invoice.total) if invoice else None,
                decision=state.decision,
                route=state.route,
                reasons=[issue.message for issue in state.reasons],
                actions=state.investigation.actions() if state.investigation else [],
            )
            answer = Approval.model_validate(interrupt(request.model_dump(mode="json")))
            set_attributes(
                current,
                {
                    "app.approval.decision": answer.decision.value,
                    "app.approval.by": answer.approver,
                },
            )
        return {"approval": answer}

    graph = StateGraph(InvoiceState)
    for name, node in (
        ("read", read),
        ("check", check),
        ("contract", contract),
        ("route", route),
        ("investigate", investigation),
        ("decide", decision),
        ("approval", approval),
    ):
        graph.add_node(name, node)
    graph.add_edge(START, "read")
    graph.add_conditional_edges(
        "read", lambda s: "check" if s.invoice is not None else "route", ["check", "route"]
    )
    graph.add_edge("check", "contract")
    graph.add_edge("contract", "route")
    graph.add_conditional_edges(
        "route",
        lambda s: "investigate" if investigate and s.route == Route.INVESTIGATE else "decide",
        ["investigate", "decide"],
    )
    graph.add_edge("investigate", "decide")
    graph.add_conditional_edges(
        "decide", lambda s: "approval" if needs_approval(s) else END, ["approval", END]
    )
    graph.add_edge("approval", END)
    return graph.compile(checkpointer=checkpointer)


def config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def resume_approval(graph: CompiledStateGraph, thread_id: str, answer: Approval) -> InvoiceState:
    """Continue an invoice that stopped at approval. Refuses one that is not waiting."""
    snapshot = graph.get_state(config(thread_id))
    if "approval" not in snapshot.next:
        raise ValueError(f"{thread_id} is not waiting for approval")
    graph.invoke(Command(resume=answer.model_dump(mode="json")), config(thread_id))
    return InvoiceState.model_validate(graph.get_state(config(thread_id)).values)


def pending_approvals(graph: CompiledStateGraph, checkpointer) -> dict[str, dict]:
    """Every thread in the checkpointer stopped at approval, with what it asks."""
    pending = {}
    for thread_id in {item.config["configurable"]["thread_id"] for item in checkpointer.list(None)}:
        snapshot = graph.get_state(config(thread_id))
        if "approval" in snapshot.next and snapshot.interrupts:
            pending[thread_id] = snapshot.interrupts[0].value
    return dict(sorted(pending.items()))
