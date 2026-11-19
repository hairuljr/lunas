"""The eval command end to end, with the LLM replaced by a stand-in."""

import json
from pathlib import Path

import pytest

from data.generate import build_dataset, load_ground_truth, write_dataset
from evals.node import run as eval_run
from lunas.llm import JsonSchema, LLMRequestError, Message, RecordingClient
from lunas.models import Decision
from lunas.pipeline.nodes.extract import PROMPT_NAME, PROMPT_VERSION
from lunas.retrieval.retriever import RecordingRetriever

SEED = 5
INVOICES = 6


# Classes whose only issue comes from match_invoice (see EXPECTED in data/generate.py).
MATCH_ONLY_CLASSES = {
    "price_mismatch",
    "quantity_mismatch",
    "no_po",
    "bank_account_change",
    "payment_terms",
}


class NoClauses:
    """A contract index that finds nothing, so no contract question reaches the model."""

    def price_clauses(self, vendor_id: str, vendor_name: str, item: str) -> list:
        return []


def no_contract_index(settings, recordings=None, replay_only=False):
    if recordings is None:
        return NoClauses()
    return RecordingRetriever(recordings, None if replay_only else NoClauses())


class OracleClient:
    model = "oracle"
    host = "provider.example"

    def __init__(self, out: Path, unanswered: str | None = None) -> None:
        self.records = load_ground_truth(out)
        self.unanswered = unanswered
        self.usage = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def complete(self, messages: list[Message], schema: JsonSchema | None = None) -> str:
        document = messages[-1]["content"]
        (record,) = [r for r in self.records if r.invoice.invoice_number in document]
        if record.id == self.unanswered:
            raise LLMRequestError("RateLimitError: slow down")
        self.usage["requests"] += 1
        return record.invoice.model_dump_json()


