"""gapura: the gateway between any agent or workflow and the MCP tool servers.

Every call goes through `Gateway.call`, which decides in code, in this order:
pin (quarantined tools are refused) -> role (default deny) -> idempotency key ->
constraints -> egress -> taint -> approval threshold. The strictest outcome wins. A call that
needs approval is not executed until it is made again with `approved_by`. Every decision is
written to the audit log and traced.
"""

import json
import threading
from dataclasses import dataclass, field
from enum import StrEnum

from lunas.erp import ErpData
from lunas.gapura.egress import egress_violations
from lunas.gapura.pins import load_pins, unpinned
from lunas.gapura.policy import CONSTRAINTS, Outcome, Policy, load_policy
from lunas.gapura.transport import McpServers, ToolSpec
from lunas.telemetry import set_attributes, span
from lunas.text import parse_amount


class Status(StrEnum):
    EXECUTED = "executed"
    # `authorize` only: the policy allows the call; the caller runs it itself.
    ALLOWED = "allowed"
    DENIED = "denied"
    APPROVAL_REQUIRED = "approval_required"


@dataclass
class Session:
    """One caller working on one invoice. Taint only grows."""

    role: str
    taint: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class GateResult:
    status: Status
    reasons: tuple[str, ...] = ()
    output: str | None = None

    def for_model(self) -> str:
        """What an agent is told back: the tool's output, or why nothing happened."""
        if self.status == Status.EXECUTED:
            return self.output or ""
        return json.dumps({"status": self.status.value, "reasons": list(self.reasons)})


@dataclass(frozen=True)
class AuditEntry:
    role: str
    tool: str
    arguments: dict
    status: Status
    reasons: tuple[str, ...]
    taint_before: tuple[str, ...]
    approved_by: str | None


class Gateway:
    def __init__(
        self,
        servers: McpServers,
        erp: ErpData,
        policy: Policy | None = None,
        pins: dict[str, str] | None = None,
    ) -> None:
        self._servers = servers
        self._erp = erp
        self.policy = policy or load_policy()
        self._pins = load_pins() if pins is None else pins
        self.audit: list[AuditEntry] = []
        self._lock = threading.Lock()

    def session(self, role: str) -> Session:
        if role not in self.policy.roles:
            raise ValueError(f"unknown role {role}")
        return Session(role, set(self.policy.roles[role].initial_taint))

    def quarantined(self) -> dict[str, str]:
        """Re-read every server's tool list: a server can change a tool at any time."""
        return unpinned(self._servers.list_tools(), self._pins)

    def tools(self, session: Session) -> list[ToolSpec]:
        """The tools this caller may see: in its role, and matching their pins."""
        specs = self._servers.list_tools()
        blocked = unpinned(specs, self._pins)
        allowed = set(self.policy.roles[session.role].tools)
        return [s for s in specs if s.qualified in allowed and s.qualified not in blocked]

    def _evaluate(self, session: Session, tool: str, args: dict, approved_by: str | None):
        deny, approval = [], []
        if reason := self.quarantined().get(tool):
            deny.append(f"{tool} is quarantined: {reason}")
        policy = self.policy.tools.get(tool)
        if policy is None or tool not in self.policy.roles[session.role].tools:
            deny.append(f"{tool} is not allowed for role {session.role}")
            return deny, approval
        if policy.idempotency_key and not str(args.get(policy.idempotency_key, "")).strip():
            deny.append(f"{tool} needs an idempotency key ({policy.idempotency_key})")
        for name in policy.constraints:
            if reason := CONSTRAINTS[name](args, self._erp):
                deny.append(reason)
        if {"can_exfiltrate", "moves_money"} & set(policy.labels):
            deny += [
                f"the call carries {leak}"
                for leak in egress_violations(args, self._erp, self.policy.egress)
            ]
        for rule in self.policy.taint:
            if rule.tool_label in policy.labels and set(rule.session) <= session.taint:
                message = (
                    f"the session has seen {' and '.join(sorted(rule.session))} data and "
                    f"{tool} is {rule.tool_label}"
                )
                (deny if rule.outcome == Outcome.DENY else approval).append(message)
        if policy.approval:
            amount = parse_amount(args.get("amount"))
            if amount is not None and amount > policy.approval.amount_above:
                approval.append(f"amount above {policy.approval.amount_above}")
        if approved_by:
            approval = []
        return deny, approval

    def authorize(self, session: Session, tool: str, args: dict) -> GateResult:
        """The policy's verdict on a call the caller runs itself (an in-process tool such as
        the investigation's), with the same checks, taint and audit as `call`."""
        return self._gate(session, tool, args, None, execute=False)

    def call(
        self, session: Session, tool: str, args: dict, approved_by: str | None = None
    ) -> GateResult:
        return self._gate(session, tool, args, approved_by, execute=True)

    def _gate(
        self, session: Session, tool: str, args: dict, approved_by: str | None, execute: bool
    ) -> GateResult:
        with span("gapura.call", {"gen_ai.tool.name": tool, "app.gapura.role": session.role}) as s:
            taint_before = tuple(sorted(session.taint))
            deny, approval = self._evaluate(session, tool, args, approved_by)
            if deny:
                result = GateResult(Status.DENIED, tuple(deny))
            elif approval:
                result = GateResult(Status.APPROVAL_REQUIRED, tuple(approval))
            else:
                output = None
                if execute:
                    server, name = tool.split(".", 1)
                    output = self._servers.call(server, name, args)
                labels = self.policy.tools[tool].labels
                if "reads_private" in labels:
                    session.taint.add("private")
                if "returns_untrusted" in labels:
                    session.taint.add("untrusted")
                result = GateResult(Status.EXECUTED if execute else Status.ALLOWED, output=output)
            set_attributes(
                s, {"app.gapura.status": result.status.value, "app.gapura.reasons": result.reasons}
            )
        with self._lock:
            self.audit.append(
                AuditEntry(
                    session.role,
                    tool,
                    args,
                    result.status,
                    result.reasons,
                    taint_before,
                    approved_by,
                )
            )
        return result
