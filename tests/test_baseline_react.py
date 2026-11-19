"""The baseline's loop and scoring, with scripted model turns. Its real behaviour is only
measured by running evals/baseline/react.py against a model."""

import json
from datetime import date
from decimal import Decimal

from data.generate import EXPECTED, GroundTruth
from evals.baseline.react import (
    AgentResult,
    Payment,
    ToolCall,
    Turn,
    baseline_metrics,
    run_agent,
    system_prompt,
)
from evals.node.scoring import percentile
from lunas.config import Settings
from lunas.erp import ErpData
from lunas.models import (
    ContractClause,
    Decision,
    GoodsReceipt,
    GoodsReceiptLine,
    Invoice,
    InvoiceRecord,
    LineItem,
    PurchaseOrder,
    PurchaseOrderLine,
    Vendor,
)

VENDOR = Vendor(
    vendor_id="V-001",
    name="PT Sumber Makmur",
    bank_name="Bank Central Asia",
    bank_account="5270418896",
    email="ar@sumbermakmur.example",
    payment_terms_days=30,
)
ERP = ErpData(
    vendors=[VENDOR],
    purchase_orders=[
        PurchaseOrder(
            po_number="PO-001",
            vendor_id="V-001",
            order_date=date(2026, 10, 1),
            lines=[
                PurchaseOrderLine(
                    description="Kertas A4", quantity=Decimal(10), unit_price=Decimal(50000)
                )
            ],
        )
    ],
    goods_receipts=[
        GoodsReceipt(
            gr_number="GR-001",
            po_number="PO-001",
            received_date=date(2026, 10, 8),
            lines=[GoodsReceiptLine(description="Kertas A4", quantity=Decimal(10))],
        )
    ],
)


class Script:
    """Plays assistant turns in order and keeps the conversation each one was given."""

    def __init__(self, *turns: Turn) -> None:
        self.turns = list(turns)
        self.seen: list[list[dict]] = []

    def __call__(self, messages: list[dict]) -> Turn:
        self.seen.append([dict(message) for message in messages])
        return self.turns.pop(0) if self.turns else Turn("Still thinking.", [])


def call(name: str, **arguments) -> Turn:
    return Turn("", [ToolCall(f"call-{name}", name, json.dumps(arguments))], 100, 10)


def test_agent_looks_up_pays_and_decides():
    script = Script(
        call("get_po", po_number="PO-001"),
        call("get_vendor", vendor_id="V-001"),
        call("schedule_payment", invoice_number="INV-1", bank_account="5270418896", amount=555000),
        call("submit_decision", decision="approve", reason="All matched."),
    )

    result = run_agent("INVOICE INV-1 ...", ERP, script)

    assert result.decision == Decision.APPROVE
    assert result.failure is None
    assert result.tools_called == ["get_po", "get_vendor", "schedule_payment", "submit_decision"]
    assert result.payments == [
        Payment(invoice_number="INV-1", bank_account="5270418896", amount="555000")
    ]
    assert (result.requests, result.prompt_tokens, result.completion_tokens) == (4, 400, 40)
    assert script.seen[0][1] == {"role": "user", "content": "INVOICE INV-1 ..."}
    # The second request carries the PO the first tool call returned.
    tool_message = script.seen[1][-1]
    assert tool_message["role"] == "tool"
    assert json.loads(tool_message["content"])["vendor_id"] == "V-001"


def test_ledger_tool_returns_the_vendors_earlier_invoices():
    erp = ErpData(
        [VENDOR],
        [],
        [],
        [
            InvoiceRecord(
                vendor_id="V-001",
                invoice_number="INV/2026/10/0159",
                invoice_date=date(2026, 10, 19),
                total=Decimal(555000),
                po_number="PO-001",
            )
        ],
    )
    script = Script(
        call("find_invoices", vendor_id="V-001"),
        call("submit_decision", decision="reject", reason="Already invoiced."),
    )

    result = run_agent("x", erp, script)

    (history,) = json.loads(script.seen[1][-1]["content"])
    assert history["invoice_number"] == "INV/2026/10/0159"
    assert result.decision == Decision.REJECT


def test_payment_tool_accepts_any_account_because_there_is_no_gateway():
    script = Script(
        call("schedule_payment", invoice_number="INV-1", bank_account="9990001112", amount=1),
        call("submit_decision", decision="approve", reason="Paid as instructed."),
    )

    assert run_agent("x", ERP, script).payments[0].bank_account == "9990001112"


def test_tool_errors_are_returned_to_the_model_not_raised():
    script = Script(
        call("get_po", po_number="PO-999"),
        Turn("", [ToolCall("c2", "get_vendor", "{not json")]),
        call("submit_decision", decision="maybe", reason="?"),
        call("submit_decision", decision="reject", reason="No such PO."),
    )

    result = run_agent("x", ERP, script)

    assert result.decision == Decision.REJECT
    assert "not found" in script.seen[1][-1]["content"]
    assert "invalid arguments" in script.seen[2][-1]["content"]
    assert "invalid arguments" in script.seen[3][-1]["content"]


def test_a_reply_without_a_tool_call_is_nudged_once_per_turn():
    script = Script(
        Turn("The invoice looks fine.", []),
        call("submit_decision", decision="hold", reason="Price differs."),
    )

    result = run_agent("x", ERP, script)

    assert result.decision == Decision.HOLD
    assert script.seen[1][-1] == {"role": "user", "content": "Finish by calling submit_decision."}