@pytest.fixture
def out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "dataset"
    write_dataset(build_dataset(SEED, INVOICES), path, SEED)
    monkeypatch.setattr(eval_run, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(eval_run, "RECORDINGS_DIR", tmp_path / "recordings")
    return path


def run_eval(
    monkeypatch: pytest.MonkeyPatch, out: Path, client, *flags: str, expect: int = 0
) -> dict:
    monkeypatch.setattr(eval_run, "build_client", lambda *args: client)
    monkeypatch.setattr(eval_run, "build_retriever", no_contract_index)
    # Every invoice, so counts below are over the whole small dataset. The gate replays the
    # split it was recorded on.
    split = [] if "--gate" in flags else ["--split", "all", "--no-investigate"]
    monkeypatch.setattr("sys.argv", ["run", "--data", str(out), *split, *flags])
    for old in (out.parent / "runs").glob("node-*.json"):
        old.unlink()

    assert eval_run.main() == expect

    reports = list((out.parent / "runs").glob("node-*.json"))
    return json.loads(reports[0].read_text(encoding="utf-8")) if reports else {}


def test_report_carries_metrics_and_run_metadata(out: Path, monkeypatch, capsys):
    report = run_eval(monkeypatch, out, OracleClient(out))

    assert report["model"] == "oracle"
    assert report["provider"] == "provider.example"
    assert report["usage"]["requests"] == INVOICES
    assert report["replies"] is None
    assert report["extraction"]["key_field_accuracy"] == 1.0
    assert report["extraction"]["unanswered"] == 0
    assert report["decision_end_to_end"]["accuracy"] == 1.0
    assert report["misses"] == []
    assert "UNANSWERED" not in capsys.readouterr().out


def test_unanswered_invoices_are_counted_apart_from_model_errors(out: Path, monkeypatch, capsys):
    report = run_eval(monkeypatch, out, OracleClient(out, unanswered="inv-002"))

    assert report["extraction"]["unanswered"] == 1
    assert report["extraction"]["extraction_failures"] == 0
    assert report["extraction"]["documents_fully_correct"] == (INVOICES - 1) / INVOICES
    assert [miss["id"] for miss in report["misses"]] == ["inv-002"]
    assert "UNANSWERED            1" in capsys.readouterr().out


def test_rerun_with_cache_only_calls_the_model_for_what_is_missing(out: Path, monkeypatch):
    cache = out.parent / "cache"
    flaky = OracleClient(out, unanswered="inv-002")
    run_eval(monkeypatch, out, RecordingClient(cache, flaky), "--cache", str(cache))

    healthy = OracleClient(out)
    report = run_eval(monkeypatch, out, RecordingClient(cache, healthy), "--cache", str(cache))

    assert report["replies"] == {"live": 1, "replayed": INVOICES - 1}
    assert healthy.usage["requests"] == 1
    assert report["extraction"]["unanswered"] == 0
    assert report["extraction"]["documents_fully_correct"] == 1.0


def test_skip_llm_measures_only_the_deterministic_nodes(out: Path, monkeypatch, capsys):
    report = run_eval(monkeypatch, out, None, "--skip-llm")

    assert "extraction" not in report
    assert report["match_decide_on_ground_truth"]["accuracy"] == 1.0
    assert "not measured" in capsys.readouterr().out


def record(monkeypatch, out: Path, **oracle) -> Path:
    recordings = out.parent / "recordings"
    client = RecordingClient(recordings, OracleClient(out, **oracle))
    run_eval(monkeypatch, out, client, "--record", expect=1 if oracle else 0)
    return recordings


def test_gate_passes_on_fresh_recordings_without_any_client(out: Path, monkeypatch, capsys):
    recordings = record(monkeypatch, out)
    assert len(list(recordings.glob("*.json"))) == INVOICES + 1

    report = run_eval(monkeypatch, out, None, "--gate")

    assert report["replies"] == {"live": 0, "replayed": INVOICES}
    assert report["gate"]["current"] == report["gate"]["baseline"]
    assert "GATE PASSED" in capsys.readouterr().out


def test_record_prunes_recordings_no_longer_used(out: Path, monkeypatch):
    recordings = out.parent / "recordings"
    recordings.mkdir()
    (recordings / "stale.json").write_text("{}", encoding="utf-8")

    record(monkeypatch, out)

    assert not (recordings / "stale.json").exists()
    assert len(list(recordings.glob("*.json"))) == INVOICES + 1


def test_incomplete_run_is_not_recorded_as_a_baseline(out: Path, monkeypatch, capsys):
    recordings = record(monkeypatch, out, unanswered="inv-002")

    assert not (recordings / "recording.json").exists()
    assert "NOT RECORDED" in capsys.readouterr().out


def test_gate_fails_without_recordings(out: Path, monkeypatch, capsys):
    run_eval(monkeypatch, out, None, "--gate", expect=1)

    assert "Create them with --record" in capsys.readouterr().out


def test_gate_fails_when_the_prompt_changed_since_recording(out: Path, monkeypatch, capsys):
    record(monkeypatch, out)
    monkeypatch.setattr(eval_run, "load_prompt", lambda name, version: "A reworded prompt.")

    run_eval(monkeypatch, out, None, "--gate", expect=1)

    output = capsys.readouterr().out
    assert "GATE FAILED: the recordings are stale" in output
    assert f"{PROMPT_NAME}.{PROMPT_VERSION}.md changed" in output


def test_gate_fails_when_a_recording_is_missing(out: Path, monkeypatch, capsys):
    recordings = record(monkeypatch, out)
    next(path for path in recordings.glob("*.json") if path.name != "recording.json").unlink()

    run_eval(monkeypatch, out, None, "--gate", expect=1)

    assert "GATE FAILED: No recording" in capsys.readouterr().out


def test_gate_fails_when_recorded_replies_now_lead_to_worse_decisions(
    out: Path, monkeypatch, capsys
):
    """A code regression: the same model replies, but the pipeline now mishandles them."""
    record(monkeypatch, out)
    monkeypatch.setattr("lunas.pipeline.run.match_invoice", lambda invoice, erp, tolerance: [])

    report = run_eval(monkeypatch, out, None, "--gate", expect=1)

    # Without matching, only the classes that matching alone catches slip through to approve.
    records = load_ground_truth(out)
    must_not_pay = [r for r in records if r.expected_decision != Decision.APPROVE]
    caught_only_by_match = [r for r in must_not_pay if r.label in MATCH_ONLY_CLASSES]
    assert caught_only_by_match
    assert report["gate"]["current"]["false_approve_rate"] == len(caught_only_by_match) / len(
        must_not_pay
    )
    output = capsys.readouterr().out
    assert "GATE FAILED" in output
    assert "false_approve_rate rose" in output


def test_gate_thresholds():
    baseline = {"key_field_accuracy": 1.0, "decision_accuracy": 1.0, "false_approve_rate": 0.0}

    assert eval_run.gate_failures(baseline, baseline) == []
    assert eval_run.gate_failures(baseline, baseline | {"decision_accuracy": 0.98}) == []
    assert len(eval_run.gate_failures(baseline, baseline | {"decision_accuracy": 0.96})) == 1
    assert len(eval_run.gate_failures(baseline, baseline | {"key_field_accuracy": 0.9})) == 1
    assert len(eval_run.gate_failures(baseline, baseline | {"false_approve_rate": 0.1})) == 1
