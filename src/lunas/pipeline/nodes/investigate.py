"""Step 8, the exception investigation: the only agent loop in lunas (SPEC §4).

The investigator diagnoses; it never decides. An invoice it looks at stays on hold until the
vendor corrects it or a person acts (SPEC §4, decision log 2026-11-19). Its output is:

- a cause per issue, from a fixed taxonomy, so its accuracy can be measured (SPEC §2:
  exception reason accuracy >= 90%);
- evidence: ids of records it fetched with its read-only tools, which code checks exist;
- an action, which code derives from the cause. The model does not choose it.

It is given typed fields and issues only, never the invoice text (SPEC §6).
"""

import json
import time
from collections.abc import Callable
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from lunas.config import DEFAULT_TAX_RATE
from lunas.erp import ErpData
from lunas.gapura.gateway import Status
from lunas.llm import LLMRequestError, Message, ToolChatClient, load_prompt
from lunas.models import (
    ContractClause,
    GoodsReceipt,
    Invoice,
    InvoiceRecord,
    Issue,
    PurchaseOrder,
    Vendor,
)
from lunas.telemetry import set_attributes, span
from lunas.text import normalize

if TYPE_CHECKING:
    from lunas.pipeline.nodes.contract import ContractFinding

# (vendor_id, question) -> the vendor's contract articles that answer it.
ContractSearch = Callable[[str, str], list[ContractClause]]


class ExceptionCause(StrEnum):
    OVERCHARGE = "overcharge"  # unit price above the PO, and the contract does not allow it
    CONTRACT_PRICE_NOT_YET_EFFECTIVE = "contract_price_not_yet_effective"  # billed too early
    PO_AMBIGUOUS = "po_ambiguous"  # the PO lists the item at more than one price
    SHORT_DELIVERY = "short_delivery"  # billed more than has been received so far
    OVER_BILLING = "over_billing"  # billed more than was ordered
    ITEM_NOT_ORDERED = "item_not_ordered"  # a line that is not on the PO
    RESUBMISSION = "resubmission"  # an invoice already paid, sent again under a new number
    SEPARATE_DELIVERY = "separate_delivery"  # same amount as an earlier invoice, but legitimate
    WRONG_TAX_RATE = "wrong_tax_rate"
    PAYMENT_TERMS_SHORTER = "payment_terms_shorter"  # asks to be paid sooner than agreed
    ARITHMETIC_ERROR = "arithmetic_error"  # line amounts, subtotal or total do not add up
    INVOICE_DATE_ERROR = "invoice_date_error"  # due date before the invoice date
    UNCLEAR = "unclear"  # the evidence does not settle it


class Action(StrEnum):
    REQUEST_CREDIT_NOTE = "request_credit_note"
    REQUEST_CORRECTED_INVOICE = "request_corrected_invoice"
    WAIT_FOR_GOODS_RECEIPT = "wait_for_goods_receipt"
    REJECT_AS_DUPLICATE = "reject_as_duplicate"  # a recommendation to the person; still held
    CONFIRM_SEPARATE_DELIVERY = "confirm_separate_delivery"
    PAY_ON_AGREED_TERMS = "pay_on_agreed_terms"
    FIX_PURCHASE_ORDER = "fix_purchase_order"
    MANUAL_REVIEW = "manual_review"


# The causes a diagnosis of each issue may choose from. Anything else is rejected by code.
POSSIBLE_CAUSES: dict[str, tuple[ExceptionCause, ...]] = {
    "price_mismatch": (
        ExceptionCause.OVERCHARGE,
        ExceptionCause.CONTRACT_PRICE_NOT_YET_EFFECTIVE,
    ),
    "po_price_ambiguous": (ExceptionCause.PO_AMBIGUOUS,),
    "quantity_exceeds_received": (ExceptionCause.SHORT_DELIVERY,),
    "quantity_exceeds_po": (ExceptionCause.OVER_BILLING,),
    "line_not_on_po": (ExceptionCause.ITEM_NOT_ORDERED,),
    "possible_duplicate": (ExceptionCause.RESUBMISSION, ExceptionCause.SEPARATE_DELIVERY),
    "tax_mismatch": (ExceptionCause.WRONG_TAX_RATE,),
    "payment_terms_mismatch": (ExceptionCause.PAYMENT_TERMS_SHORTER,),
    "line_amount_mismatch": (ExceptionCause.ARITHMETIC_ERROR,),
    "subtotal_mismatch": (ExceptionCause.ARITHMETIC_ERROR,),
    "total_mismatch": (ExceptionCause.ARITHMETIC_ERROR,),
    "due_before_invoice_date": (ExceptionCause.INVOICE_DATE_ERROR,),
}

