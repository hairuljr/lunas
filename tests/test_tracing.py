"""The span tree and its attributes, read back from an in-memory exporter."""

import json
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from data.generate import build_dataset, load_ground_truth, write_dataset
from lunas.config import Settings
from lunas.erp import ErpData
from lunas.llm import JsonSchema, LLMRequestError, Message, OpenAICompatClient, RecordingClient
from lunas.pipeline.nodes.extract import PROMPT_VERSION
from lunas.pipeline.run import process_invoice, process_invoices
from lunas.telemetry import configure_tracing, shutdown_tracing
from tests.test_llm import fake_response

SETTINGS = Settings(_env_file=None)


@pytest.fixture
def spans() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    configure_tracing(SETTINGS, [exporter])
    yield exporter
    shutdown_tracing()


@pytest.fixture(scope="module")
def out(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("dataset")
    write_dataset(build_dataset(3, 3), path, 3)
    return path


def live_client(out: Path, monkeypatch, replies: list[str] | None = None) -> OpenAICompatClient:
    """A real client whose HTTP call is replaced: replies with the ground truth, or with
    `replies` in order."""
    records = load_ground_truth(out)
    client = OpenAICompatClient("https://provider.example/v1", "k", "some-model")

    def create(**request):
        if replies is not None:
            return fake_response(replies.pop(0))
        document = request["messages"][-1]["content"]
        (record,) = [r for r in records if r.invoice.invoice_number in document]
        return fake_response(record.invoice.model_dump_json())

    monkeypatch.setattr(client._client.chat.completions, "create", create)
    return client


def tree(finished: tuple[ReadableSpan, ...]) -> dict[str, list[str]]:
    """Parent span name -> child span names, in start order."""
    by_id = {span.context.span_id: span for span in finished}
    children: dict[str, list[str]] = {}
    for span in sorted(finished, key=lambda span: span.start_time):
        parent = by_id[span.parent.span_id].name if span.parent else "(root)"
        children.setdefault(parent, []).append(span.name)
    return children


def named(finished: tuple[ReadableSpan, ...], name: str) -> ReadableSpan:
    (span,) = [span for span in finished if span.name == name]
    return span


def test_one_invoice_is_one_trace_with_a_span_per_node_and_per_model_call(
    out: Path, monkeypatch, spans: InMemorySpanExporter
):
    record = load_ground_truth(out)[0]

    process_invoice(out / record.file, ErpData.load(out), live_client(out, monkeypatch), SETTINGS)

    finished = spans.get_finished_spans()
    assert len({span.context.trace_id for span in finished}) == 1
    assert tree(finished) == {
        "(root)": ["process_invoice"],
        "process_invoice": [
            "parse",
            "extract",
            "validate",
            "duplicates",
            "match",
            "route",
            "decide",
        ],
        "extract": ["chat some-model"],
    }
    root = named(finished, "process_invoice")
    assert root.attributes["app.invoice.file"] == "inv-001.pdf"
    assert root.attributes["app.ingest.backend"] == "pdf_text"
    assert root.attributes["app.decision"] == record.expected_decision.value
    assert list(root.attributes["app.issue_codes"]) == record.expected_issue_codes
    assert "app.extraction.failure" not in root.attributes
    assert named(finished, "extract").attributes["app.prompt.version"] == PROMPT_VERSION
    assert named(finished, "match").attributes["app.po_number"] == record.invoice.po_number
    assert root.resource.attributes["service.name"] == "lunas"


def test_chat_span_carries_model_provider_and_token_usage_but_no_content(
    out: Path, monkeypatch, spans: InMemorySpanExporter
):
    record = load_ground_truth(out)[0]

    process_invoice(out / record.file, ErpData.load(out), live_client(out, monkeypatch), SETTINGS)

    chat = named(spans.get_finished_spans(), "chat some-model")
    assert chat.kind == SpanKind.CLIENT
    assert dict(chat.attributes) == {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "provider.example",
        "server.address": "provider.example",
        "gen_ai.request.model": "some-model",
        "gen_ai.output.type": "json",
        "app.llm.structured_output": "json_schema",
        "gen_ai.response.model": "some-model-0101",
        "gen_ai.response.id": "resp-1",
        "gen_ai.response.finish_reasons": ("stop",),
        "gen_ai.usage.input_tokens": 7,
        "gen_ai.usage.output_tokens": 3,
    }


def test_content_is_captured_only_when_asked_for(out: Path, monkeypatch):
    record = load_ground_truth(out)[0]
    exporter = InMemorySpanExporter()
    configure_tracing(Settings(_env_file=None, trace_content=True), [exporter])
    try:
        client = live_client(out, monkeypatch)
        process_invoice(out / record.file, ErpData.load(out), client, SETTINGS)
    finally:
        shutdown_tracing()

    chat = named(exporter.get_finished_spans(), "chat some-model")
    sent = json.loads(chat.attributes["gen_ai.input.messages"])
    assert [message["role"] for message in sent] == ["system", "user"]
    assert sent[1]["parts"][0]["type"] == "text"
    assert record.invoice.invoice_number in sent[1]["parts"][0]["content"]
    (reply,) = json.loads(chat.attributes["gen_ai.output.messages"])
    assert (reply["role"], reply["finish_reason"]) == ("assistant", "stop")
    assert record.invoice.invoice_number in reply["parts"][0]["content"]


def test_the_corrective_retry_is_a_second_chat_span(
    out: Path, monkeypatch, spans: InMemorySpanExporter
):
    record = load_ground_truth(out)[0]
    client = live_client(out, monkeypatch, ["not json", record.invoice.model_dump_json()])

    result = process_invoice(out / record.file, ErpData.load(out), client, SETTINGS)

    assert result.invoice == record.invoice
    assert tree(spans.get_finished_spans())["extract"] == ["chat some-model", "chat some-model"]


def test_failed_extraction_is_an_error_on_the_extract_span_and_named_on_the_root(
    out: Path, monkeypatch, spans: InMemorySpanExporter
):
    record = load_ground_truth(out)[0]
    client = live_client(out, monkeypatch, ["not json", "still not json"])

    process_invoice(out / record.file, ErpData.load(out), client, SETTINGS)

    finished = spans.get_finished_spans()
    extract = named(finished, "extract")
    assert extract.status.status_code == StatusCode.ERROR
    assert extract.attributes["error.type"] == "StructuredOutputError"
    root = named(finished, "process_invoice")
    assert root.attributes["app.extraction.failure"] == "extraction_failed"
    assert root.attributes["app.decision"] == "hold"
    # The pipeline handled it, so the invoice itself did not fail.
    assert root.status.status_code == StatusCode.UNSET
    # An unreadable invoice goes to a person: route, then the approval checkpoint.
    assert tree(finished)["process_invoice"] == ["parse", "extract", "route", "decide", "approval"]
    assert root.attributes["app.status"] == "awaiting_approval"


def test_unanswered_request_is_an_error_on_the_chat_span(out: Path, spans: InMemorySpanExporter):
    class DownClient:
        model = "some-model"

        def complete(self, messages: list[Message], schema: JsonSchema | None = None) -> str:
            raise LLMRequestError("RateLimitError: slow down")

    record = load_ground_truth(out)[0]

    process_invoice(out / record.file, ErpData.load(out), DownClient(), SETTINGS)

    finished = spans.get_finished_spans()
    assert named(finished, "extract").attributes["error.type"] == "LLMRequestError"
    assert named(finished, "process_invoice").attributes["app.extraction.failure"] == (
        "llm_unavailable"
    )


def test_parallel_invoices_get_separate_traces(out: Path, monkeypatch, spans: InMemorySpanExporter):
    records = load_ground_truth(out)
    paths = [out / record.file for record in records]

    process_invoices(paths, ErpData.load(out), live_client(out, monkeypatch), SETTINGS, workers=3)

    finished = spans.get_finished_spans()
    roots = [span for span in finished if span.name == "process_invoice"]
    assert len({span.context.trace_id for span in roots}) == len(records)
    for root in roots:
        in_trace = [s.name for s in finished if s.context.trace_id == root.context.trace_id]
        assert sorted(in_trace) == sorted(
            [
                "process_invoice",
                "parse",
                "extract",
                "chat some-model",
                "validate",
                "duplicates",
                "match",
                "route",
                "decide",
            ]
        )


def test_replayed_reply_is_marked_and_has_no_chat_span(
    out: Path, monkeypatch, spans: InMemorySpanExporter, tmp_path: Path
):
    record = load_ground_truth(out)[0]
    erp = ErpData.load(out)
    client = RecordingClient(tmp_path, live_client(out, monkeypatch))
    process_invoice(out / record.file, erp, client, SETTINGS)
    spans.clear()

    process_invoice(out / record.file, erp, client, SETTINGS)

    finished = spans.get_finished_spans()
    assert "extract" not in tree(finished)
    assert named(finished, "extract").attributes["app.llm.replayed"] is True


def test_tracing_is_off_without_an_exporter(out: Path, monkeypatch):
    assert configure_tracing(SETTINGS) is False

    record = load_ground_truth(out)[0]
    result = process_invoice(
        out / record.file, ErpData.load(out), live_client(out, monkeypatch), SETTINGS
    )

    assert result.decision == record.expected_decision


def test_trace_file_gets_one_json_span_per_line(out: Path, monkeypatch, tmp_path: Path):
    trace_file = tmp_path / "traces" / "run.jsonl"
    configure_tracing(Settings(_env_file=None, trace_file=trace_file))
    record = load_ground_truth(out)[0]
    process_invoice(out / record.file, ErpData.load(out), live_client(out, monkeypatch), SETTINGS)
    shutdown_tracing()

    lines = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines()]

    assert sorted(line["name"] for line in lines) == sorted(
        [
            "process_invoice",
            "parse",
            "extract",
            "chat some-model",
            "validate",
            "duplicates",
            "match",
            "route",
            "decide",
        ]
    )
    chat = next(line for line in lines if line["name"] == "chat some-model")
    assert chat["attributes"]["gen_ai.usage.input_tokens"] == 7