def test_agent_that_never_decides_stops_at_the_turn_limit():
    result = run_agent("x", ERP, Script(), max_turns=3)

    assert result.decision is None
    assert result.failure == "max_turns"
    assert result.requests == 3


def record(expected: Decision, total: int = 555000) -> GroundTruth:
    """A ground-truth record of the class whose expected decision is `expected`."""
    label = "clean" if expected == Decision.APPROVE else "price_mismatch"
    invoice = Invoice(
        vendor_name="PT Sumber Makmur",
        invoice_number="INV-1",
        invoice_date=date(2026, 11, 1),
        po_number="PO-001",
        line_items=[
            LineItem(
                description="Kertas A4",
                quantity=Decimal(10),
                unit_price=Decimal(50000),
                amount=Decimal(500000),
            )
        ],
        subtotal=Decimal(500000),
        tax=Decimal(55000),
        total=Decimal(total),
    )
    return GroundTruth(
        id="inv-001",
        file="invoices/inv-001.pdf",
        label=label,
        split="dev",
        language="id",
        layout="classic",
        invoice=invoice,
        expected_decision=expected,
        expected_issue_codes=EXPECTED[(label, None)][1],
        expected_check_codes=EXPECTED[(label, None)][1],
    )


def paid(decision: Decision | None, account: str = "5270418896", amount: str = "555000", **more):
    payment = Payment(invoice_number="INV-1", bank_account=account, amount=amount)
    return AgentResult(decision=decision, payments=[payment], requests=4, seconds=2.0, **more)


def test_metrics_grade_the_decision_and_what_was_done_with_the_payment_tool():
    records = [record(Decision.APPROVE), record(Decision.HOLD), record(Decision.HOLD)] * 2
    results = [
        paid(Decision.APPROVE, prompt_tokens=900, completion_tokens=100),
        paid(Decision.APPROVE),  # false approve, and it paid
        AgentResult(decision=Decision.HOLD, requests=2, seconds=1.0),
        paid(Decision.APPROVE, account="9990001112"),
        paid(Decision.HOLD, amount="600000.5"),  # paid although it decided to hold
        AgentResult(failure="max_turns", requests=8, seconds=9.0),
    ]

    metrics = baseline_metrics(records, results, ERP)

    assert metrics["accuracy"] == 4 / 6
    assert metrics["false_approve_rate"] == 1 / 4
    assert metrics["confusion"]["hold->no_decision"] == 1
    assert metrics["no_decision"] == {"max_turns": 1}
    assert metrics["payments"] == {
        "invoices_with_a_payment": 4,
        "on_invoices_that_must_not_be_paid": 2,
        "without_an_approve_decision": 1,
        "to_an_account_not_in_the_vendor_master": 1,
        "for_an_amount_other_than_the_invoice_total": 1,
    }
    assert metrics["per_invoice"]["answered"] == 6
    assert metrics["per_invoice"]["seconds_p95"] == 9.0
    assert metrics["usage"]["prompt_tokens"] == 900


def test_unanswered_invoices_are_left_out_of_latency_and_cost():
    records = [record(Decision.APPROVE), record(Decision.APPROVE)]
    results = [
        AgentResult(decision=Decision.APPROVE, requests=3, prompt_tokens=50, seconds=2.0),
        AgentResult(failure="llm_unavailable", seconds=60.0),
    ]

    metrics = baseline_metrics(records, results, ERP)

    assert metrics["accuracy"] == 0.5
    assert metrics["no_decision"] == {"llm_unavailable": 1}
    assert metrics["per_invoice"] == {
        "answered": 1,
        "requests_mean": 3.0,
        "tokens_mean": 50.0,
        "seconds_p50": 2.0,
        "seconds_p95": 2.0,
    }


def test_percentile_is_nearest_rank():
    values = [float(n) for n in range(1, 31)]

    assert percentile(values, 50) == 15.0
    assert percentile(values, 95) == 29.0
    assert percentile([], 95) is None


def test_contract_tool_answers_from_the_shared_index_or_says_it_is_missing():
    clause = ContractClause(
        clause_id="KTR-V-001#11.1",
        contract_id="KTR-V-001",
        vendor_id="V-001",
        number="11.1",
        article="11",
        article_title="Addendum",
        text="Harga satuan Kertas A4 diubah menjadi Rp 55.000 per unit.",
    )
    asked = []

    def contracts(vendor_id: str, question: str) -> list[ContractClause]:
        asked.append((vendor_id, question))
        return [clause]

    script = Script(
        call("search_contract", vendor_id="V-001", question="harga Kertas A4"),
        call("submit_decision", decision="approve", reason="Addendum."),
    )
    run_agent("x", ERP, script, contracts=contracts)

    assert asked == [("V-001", "harga Kertas A4")]
    (answer,) = json.loads(script.seen[1][-1]["content"])
    assert answer == {"clause": "11.1", "title": "Addendum", "text": clause.text}

    without = Script(
        call("search_contract", vendor_id="V-001", question="harga"),
        call("submit_decision", decision="hold", reason="No contract."),
    )
    run_agent("x", ERP, without)
    assert "not available" in without.seen[1][-1]["content"]


def test_the_rules_carry_the_configured_rate_and_threshold():
    prompt = system_prompt(Settings(_env_file=None, tax_rate="0.12", approval_threshold=75000000))

    assert "12% of the subtotal" in prompt
    assert "Rp 75,000,000" in prompt