# The action follows from the cause. Code, so the model's freedom ends at the diagnosis.
RECOMMENDED_ACTION: dict[ExceptionCause, Action] = {
    ExceptionCause.OVERCHARGE: Action.REQUEST_CREDIT_NOTE,
    ExceptionCause.CONTRACT_PRICE_NOT_YET_EFFECTIVE: Action.REQUEST_CORRECTED_INVOICE,
    ExceptionCause.PO_AMBIGUOUS: Action.FIX_PURCHASE_ORDER,
    ExceptionCause.SHORT_DELIVERY: Action.WAIT_FOR_GOODS_RECEIPT,
    ExceptionCause.OVER_BILLING: Action.REQUEST_CREDIT_NOTE,
    ExceptionCause.ITEM_NOT_ORDERED: Action.REQUEST_CORRECTED_INVOICE,
    ExceptionCause.RESUBMISSION: Action.REJECT_AS_DUPLICATE,
    ExceptionCause.SEPARATE_DELIVERY: Action.CONFIRM_SEPARATE_DELIVERY,
    ExceptionCause.WRONG_TAX_RATE: Action.REQUEST_CORRECTED_INVOICE,
    ExceptionCause.PAYMENT_TERMS_SHORTER: Action.PAY_ON_AGREED_TERMS,
    ExceptionCause.ARITHMETIC_ERROR: Action.REQUEST_CORRECTED_INVOICE,
    ExceptionCause.INVOICE_DATE_ERROR: Action.REQUEST_CORRECTED_INVOICE,
    ExceptionCause.UNCLEAR: Action.MANUAL_REVIEW,
}


class EvidenceKind(StrEnum):
    INVOICE = "invoice"  # the invoice under investigation, by its number
    PURCHASE_ORDER = "purchase_order"
    GOODS_RECEIPT = "goods_receipt"
    LEDGER_INVOICE = "ledger_invoice"
    CONTRACT_CLAUSE = "contract_clause"
    VENDOR = "vendor"


class Evidence(BaseModel):
    kind: EvidenceKind
    # The record's id: a PO number, GR number, ledger invoice number, clause id or vendor id.
    # Code checks it is one the investigator actually fetched.
    ref: str
    # What in that record supports the cause, in one sentence.
    detail: str


class Diagnosis(BaseModel):
    issue_code: str
    line: int | None = None
    cause: ExceptionCause
    evidence: list[Evidence]
    explanation: str
    # Set by code, not the model: whether every cited record was one the tools returned,
    # and the cause the model gave if code rejected it as not possible for this issue.
    evidence_valid: bool = False
    rejected_cause: str | None = None


class InvestigationReport(BaseModel):
    diagnoses: list[Diagnosis]
    turns: int
    # False when the turn limit ran out, or the endpoint stopped answering, before a report.
    finished: bool
    # Why the loop ended: a report, the turn limit, or no answer from the endpoint. The last
    # says nothing about the model and is kept apart from its accuracy.
    stopped: Literal["report", "turn_limit", "llm_unavailable"] = "report"
    error: str | None = None
    tool_calls: list[str] = []
    seconds: float = 0.0

    def actions(self) -> list[Action]:
        return [RECOMMENDED_ACTION[d.cause] for d in self.diagnoses]


def allowed_cause(diagnosis: Diagnosis) -> bool:
    """A cause outside the issue's possible causes is not a diagnosis of that issue."""
    return diagnosis.cause == ExceptionCause.UNCLEAR or diagnosis.cause in POSSIBLE_CAUSES.get(
        diagnosis.issue_code, ()
    )


# --- the loop ---------------------------------------------------------------------------

PROMPT_NAME = "investigate"
PROMPT_VERSION = "v2"
MAX_TURNS = 6


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    parameters = {"type": "object", "properties": properties, "required": required}
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


