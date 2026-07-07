"""MCP server tests — build the server in-process and exercise it over an in-memory
client (no network, no model calls). Skipped when the optional `serve` extra
(fastmcp) isn't installed."""
import asyncio

import pytest

pytest.importorskip("fastmcp", reason="needs the `serve` extra (pip install 'legumista[serve]')")


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_build_server_exposes_native_tools():
    """Every native + local + genomics tool is registered with a schema, and each tool's
    readOnlyHint annotation matches its source `read_only` flag (the write-capable pysam
    dispatchers are correctly advertised as not-read-only)."""
    from fastmcp import Client
    from legumista_agent.mcp_server import build_server
    from legumista_agent.tools_local import local_read_tools
    from legumista_agent.tools_native import native_tools
    from legumista_agent.tools_pysam import bio_tools

    source = {t.name: t for t in local_read_tools() + native_tools() + bio_tools()}

    async def check():
        async with Client(build_server()) as c:
            tools = await c.list_tools()
            assert {t.name for t in tools} == set(source)
            for t in tools:
                assert t.annotations.readOnlyHint is source[t.name].read_only
                assert (t.inputSchema or {}).get("type") == "object"
            # samtools/bcftools are write-capable dispatchers -> not read-only
            assert next(t for t in tools if t.name == "samtools").annotations.readOnlyHint is False

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
