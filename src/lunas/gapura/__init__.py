"""gapura: the gateway between lunas (or any agent) and the tool servers it acts through.

policy (policies/default.yaml) -> taint -> constraints -> egress -> pins -> audit, all in
code, outside any model (SPEC §4, §7). `local_gateway` runs it against the in-process mock
servers in `lunas.servers`.
"""

from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def local_gateway(erp) -> Iterator[tuple]:
    """A gateway over fresh in-process mock servers, and the backoffice they write to.
    What they record (payments, ledger, outbox) lasts as long as the process."""
    from lunas.gapura.gateway import Gateway
    from lunas.gapura.transport import McpServers
    from lunas.servers import Backoffice, build_servers

    office = Backoffice(erp)
    with McpServers(build_servers(office)) as servers:
        yield Gateway(servers, erp), office