_PO = {"po_number": {"type": "string"}}
TOOLS = [
    _tool(
        "get_purchase_order",
        "A purchase order of this vendor: lines, quantities, prices.",
        _PO,
        ["po_number"],
    ),
    _tool(
        "get_goods_receipts",
        "The goods receipts recorded against a purchase order.",
        _PO,
        ["po_number"],
    ),
    _tool(
        "get_billing_position",
        "For a purchase order: value received (all goods receipts, at PO prices, with tax) "
        "against everything invoiced on it so far, this invoice included. Computed by code.",
        _PO,
        ["po_number"],
    ),
    _tool(
        "get_vendor_invoices",
        "This vendor's earlier invoices in our ledger: number, date, total, purchase order.",
        {},
        [],
    ),
    _tool("get_vendor_terms", "This vendor's agreed payment terms in days.", {}, []),
    _tool(
        "search_contract",
        "Search this vendor's signed contract; returns whole articles with clause ids.",
        {"question": {"type": "string"}},
        ["question"],
    ),
    _tool(
        "submit_report",
        "Submit the diagnosis of every issue. Call once, last.",
        {
            "diagnoses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "issue_code": {"type": "string"},
                        "line": {"type": ["integer", "null"]},
                        "cause": {"type": "string", "enum": [c.value for c in ExceptionCause]},
                        "evidence": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "kind": {
                                        "type": "string",
                                        "enum": [k.value for k in EvidenceKind],
                                    },
                                    "ref": {"type": "string"},
                                    "detail": {"type": "string"},
                                },
                                "required": ["kind", "ref", "detail"],
                            },
                        },
                        "explanation": {"type": "string"},
                    },
                    "required": ["issue_code", "line", "cause", "evidence", "explanation"],
                },
            }
        },
        ["diagnoses"],
    ),
]  # fmt: skip


class _Submitted(BaseModel):
    diagnoses: list[Diagnosis]


def billing_position(
    po: PurchaseOrder,
    receipts: list[GoodsReceipt],
    ledger: list[InvoiceRecord],
    invoice: Invoice,
    tax_rate: Decimal,
) -> dict[str, Any]:
    """What has been received on a PO against what has been invoiced on it, this invoice
    included. Arithmetic is code (design rule), so the investigator reads this, not sums."""
    price: dict[str, Decimal] = {}
    ordered: dict[str, Decimal] = {}
    received: dict[str, Decimal] = {}
    names: dict[str, str] = {}
    for line in po.lines:
        key = normalize(line.description)
        price.setdefault(key, line.unit_price)
        names.setdefault(key, line.description)
        ordered[key] = ordered.get(key, Decimal(0)) + line.quantity
    for receipt in receipts:
        for line in receipt.lines:
            key = normalize(line.description)
            received[key] = received.get(key, Decimal(0)) + line.quantity
    value = sum((received.get(k, Decimal(0)) * price[k] for k in price), Decimal(0))
    value_with_tax = (value * (1 + tax_rate)).quantize(Decimal(1))
    before = sum((record.total for record in ledger), Decimal(0))
    including = before + invoice.total
    # Each invoice's tax may be rounded by up to one unit.
    slack = Decimal(len(ledger) + 1)
    return {
        "po_number": po.po_number,
        "items": [
            {
                "description": names[k],
                "ordered": str(ordered[k]),
                "received": str(received.get(k, Decimal(0))),
                "unit_price": str(price[k]),
            }
            for k in price
        ],
        "goods_receipts": [receipt.gr_number for receipt in receipts],
        "value_received_with_tax": str(value_with_tax),
        "invoiced_before": [
            {"invoice_number": r.invoice_number, "total": str(r.total)} for r in ledger
        ],
        "this_invoice_total": str(invoice.total),
        "invoiced_including_this": str(including),
        "within_received": including <= value_with_tax + slack,
    }


# Each investigation tool, as the gapura tools whose records it reads (role
# `investigator`). An unmapped name is authorized as itself and so refused by default deny.
GAPURA_TOOLS = {
    "get_purchase_order": ("erp.get_po",),
    "get_goods_receipts": ("erp.get_goods_receipts",),
    "get_billing_position": ("erp.get_po", "erp.get_goods_receipts", "erp.find_invoices"),
    "get_vendor_invoices": ("erp.find_invoices",),
    "get_vendor_terms": ("erp.get_vendor",),
    "search_contract": ("contracts.search",),
}


