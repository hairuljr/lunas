"""Serve one mock server over stdio: `python -m lunas.servers erp|payments|mail`."""

import sys

from lunas.config import Settings
from lunas.erp import ErpData
from lunas.servers import Backoffice, build_servers


def main() -> None:
    names = ("erp", "payments", "mail")
    if len(sys.argv) != 2 or sys.argv[1] not in names:
        sys.exit(f"usage: python -m lunas.servers {'|'.join(names)}")
    office = Backoffice(ErpData.load(Settings().data_dir))
    build_servers(office)[sys.argv[1]].run("stdio")


if __name__ == "__main__":
    main()
