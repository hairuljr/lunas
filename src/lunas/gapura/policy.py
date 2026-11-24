"""The gapura policy file (`policies/default.yaml`): schema, loading, and the named checks
it may refer to.

The YAML only names checks; their code lives here. Loading fails on anything unknown (a
key, a label, a constraint, a tool in a role that has no policy entry), so a typo cannot
quietly disable a control.
"""

from collections.abc import Callable
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from lunas.erp import ErpData
from lunas.text import digits, parse_amount
from lunas.vendor_mail import REASONS, TEMPLATES

DEFAULT_POLICY = Path(__file__).resolve().parents[3] / "policies" / "default.yaml"

ToolLabel = Literal["reads_private", "writes", "moves_money", "can_exfiltrate", "returns_untrusted"]
Taint = Literal["untrusted", "private"]


class Outcome(StrEnum):
    ALLOW = "allow"
    APPROVAL = "approval"
    DENY = "deny"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Approval(_Strict):
    amount_above: Decimal


class ToolPolicy(_Strict):
    labels: list[ToolLabel] = []
    # The argument that carries the idempotency key; a call without one is refused.
    idempotency_key: str | None = None
    constraints: list[str] = []
    approval: Approval | None = None


class Role(_Strict):
    initial_taint: list[Taint]
    tools: list[str]


class TaintRule(_Strict):
    session: list[Taint]
    tool_label: ToolLabel
    outcome: Literal[Outcome.APPROVAL, Outcome.DENY]


class Egress(_Strict):
    vendor_bank_accounts: bool = True
    vendor_emails: bool = True
    secrets: bool = True


class Policy(_Strict):
    version: Literal[1]
    roles: dict[str, Role]
    tools: dict[str, ToolPolicy]
    taint: list[TaintRule] = []
    egress: Egress = Egress()

    @model_validator(mode="after")
    def _known_names(self) -> Policy:
        for name, tool in self.tools.items():
            unknown = [c for c in tool.constraints if c not in CONSTRAINTS]
            if unknown:
                raise ValueError(f"{name}: unknown constraint(s) {unknown}")
        for role_name, role in self.roles.items():
            missing = [t for t in role.tools if t not in self.tools]
            if missing:
                raise ValueError(f"role {role_name}: tools without a policy entry {missing}")
        return self


def load_policy(path: Path = DEFAULT_POLICY) -> Policy:
    return Policy.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


# --- named constraints -------------------------------------------------------------------
# Each returns None when the call satisfies it, or the reason it does not.

Constraint = Callable[[dict, ErpData], str | None]


def destination_is_vendor_account(args: dict, erp: ErpData) -> str | None:
    vendor = erp.get_vendor(str(args.get("vendor_id", "")))
    if vendor is None:
        return "the payee is not in the vendor master"
    if digits(args.get("bank_account")) != digits(vendor.bank_account):
        return "the destination account is not the payee's account in the vendor master"
    return None


def amount_is_positive(args: dict, erp: ErpData) -> str | None:
    amount = parse_amount(args.get("amount"))
    if amount is None:
        return "the amount is not a number"
    return None if amount > 0 else "the amount is not positive"


def template_only(args: dict, erp: ErpData) -> str | None:
    if args.get("template") not in TEMPLATES or args.get("reason") not in REASONS:
        return "only the fixed vendor templates may be sent"
    return None


def recipient_is_vendor_email(args: dict, erp: ErpData) -> str | None:
    recipient = str(args.get("to", "")).strip().lower()
    if recipient not in erp.vendor_emails():
        return "the recipient is not a vendor email in the vendor master"
    return None


CONSTRAINTS: dict[str, Constraint] = {
    "destination_is_vendor_account": destination_is_vendor_account,
    "amount_is_positive": amount_is_positive,
    "template_only": template_only,
    "recipient_is_vendor_email": recipient_is_vendor_email,
}