class _Session:
    """The tools, scoped to one vendor, and every record id they returned."""

    def __init__(
        self,
        erp: ErpData,
        vendor: Vendor,
        contracts: ContractSearch | None,
        invoice: Invoice,
        tax_rate: Decimal,
        gateway=None,
    ) -> None:
        self.erp = erp
        # Behind gapura, each call is authorized first as role `investigator`.
        self.gateway = gateway
        self.gate_session = gateway.session("investigator") if gateway else None
        self.vendor = vendor
        self.contracts = contracts
        self.invoice = invoice
        self.tax_rate = tax_rate
        self.fetched: dict[EvidenceKind, set[str]] = {kind: set() for kind in EvidenceKind}
        # Given in the brief, so citable without a tool call.
        self.fetched[EvidenceKind.VENDOR].add(vendor.vendor_id)

    def call(self, name: str, arguments: str) -> str:
        try:
            args = json.loads(arguments or "{}")
            if self.gateway is not None:
                for tool in GAPURA_TOOLS.get(name, (f"investigate.{name}",)):
                    verdict = self.gateway.authorize(self.gate_session, tool, args)
                    if verdict.status != Status.ALLOWED:
                        return verdict.for_model()
            if name == "get_purchase_order":
                po = self.erp.get_po(str(args["po_number"]))
                if po is None or po.vendor_id != self.vendor.vendor_id:
                    return _error("no such purchase order for this vendor")
                self.fetched[EvidenceKind.PURCHASE_ORDER].add(po.po_number)
                return po.model_dump_json()
            if name == "get_goods_receipts":
                po = self.erp.get_po(str(args["po_number"]))
                if po is None or po.vendor_id != self.vendor.vendor_id:
                    return _error("no such purchase order for this vendor")
                receipts = self.erp.get_goods_receipts(po.po_number)
                self.fetched[EvidenceKind.GOODS_RECEIPT] |= {r.gr_number for r in receipts}
                return json.dumps([r.model_dump(mode="json") for r in receipts])
            if name == "get_billing_position":
                po = self.erp.get_po(str(args["po_number"]))
                if po is None or po.vendor_id != self.vendor.vendor_id:
                    return _error("no such purchase order for this vendor")
                receipts = self.erp.get_goods_receipts(po.po_number)
                ledger = [
                    record
                    for record in self.erp.find_invoices(self.vendor.vendor_id)
                    if record.po_number == po.po_number
                ]
                self.fetched[EvidenceKind.PURCHASE_ORDER].add(po.po_number)
                self.fetched[EvidenceKind.GOODS_RECEIPT] |= {r.gr_number for r in receipts}
                self.fetched[EvidenceKind.LEDGER_INVOICE] |= {r.invoice_number for r in ledger}
                return json.dumps(
                    billing_position(po, receipts, ledger, self.invoice, self.tax_rate)
                )
            if name == "get_vendor_invoices":
                history = self.erp.find_invoices(self.vendor.vendor_id)
                self.fetched[EvidenceKind.LEDGER_INVOICE] |= {r.invoice_number for r in history}
                return json.dumps([r.model_dump(mode="json") for r in history])
            if name == "get_vendor_terms":
                # The vendor record without its bank details: the agent never sees them.
                self.fetched[EvidenceKind.VENDOR].add(self.vendor.vendor_id)
                return json.dumps(
                    {
                        "vendor_id": self.vendor.vendor_id,
                        "name": self.vendor.name,
                        "payment_terms_days": self.vendor.payment_terms_days,
                    }
                )
            if name == "search_contract":
                if self.contracts is None:
                    return _error("contract search is not available")
                clauses = self.contracts(self.vendor.vendor_id, str(args["question"]))
                self.fetched[EvidenceKind.CONTRACT_CLAUSE] |= {c.clause_id for c in clauses}
                return json.dumps(
                    [{"clause_id": c.clause_id, "text": c.text} for c in clauses],
                    ensure_ascii=False,
                )
            return _error(f"unknown tool {name}")
        except (KeyError, TypeError, ValueError) as error:
            return _error(f"invalid arguments: {error}")


def _error(message: str) -> str:
    return json.dumps({"error": message})


def build_brief(
    invoice: Invoice, issues: list[Issue], vendor: Vendor, findings: list[ContractFinding]
) -> dict[str, Any]:
    """Typed fields and issues only: no invoice text, notes or bank account (SPEC §6)."""
    return {
        "invoice": {
            "invoice_number": invoice.invoice_number,
            "invoice_date": invoice.invoice_date.isoformat(),
            "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
            "payment_terms_days": invoice.payment_terms_days,
            "po_number": invoice.po_number,
            "lines": [
                {
                    "line": number,
                    "description (vendor-written)": item.description,
                    "quantity": str(item.quantity),
                    "unit_price": str(item.unit_price),
                    "amount": str(item.amount),
                }
                for number, item in enumerate(invoice.line_items, start=1)
            ],
            "subtotal": str(invoice.subtotal),
            "tax": str(invoice.tax),
            "total": str(invoice.total),
        },
        "vendor": {"vendor_id": vendor.vendor_id, "name": vendor.name},
        "issues": [
            {
                "issue_code": issue.code,
                "line": issue.line,
                "message": issue.message,
                "possible_causes": [c.value for c in POSSIBLE_CAUSES[issue.code]]
                + [ExceptionCause.UNCLEAR.value],
            }
            for issue in issues
        ],
        "contract_check": [
            {
                "line": f.line,
                "verdict": f.verdict,
                "clause_id": f.clause_id,
                "contract_price": str(f.contract_price) if f.contract_price else None,
                "effective_date": f.effective_date.isoformat() if f.effective_date else None,
            }
            for f in findings
        ],
    }


