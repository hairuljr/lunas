"""Step 6: can the contract explain a price the PO does not? LLM over retrieved clauses,
every answer verified in code.

The model only locates and copies: which clause sets this item's price, the sentence that
says so, the price and the date. Code then checks that the clause was retrieved from this
vendor's own contract, that the sentence is in it word for word, that the price and the date
are written in that sentence, that the price is the invoiced one, and that it was in effect
on the invoice date. Only then is the price mismatch cleared.

No invoice text reaches this model: the item name comes from the PO and every number from
typed, validated fields.
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel

from lunas.erp import ErpData
from lunas.llm import (
    ChatClient,
    LLMRequestError,
    Message,
    StructuredOutputError,
    complete_structured,
    load_prompt,
)
from lunas.models import ContractClause, Invoice, Issue
from lunas.retrieval.retriever import ContractRetriever
from lunas.telemetry import set_attributes, span
from lunas.text import normalize

PROMPT_NAME = "contract"
PROMPT_VERSION = "v1"

Verdict = Literal[
    "explained",  # the cited clause sets the invoiced price, in effect on the invoice date
    "not_found",  # no clause sets a price for this item
    "no_answer",  # the model gave no valid answer
    "citation_invalid",  # the clause, sentence, price or date cited is not what is written
    "price_differs",  # the contract price is not the invoiced price
    "not_yet_effective",  # the contract price applies only after the invoice date
]


class PriceTerms(BaseModel):
    """What the model reads from the clauses."""

    clause_id: str | None
    quote: str | None
    unit_price: Decimal | None
    effective_date: date | None


class ContractFinding(BaseModel):
    line: int
    item: str
    po_price: Decimal
    invoiced_price: Decimal
    clause_id: str | None = None
    quote: str | None = None
    contract_price: Decimal | None = None
    effective_date: date | None = None
    verdict: Verdict


def build_messages(
    vendor_name: str,
    item: str,
    po_price: Decimal,
    invoiced_price: Decimal,
    invoice_date: date,
    clauses: list[ContractClause],
) -> list[Message]:
    listed = "\n".join(
        f'<clause id="{c.clause_id}" article="{c.article}. {c.article_title}">{c.text}</clause>'
        for c in clauses
    )
    question = (
        f"Supplier: {vendor_name}\n"
        f"Item: {item}\n"
        f"Unit price on the purchase order: {po_price}\n"
        f"Unit price invoiced: {invoiced_price}\n"
        f"Invoice date: {invoice_date.isoformat()}"
    )
    return [
        {"role": "system", "content": load_prompt(PROMPT_NAME, PROMPT_VERSION)},
        {
            "role": "user",
            "content": f"<question>\n{question}\n</question>\n<clauses>\n{listed}\n</clauses>",
        },
    ]


_AMOUNT = re.compile(r"\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?")


def amounts_in(text: str) -> set[Decimal]:
    """Every number written in the text, read with either thousands separator."""
    found = set()
    for token in _AMOUNT.findall(text):
        whole, decimals = token, ""
        if re.search(r"[.,]\d{1,2}$", token) and not re.fullmatch(r"\d{1,3}[.,]\d{3}", token):
            whole, decimals = (
                token[:-3] if token[-3] in ".," else token[:-2],
                token[-2:].strip(".,"),
            )
        try:
            found.add(Decimal(re.sub(r"[.,]", "", whole) + (f".{decimals}" if decimals else "")))
        except InvalidOperation:
            continue
    return found


def verify(
    terms: PriceTerms,
    clauses: list[ContractClause],
    invoiced_price: Decimal,
    invoice_date: date,
    tolerance: Decimal,
) -> Verdict:
    if terms.clause_id is None or terms.unit_price is None or terms.effective_date is None:
        return "not_found"
    clause = next((c for c in clauses if c.clause_id == terms.clause_id), None)
    quote = normalize(terms.quote or "")
    if clause is None or not quote or quote not in normalize(clause.text):
        return "citation_invalid"
    written = terms.quote or ""
    day, year = str(terms.effective_date.day), str(terms.effective_date.year)
    if terms.unit_price not in amounts_in(written) or not (
        re.search(rf"\b{day}\b", written) and year in written
    ):
        return "citation_invalid"
    if abs(terms.unit_price - invoiced_price) > tolerance:
        return "price_differs"
    if terms.effective_date > invoice_date:
        return "not_yet_effective"
    return "explained"


def check_contract(
    invoice: Invoice,
    issues: list[Issue],
    erp: ErpData,
    retriever: ContractRetriever,
    client: ChatClient,
    tolerance: Decimal = Decimal("0.01"),
) -> tuple[list[Issue], list[ContractFinding]]:
    """Returns the issues left after the contract check, and what it found per line."""
    lines = [i.line for i in issues if i.code == "price_mismatch" and i.line is not None]
    po = erp.get_po(invoice.po_number) if lines and invoice.po_number else None
    vendor = erp.get_vendor(po.vendor_id) if po else None
    if po is None or vendor is None:
        return issues, []

    findings = []
    with span("contract_check", {"app.prompt.version": PROMPT_VERSION}) as current:
        for line in lines:
            item = invoice.line_items[line - 1]
            po_line = next(
                p for p in po.lines if normalize(p.description) == normalize(item.description)
            )
            finding = ContractFinding(
                line=line,
                item=po_line.description,
                po_price=po_line.unit_price,
                invoiced_price=item.unit_price,
                verdict="not_found",
            )
            clauses = retriever.price_clauses(vendor.vendor_id, vendor.name, po_line.description)
            if clauses:
                messages = build_messages(
                    vendor.name,
                    po_line.description,
                    po_line.unit_price,
                    item.unit_price,
                    invoice.invoice_date,
                    clauses,
                )
                try:
                    terms = complete_structured(client, messages, PriceTerms, retries=1)
                except StructuredOutputError, LLMRequestError:
                    finding.verdict = "no_answer"
                else:
                    finding = finding.model_copy(
                        update={
                            "clause_id": terms.clause_id,
                            "quote": terms.quote,
                            "contract_price": terms.unit_price,
                            "effective_date": terms.effective_date,
                            "verdict": verify(
                                terms, clauses, item.unit_price, invoice.invoice_date, tolerance
                            ),
                        }
                    )
            findings.append(finding)
        set_attributes(
            current,
            {
                "app.contract.verdicts": [f.verdict for f in findings],
                "app.contract.citations": [f.clause_id or "" for f in findings],
            },
        )

    cleared = {f.line for f in findings if f.verdict == "explained"}
    remaining = [i for i in issues if not (i.code == "price_mismatch" and i.line in cleared)]
    return remaining, findings
