"""MCP server tests — build the server in-process and exercise it over an in-memory
client (no network, no model calls). Skipped when the optional `serve` extra
(fastmcp) isn't installed."""
import asyncio

import pytest

pytest.importorskip("fastmcp", reason="needs the `serve` extra (pip install 'legumista[serve]')")


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_build_server_exposes_native_tools():
    """Every native + local read tool is registered, read-only, with a schema."""
    from fastmcp import Client
    from legumista_agent.mcp_server import build_server
    from legumista_agent.tools_local import local_read_tools
    from legumista_agent.tools_native import native_tools

    expected = {t.name for t in local_read_tools() + native_tools()}

    async def check():
        async with Client(build_server()) as c:
            tools = await c.list_tools()
            names = {t.name for t in tools}
            assert names == expected, names ^ expected
            for t in tools:
                assert t.annotations and t.annotations.readOnlyHint is True
                assert (t.inputSchema or {}).get("type") == "object"

    _run(check())


def test_tool_invocation_bridges_to_handler():
    """Calling a tool over MCP routes to the legumista handler and returns text.
    `grep` is a local, no-network tool, so it exercises the bridge without a request."""
    from fastmcp import Client
    from legumista_agent.mcp_server import build_server

    async def check():
        async with Client(build_server()) as c:
            res = await c.call_tool("grep", {"pattern": "zzz-no-such-token", "path": "."})
            text = res.content[0].text
            assert isinstance(text, str) and text  # a real (text) result, not an empty block

    _run(check())
