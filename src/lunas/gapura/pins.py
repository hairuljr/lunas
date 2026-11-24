"""Tool pinning against tool poisoning and rug pulls (SPEC §7).

A tool's description and input schema are what a model reads to decide how to use it, so a
server that changes them (an injected instruction in a description, a new argument) changes
the agent's behaviour without a code change. gapura pins a hash of each tool's name,
description and schema in `policies/pins.json`; a tool whose hash differs, or that has no
pin, is quarantined: not shown and not callable.

    uv run python -m lunas.gapura.pins --update   # after a reviewed change to lunas.servers
"""

import argparse
import hashlib
import json
from pathlib import Path

from lunas.gapura.transport import ToolSpec

DEFAULT_PINS = Path(__file__).resolve().parents[3] / "policies" / "pins.json"


def tool_hash(spec: ToolSpec) -> str:
    canonical = json.dumps(
        {"name": spec.qualified, "description": spec.description, "schema": spec.input_schema},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def load_pins(path: Path = DEFAULT_PINS) -> dict[str, str]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def unpinned(specs: list[ToolSpec], pins: dict[str, str]) -> dict[str, str]:
    """Tools to quarantine, with why."""
    found = {}
    for spec in specs:
        pinned = pins.get(spec.qualified)
        if pinned is None:
            found[spec.qualified] = "not pinned"
        elif pinned != tool_hash(spec):
            found[spec.qualified] = "description or schema changed since it was pinned"
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description="Pin the mock servers' tools.")
    parser.add_argument("--update", action="store_true", help="write policies/pins.json")
    args = parser.parse_args()

    from lunas.config import Settings
    from lunas.erp import ErpData
    from lunas.gapura.transport import McpServers
    from lunas.servers import Backoffice, build_servers

    with McpServers(build_servers(Backoffice(ErpData.load(Settings().data_dir)))) as servers:
        specs = servers.list_tools()
    pins = {spec.qualified: tool_hash(spec) for spec in specs}
    if args.update:
        DEFAULT_PINS.write_text(json.dumps(pins, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Pinned {len(pins)} tools to {DEFAULT_PINS}")
    else:
        changed = unpinned(specs, load_pins())
        print(json.dumps(changed, indent=2) if changed else "All tools match their pins.")


if __name__ == "__main__":
    main()
