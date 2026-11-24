"""Synchronous access to MCP servers for the (synchronous) pipeline and evals.

One background event loop holds one MCP client session per server for the life of the
`McpServers` object; calls from any thread are run on that loop. A server is an in-process
`MCPServer` (evals, tests) or anything `mcp.Client` accepts (a stdio command, a URL).
"""

import asyncio
import json
import threading
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import Client


@dataclass(frozen=True)
class ToolSpec:
    """What a client is shown about a tool, and what gapura pins."""

    server: str
    name: str
    description: str
    input_schema: dict

    @property
    def qualified(self) -> str:
        return f"{self.server}.{self.name}"


class McpServers:
    def __init__(self, servers: dict[str, Any], timeout: float = 30.0) -> None:
        self._timeout = timeout
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._clients: dict[str, Client] = {}
        self._stop: asyncio.Event | None = None
        # anyio requires a context to be exited by the task that entered it, so one owner
        # task opens every client, waits for close(), then closes them.
        ready: asyncio.Future = asyncio.run_coroutine_threadsafe(
            self._ready_future(), self._loop
        ).result()
        self._owner = asyncio.run_coroutine_threadsafe(self._own(servers, ready), self._loop)
        try:
            asyncio.run_coroutine_threadsafe(asyncio.wait_for(ready, timeout), self._loop).result()
        except BaseException:
            self.close()
            raise

    async def _ready_future(self) -> asyncio.Future:
        return self._loop.create_future()

    async def _own(self, servers: dict[str, Any], ready: asyncio.Future) -> None:
        self._stop = asyncio.Event()
        try:
            async with AsyncExitStack() as stack:
                for name, server in servers.items():
                    self._clients[name] = await stack.enter_async_context(Client(server))
                ready.set_result(None)
                await self._stop.wait()
        except BaseException as error:
            if not ready.done():
                ready.set_exception(error)
            raise

    def _run(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(self._timeout)

    def list_tools(self) -> list[ToolSpec]:
        async def gather() -> list[ToolSpec]:
            names = list(self._clients)
            listings = await asyncio.gather(*(self._clients[n].list_tools() for n in names))
            return [
                ToolSpec(server, tool.name, tool.description or "", tool.input_schema)
                for server, listing in zip(names, listings, strict=True)
                for tool in listing.tools
            ]

        return self._run(gather())

    def call(self, server: str, tool: str, arguments: dict) -> str:
        """The tool's text result. A server-side error comes back as a JSON error object."""

        async def call() -> str:
            result = await self._clients[server].call_tool(tool, arguments)
            text = "".join(getattr(part, "text", "") for part in result.content)
            if result.is_error:
                return json.dumps({"error": text or "tool failed"})
            return text

        return self._run(call())

    def close(self) -> None:
        if not self._loop.is_running():
            return
        if self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        try:
            self._owner.result(self._timeout)
        except BaseException:
            pass  # a server that failed to start has nothing left to close
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def __enter__(self) -> McpServers:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