def finalize(
    submitted: list[Diagnosis], issues: list[Issue], fetched: dict[EvidenceKind, set[str]]
) -> list[Diagnosis]:
    """One diagnosis per issue, in issue order. Code checks each one: a cause outside the
    issue's possible causes becomes `unclear`, and evidence is valid only if every record it
    names was returned by a tool in this investigation."""
    final = []
    for issue in issues:
        # An issue about one line is matched by its line; one about the invoice or an item
        # as a whole (quantities, duplicates, terms) by its code alone.
        given = next(
            (
                d
                for d in submitted
                if d.issue_code == issue.code and (issue.line is None or d.line == issue.line)
            ),
            None,
        )
        if given is None:
            final.append(
                Diagnosis(
                    issue_code=issue.code,
                    line=issue.line,
                    cause=ExceptionCause.UNCLEAR,
                    evidence=[],
                    explanation="Not diagnosed.",
                )
            )
            continue
        rejected = None if allowed_cause(given) else given.cause.value
        final.append(
            given.model_copy(
                update={
                    "cause": ExceptionCause.UNCLEAR if rejected else given.cause,
                    "rejected_cause": rejected,
                    "evidence_valid": bool(given.evidence)
                    and all(e.ref in fetched[e.kind] for e in given.evidence),
                }
            )
        )
    return final


def investigate(
    invoice: Invoice,
    issues: list[Issue],
    erp: ErpData,
    client: ToolChatClient,
    contracts: ContractSearch | None = None,
    findings: list[ContractFinding] | None = None,
    max_turns: int = MAX_TURNS,
    tax_rate: Decimal = DEFAULT_TAX_RATE,
    gateway=None,
) -> InvestigationReport | None:
    """Diagnose the investigable issues. Returns None if there is no PO or vendor to work
    from; never changes the decision."""
    issues = [issue for issue in issues if issue.code in POSSIBLE_CAUSES]
    po = erp.get_po(invoice.po_number) if invoice.po_number else None
    vendor = erp.get_vendor(po.vendor_id) if po else None
    if not issues or vendor is None:
        return None

    session = _Session(erp, vendor, contracts, invoice, tax_rate, gateway)
    session.fetched[EvidenceKind.INVOICE].add(invoice.invoice_number)
    brief = build_brief(invoice, issues, vendor, findings or [])
    messages: list[Message] = [
        {"role": "system", "content": load_prompt(PROMPT_NAME, PROMPT_VERSION)},
        {"role": "user", "content": json.dumps(brief, ensure_ascii=False, indent=1)},
    ]
    started = time.monotonic()
    called: list[str] = []
    submitted: list[Diagnosis] | None = None
    unavailable: str | None = None
    turns = 0
    with span("investigate", {"app.prompt.version": PROMPT_VERSION}) as current:
        while turns < max_turns and submitted is None:
            turns += 1
            try:
                turn = client.complete_tools(messages, TOOLS)
            except LLMRequestError as error:
                unavailable = str(error)
                break
            if not turn.tool_calls:
                messages += [
                    {"role": "assistant", "content": turn.content},
                    {"role": "user", "content": "Use the tools, and finish with submit_report."},
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
                called.append(call.name)
                if call.name == "submit_report":
                    try:
                        submitted = _Submitted.model_validate_json(call.arguments).diagnoses
                        output = json.dumps({"status": "received"})
                    except ValueError as error:
                        output = _error(f"invalid report: {error}")
                else:
                    output = session.call(call.name, call.arguments)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
        report = InvestigationReport(
            diagnoses=finalize(submitted or [], issues, session.fetched),
            turns=turns,
            finished=submitted is not None,
            stopped="report"
            if submitted is not None
            else ("llm_unavailable" if unavailable else "turn_limit"),
            error=unavailable,
            tool_calls=called,
            seconds=round(time.monotonic() - started, 2),
        )
        set_attributes(
            current,
            {
                "app.investigation.turns": turns,
                "app.investigation.finished": report.finished,
                "app.investigation.causes": [d.cause.value for d in report.diagnoses],
                "app.investigation.tools": called,
            },
        )
    return report
