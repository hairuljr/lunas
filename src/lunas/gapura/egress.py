"""Egress check: arguments of a tool that sends data out, or moves money, may not carry
private data the call has no reason to carry (SPEC §7). Code only."""

import re

from lunas.erp import ErpData
from lunas.gapura.policy import Egress
from lunas.text import EMAIL, digits

# API keys and tokens as they are usually written. A false alarm denies one call; a miss
# leaks a credential, so the patterns lean wide.
_SECRETS = re.compile(
    r"sk-[A-Za-z0-9_\-]{16,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|gh[pousr]_[A-Za-z0-9]{30,}"
    r"|xox[abpr]-[A-Za-z0-9\-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"
)


def _strings(value: object) -> list[str]:
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)]
    if isinstance(value, list | tuple):
        return [s for item in value for s in _strings(item)]
    return [str(value)] if value is not None else []


def egress_violations(args: dict, erp: ErpData, rules: Egress) -> list[str]:
    """What the call would leak. The payee's own account and the recipient's own address
    are expected in the call and are not counted."""
    payee = erp.get_vendor(str(args.get("vendor_id", "")))
    own_account = digits(payee.bank_account if payee else "")
    recipient = str(args.get("to", "")).strip().lower()
    text = "\n".join(_strings({k: v for k, v in args.items() if k not in ("bank_account", "to")}))
    digits_runs = set(re.findall(r"\d[\d .\-]{6,}\d", text))
    numbers = {digits(run) for run in digits_runs}

    found = []
    if rules.vendor_bank_accounts:
        for vendor in erp.vendors():
            if vendor.bank_account != own_account and any(
                vendor.bank_account in run for run in numbers
            ):
                found.append(f"the bank account of vendor {vendor.vendor_id}")
    if rules.vendor_emails:
        known = erp.vendor_emails()
        for email in {match.lower() for match in EMAIL.findall(text)}:
            if email in known and email != recipient:
                found.append(f"the email address of another vendor ({email})")
    if rules.secrets and _SECRETS.search(text):
        found.append("a credential or token")
    return found
