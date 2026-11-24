"""Mock MCP servers for the systems lunas acts on: erp, payments, mail (SPEC §5).

They are deliberately naive, like real back-office APIs: the payments server pays whatever
account it is given and the mail server sends whatever it is asked to. Every control lives
in `lunas.gapura`, in front of them, so it holds for any client (lunas, the baseline agent,
or anything else that speaks MCP).

In-process (evals, tests) all three share one `Backoffice`, so an eval can read what was
paid and sent. `python -m servers.erp` (or payments, mail) serves one over stdio.
"""

from lunas.servers.backoffice import Backoffice, LedgerEntry, PaymentOrder, SentEmail
from lunas.servers.tools import build_erp, build_mail, build_payments, build_servers

__all__ = [
    "Backoffice",
    "LedgerEntry",
    "PaymentOrder",
    "SentEmail",
    "build_erp",
    "build_mail",
    "build_payments",
    "build_servers",
]
