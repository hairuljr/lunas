"""The workflow graph: who stops at the approval checkpoint, what they carry there, and
resuming them, in memory and from Postgres after a restart."""

import os
import uuid
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from pydantic import ValidationError

from data.generate import build_dataset, load_ground_truth, write_dataset
from lunas.config import Settings
from lunas.erp import ErpData
from lunas.models import Decision
from lunas.pipeline.graph import (
    Approval,
    build_graph,
    memory_checkpointer,
    pending_approvals,
    postgres_checkpointer,
    resume_approval,
)
from lunas.pipeline.nodes.route import Route
from lunas.pipeline.run import process_invoice, process_invoices
from tests.test_pipeline import OracleClient

SETTINGS = Settings(_env_file=None)
ANSWER = Approval(decision=Decision.APPROVE, approver="ana", note="Called the vendor.")


@pytest.fixture(scope="module")
def out(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("dataset")
    write_dataset(build_dataset(3, 16), path, 3)
    return path


def first(out: Path, label: str):
    return next(r for r in load_ground_truth(out) if r.label == label)


def test_a_clean_invoice_is_decided_without_stopping(out: Path):
    record = first(out, "clean")

    result = process_invoice(
        out / record.file, ErpData.load(out), OracleClient(load_ground_truth(out)), SETTINGS
    )

    assert (result.status, result.decision, result.approval_request) == (
        "decided",
        Decision.APPROVE,
        None,
    )


def test_a_changed_bank_account_stops_for_a_person_with_the_codes_view(out: Path):
    record = first(out, "bank_account_change")

    result = process_invoice(
        out / record.file, ErpData.load(out), OracleClient(load_ground_truth(out)), SETTINGS
    )

    assert result.status == "awaiting_approval"
    request = result.approval_request
    assert (request.route, request.decision) == (Route.HUMAN, Decision.ESCALATE)
    assert request.invoice_number == record.invoice.invoice_number
    assert any("bank account" in reason for reason in request.reasons)
    assert "transfer" not in str(request).lower()  # the invoice's own note is not shown


def test_a_clean_invoice_above_the_threshold_stops_too(out: Path):
    record = first(out, "clean")
    settings = Settings(_env_file=None, approval_threshold=Decimal(1))

    result = process_invoice(
        out / record.file, ErpData.load(out), OracleClient(load_ground_truth(out)), settings
    )

    assert (result.status, result.route, result.decision) == (
        "awaiting_approval",
        Route.CLEAN,
        Decision.ESCALATE,
    )


def test_resuming_records_the_persons_answer_and_keeps_the_codes_decision(out: Path):
    records = load_ground_truth(out)
    record = first(out, "bank_account_change")
    erp, saver = ErpData.load(out), memory_checkpointer()
    graph = build_graph(erp, OracleClient(records), SETTINGS, saver)
    result = process_invoice(
        out / record.file, erp, OracleClient(records), SETTINGS, graph=graph, thread_id="t1"
    )

    state = resume_approval(graph, result.thread_id, ANSWER)

    assert state.decision == Decision.ESCALATE
    assert state.approval == ANSWER
    assert pending_approvals(graph, saver) == {}
    with pytest.raises(ValueError, match="not waiting"):
        resume_approval(graph, "t1", ANSWER)


def test_a_person_cannot_answer_escalate():
    with pytest.raises(ValidationError):
        Approval(decision=Decision.ESCALATE, approver="ana")


def test_a_batch_runs_through_and_lists_what_waits(out: Path):
    records = load_ground_truth(out)
    erp, saver = ErpData.load(out), memory_checkpointer()

    results = process_invoices(
        [out / r.file for r in records],
        erp,
        OracleClient(records),
        SETTINGS,
        checkpointer=saver,
        run_id="batch",
    )

    waiting = {r.thread_id for r in results if r.status == "awaiting_approval"}
    assert waiting
    assert set(pending_approvals(build_graph(erp, None, SETTINGS, saver), saver)) == waiting


def database_url() -> str:
    url = SETTINGS.database_url
    try:
        psycopg.connect(url, connect_timeout=2).close()
    except psycopg.OperationalError:
        if os.environ.get("CI"):
            raise
        pytest.skip("Postgres is not running (podman compose up -d)")
    return url


def test_an_invoice_waiting_in_postgres_resumes_after_a_restart(out: Path):
    url = database_url()
    records = load_ground_truth(out)
    record = first(out, "bank_account_change")
    erp = ErpData.load(out)
    thread_id = f"test-{uuid.uuid4().hex[:8]}/{Path(record.file).name}"

    with postgres_checkpointer(url) as saver:
        graph = build_graph(erp, OracleClient(records), SETTINGS, saver)
        process_invoice(
            out / record.file,
            erp,
            OracleClient(records),
            SETTINGS,
            graph=graph,
            thread_id=thread_id,
        )

    try:
        # A new process: new pool, new graph, and no model at all.
        with postgres_checkpointer(url) as saver:
            graph = build_graph(erp, None, SETTINGS, saver)
            assert thread_id in pending_approvals(graph, saver)
            state = resume_approval(graph, thread_id, ANSWER)
            assert state.approval.approver == "ana"
            assert thread_id not in pending_approvals(graph, saver)
    finally:
        with psycopg.connect(url, autocommit=True) as connection:
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                connection.execute(f"DELETE FROM {table} WHERE thread_id = %s", (thread_id,))
