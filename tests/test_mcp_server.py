"""MCP server tests — build the server in-process and drive it over an in-memory client
(no network, no model calls)."""
import asyncio
import json
import re

import pytest

pytest.importorskip("fastmcp", reason="fastmcp is a legumista dependency — reinstall the package")

NAME_GRAMMAR = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")   # MCP / OpenAI tool-name grammar


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_served_toolset_conforms_to_mcp_spec():
    """Every legumista tool is advertised, and the served surface satisfies the MCP tool
    spec: name grammar, a JSON-serializable object inputSchema, and a readOnlyHint that
    matches the source tool's read_only flag (so write-capable dispatchers like samtools
    are correctly advertised as not-read-only)."""
    from fastmcp import Client
    from legumista_agent.mcp_server import build_server
    from legumista_agent.tools_lis import lis_tools
    from legumista_agent.tools_local import local_read_tools
    from legumista_agent.tools_mine import mine_tools
    from legumista_agent.tools_native import native_tools
    from legumista_agent.tools_pysam import bio_tools

    source = {t.name: t
              for t in (local_read_tools() + native_tools() + lis_tools()
                        + mine_tools() + bio_tools())}

    async def check():
        async with Client(build_server()) as c:
            tools = await c.list_tools()
            assert {t.name for t in tools} == set(source)
            for t in tools:
                assert NAME_GRAMMAR.match(t.name), f"{t.name!r} violates the tool-name grammar"
                assert t.annotations.readOnlyHint is source[t.name].read_only
                schema = t.inputSchema or {}
                assert schema.get("type") == "object"
                assert isinstance(schema.get("properties", {}), dict)
                json.dumps(schema)                 # advertised schema must serialize
            assert next(t for t in tools if t.name == "samtools").annotations.readOnlyHint is False

    _run(check())


def test_tool_call_returns_single_text_block():
    """Calling a tool over MCP routes to the legumista handler and comes back as the
    spec's content model — one `text` content block carrying the handler's output.
    `grep` is local (no network), so it exercises the full bridge without a request."""
    from fastmcp import Client
    from legumista_agent.mcp_server import build_server

    async def check():
        async with Client(build_server()) as c:
            res = await c.call_tool("grep", {"pattern": "zzz-no-such-token", "path": "."})
            assert len(res.content) == 1
            assert res.content[0].type == "text"
            assert res.content[0].text            # real handler text, not an empty block

    _run(check())
