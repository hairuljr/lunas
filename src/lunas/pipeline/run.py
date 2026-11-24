"""Run invoices through the workflow graph (`lunas.pipeline.graph`).

Each invoice is one graph thread. It runs to its decision, or stops at the approval
checkpoint; nothing here waits for a person. `PipelineResult.status` says which.
"""

import time
import uuid
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel

from lunas.config import Settings
from lunas.erp import ErpData
from lunas.gapura.gateway import Gateway
from lunas.ingest.recorded import DocumentRecorder
from lunas.llm import ChatClient
from lunas.models import Backend, Decision, Invoice, Issue
from lunas.pipeline.checks import EXTRACTION_FAILED, LLM_UNAVAILABLE, check_invoice, codes
from lunas.pipeline.graph import (
    Approval,
    ApprovalRequest,
    InvoiceState,
    build_graph,
    config,
    memory_checkpointer,
)
from lunas.pipeline.nodes.act import ActionTaken
from lunas.pipeline.nodes.contract import ContractFinding
from lunas.pipeline.nodes.investigate import InvestigationReport
from lunas.pipeline.nodes.route import Route
from lunas.retrieval.retriever import ContractRetriever
from lunas.telemetry import set_attributes, span

__all__ = [
    "EXTRACTION_FAILED",
    "LLM_UNAVAILABLE",
    "PipelineResult",
    "check_invoice",
    "process_invoice",
    "process_invoices",
]

Status = Literal["decided", "awaiting_approval", "resolved"]


class PipelineResult(BaseModel):
    file: str
    backend: Backend
    # None when extraction failed; the decision is then HOLD.
    invoice: Invoice | None
    issues: list[Issue]
    # The code's decision. A person's answer at the approval checkpoint is in `approval`.
    decision: Decision
    # What the contract check found for each line it looked at.
    contract_findings: list[ContractFinding] = []
    # Who looks next (step 7).
    route: Route | None = None
    # The diagnosis of an invoice routed to investigation (step 8). Never changes the decision.
    investigation: InvestigationReport | None = None
    # decided: no person needed. awaiting_approval: stopped at the checkpoint, state saved.
    # resolved: a person answered.
    status: Status = "decided"
    thread_id: str | None = None
    approval_request: ApprovalRequest | None = None
    approval: Approval | None = None
    # Wall time for this invoice, parse to decision. A replayed LLM reply makes it meaningless.
    seconds: float = 0.0
    # What step 10 did through gapura (empty without a gateway).
    actions: list[ActionTaken] = []


def process_invoice(
    path: Path,
    erp: ErpData,
    client: ChatClient,
    settings: Settings,
    retriever: ContractRetriever | None = None,
    investigate: bool = False,
    graph: CompiledStateGraph | None = None,
    thread_id: str | None = None,
) -> PipelineResult:
    """Without a retriever there is no contract check: a price mismatch then stays held.
    With `investigate`, an invoice routed to investigation is diagnosed by the agent loop;
    the client must then support tool calls. Without a graph, one is built with an
    in-memory checkpointer."""
    graph = graph or build_graph(
        erp, client, settings, memory_checkpointer(), retriever, investigate
    )
    thread_id = thread_id or f"{uuid.uuid4().hex[:8]}/{path.name}"
    started = time.monotonic()
    with span("process_invoice", {"app.invoice.file": path.name}) as root:
        graph.invoke({"path": str(path)}, config(thread_id))
        snapshot = graph.get_state(config(thread_id))
        state = InvoiceState.model_validate(snapshot.values)
        waiting = "approval" in snapshot.next
        result = PipelineResult(
            file=path.name,
            backend=state.backend,
            invoice=state.invoice,
            issues=state.reasons,
            decision=state.decision,
            contract_findings=state.findings,
            route=state.route,
            investigation=state.investigation,
            status="awaiting_approval"
            if waiting
            else ("resolved" if state.approval else "decided"),
            thread_id=thread_id,
            approval_request=ApprovalRequest.model_validate(snapshot.interrupts[0].value)
            if waiting and snapshot.interrupts
            else None,
            approval=state.approval,
            actions=state.actions,
            seconds=round(time.monotonic() - started, 2),
        )
        set_attributes(
            root,
            {
                "app.ingest.backend": result.backend,
                "app.decision": result.decision.value,
                "app.route": result.route.value if result.route else None,
                "app.status": result.status,
                "app.thread_id": thread_id,
                "app.issue_codes": codes(result.issues),
                # Set when no invoice came out of extraction: which of the two reasons.
                "app.extraction.failure": None if result.invoice else result.issues[0].code,
            },
        )
        return result


def process_invoices(
    paths: Iterable[Path],
    erp: ErpData,
    client: ChatClient,
    settings: Settings,
    workers: int = 4,
    retriever: ContractRetriever | None = None,
    investigate: bool = False,
    checkpointer=None,
    run_id: str | None = None,
    vision_client: ChatClient | None = None,
    documents: DocumentRecorder | None = None,
    gateway: Gateway | None = None,
) -> list[PipelineResult]:
    """Results come back in the order of `paths`. One graph for the batch; each invoice is
    the thread `<run_id>/<file name>`."""
    graph = build_graph(
        erp,
        client,
        settings,
        checkpointer or memory_checkpointer(),
        retriever,
        investigate,
        vision_client,
        documents,
        gateway,
    )
    run_id = run_id or uuid.uuid4().hex[:8]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(
            pool.map(
                lambda path: process_invoice(
                    path,
                    erp,
                    client,
                    settings,
                    graph=graph,
                    thread_id=f"{run_id}/{path.name}",
                ),
                paths,
            )
        )
