import pytest

from lunas.models import Issue
from lunas.pipeline.nodes import decide as decide_module
from lunas.pipeline.nodes.investigate import (
    POSSIBLE_CAUSES,
    RECOMMENDED_ACTION,
    Diagnosis,
    ExceptionCause,
    allowed_cause,
)
from lunas.pipeline.nodes.route import (
    HARD_REJECT,
    INVESTIGABLE,
    SECURITY,
    Route,
    route_invoice,
)


def route(*codes: str) -> Route:
    return route_invoice([Issue(code=code, message=code) for code in codes]).route


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        ((), Route.CLEAN),
        (("price_mismatch",), Route.INVESTIGATE),
        (("possible_duplicate", "tax_mismatch"), Route.INVESTIGATE),
        (("po_missing",), Route.HARD_REJECT),
        (("duplicate_invoice", "price_mismatch"), Route.HARD_REJECT),
        (("bank_account_mismatch",), Route.HUMAN),
        (("vendor_mismatch", "duplicate_invoice"), Route.HUMAN),
        (("bank_account_mismatch", "price_mismatch"), Route.HUMAN),
        (("bank_account_unreadable",), Route.HUMAN),
        (("extraction_failed",), Route.HUMAN),
        (("a_check_added_later",), Route.HUMAN),
        (("a_check_added_later", "price_mismatch"), Route.HUMAN),
    ],
)
def test_route_table(codes: tuple[str, ...], expected: Route):
    assert route(*codes) == expected


def test_route_names_the_issues_that_decided_it():
    result = route_invoice(
        [Issue(code="price_mismatch", message=""), Issue(code="tax_mismatch", message="")]
    )

    assert result.because == ["price_mismatch", "tax_mismatch"]


def test_router_agrees_with_the_decision_table():
    """A hard reject is a reject and a security risk an escalation, in both tables."""
    assert HARD_REJECT == decide_module._REJECT
    assert SECURITY == decide_module._ESCALATE


def test_every_investigable_issue_has_causes_and_every_cause_an_action():
    assert set(POSSIBLE_CAUSES) == INVESTIGABLE
    assert set(RECOMMENDED_ACTION) == set(ExceptionCause)


def test_a_cause_outside_the_issues_possible_causes_is_not_allowed():
    def diagnosis(code: str, cause: ExceptionCause) -> Diagnosis:
        return Diagnosis(issue_code=code, cause=cause, evidence=[], explanation="")

    assert allowed_cause(diagnosis("price_mismatch", ExceptionCause.OVERCHARGE))
    assert allowed_cause(diagnosis("price_mismatch", ExceptionCause.UNCLEAR))
    assert not allowed_cause(diagnosis("price_mismatch", ExceptionCause.SHORT_DELIVERY))
