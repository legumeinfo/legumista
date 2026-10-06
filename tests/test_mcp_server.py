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


def _field(obj, snake, camel):
    """MCP SDK v2 (FastMCP 4) renamed readOnlyHint/inputSchema to snake_case and deprecated
    the old names; FastMCP 3 has only the old ones. Read whichever this install has."""
    return getattr(obj, snake, None) if hasattr(obj, snake) else getattr(obj, camel)


def _source_tools():
    from legumista_agent.tools_catalog import catalog_tools
    from legumista_agent.tools_lis import lis_tools
    from legumista_agent.tools_local import local_read_tools
    from legumista_agent.tools_mine import mine_tools
    from legumista_agent.tools_native import native_tools
    from legumista_agent.tools_pysam import bio_tools
    from legumista_agent.tools_verify import verify_tools
    from legumista_agent.tools_browser import browser_tools
    from legumista_agent.tools_extract import extract_tools
    from legumista_agent.tools_guide import guide_tools
    return {t.name: t
            for t in (local_read_tools() + native_tools() + lis_tools()
                      + mine_tools() + catalog_tools() + bio_tools() + extract_tools()
                      + browser_tools() + verify_tools() + guide_tools())}


def test_served_toolset_conforms_to_mcp_spec():
    """Every legumista tool the server means to serve is advertised, and the served
    surface satisfies the MCP tool spec: name grammar, a JSON-serializable object
    inputSchema, and a readOnlyHint that matches the source tool's read_only flag. The
    write-capable dispatchers are read-only exactly when the server was started without
    --allow-write, since they then cannot write."""
    from fastmcp import Client
    from legumista_agent.mcp_server import build_server

    source = _source_tools()
    expected = set(source)

    async def check():
        async with Client(build_server()) as c:
            tools = await c.list_tools()
            assert {t.name for t in tools} == expected
            for t in tools:
                assert NAME_GRAMMAR.match(t.name), f"{t.name!r} violates the tool-name grammar"
                assert _field(t.annotations, "read_only_hint", "readOnlyHint") \
                    is source[t.name].read_only
                schema = _field(t, "input_schema", "inputSchema") or {}
                assert schema.get("type") == "object"
                assert isinstance(schema.get("properties", {}), dict)
                json.dumps(schema)                 # advertised schema must serialize
            samtools = next(t for t in tools if t.name == "samtools")
            assert _field(samtools.annotations, "read_only_hint", "readOnlyHint") is True
        async with Client(build_server(allow_write=True)) as c:
            samtools = next(t for t in await c.list_tools() if t.name == "samtools")
            assert _field(samtools.annotations, "read_only_hint", "readOnlyHint") is False

    _run(check())


def test_no_local_file_tools_are_served():
    """legumista carried a `read_file` and a `grep` for its own agent loop, and filtered
    them out of the served surface because MCP clients ship their own — more capable, not
    workspace-sandboxed. With the agent loop gone they were deleted rather than left as
    dead code. This guards the deletion: reintroducing either would put a second, weaker
    way to read a file in front of the model."""
    from fastmcp import Client
    from legumista_agent.mcp_server import _HANDLERS, build_server

    async def check():
        async with Client(build_server()) as c:
            names = {t.name for t in await c.list_tools()}
            assert not ({"read_file", "grep"} & names)
            assert "web_fetch" in names        # tools_local's remaining tool must survive
    _run(check())
    assert not ({"read_file", "grep"} & set(_HANDLERS)), "unadvertised handler still callable"


def test_tool_call_returns_single_text_block():
    """Calling a tool over MCP routes to the legumista handler and comes back as the
    spec's content model — one `text` content block carrying the handler's output.
    `paper_search` with a missing query fails inside the handler without a request, so
    this exercises the full bridge offline."""
    from fastmcp import Client
    from legumista_agent.mcp_server import build_server

    async def check():
        async with Client(build_server()) as c:
            res = await c.call_tool("paper_search", {"query": ""}, raise_on_error=False)
            assert len(res.content) == 1
            assert res.content[0].type == "text"
            assert res.content[0].text            # real handler text, not an empty block

    _run(check())


def test_failures_reach_the_client_flagged_is_error():
    """A tool that could not answer must reach the client as isError=true with its text
    intact (the model reads the text; the client and evals read the flag). Offline: the
    handler fails on input validation before any request is made. Valid-empty results are
    covered at the unit level in test_results.py."""
    from fastmcp import Client
    from legumista_agent.mcp_server import build_server

    async def check():
        async with Client(build_server()) as c:
            bad = await c.call_tool("openalex_by_doi", {"doi": ""}, raise_on_error=False)
            assert bad.is_error is True
            assert bad.content[0].text == "error: missing 'doi'"

    _run(check())


def test_every_tool_property_is_described():
    """An undescribed parameter leaves the model to guess what it takes (plan item D-02)."""
    missing = sorted(f"{name}.{prop}" for name, tool in _source_tools().items()
                     for prop, spec in (tool.parameters.get("properties") or {}).items()
                     if not (spec.get("description") or "").strip())
    assert not missing, f"parameters without a description: {missing}"


def _hints(**build):
    from fastmcp import Client
    from legumista_agent.mcp_server import build_server

    async def collect():
        async with Client(build_server(**build)) as c:
            return {t.name: _field(t.annotations, "read_only_hint", "readOnlyHint")
                    for t in await c.list_tools()}
    return _run(collect())


def test_a_local_deployment_advertises_write_capable_tools_honestly(monkeypatch):
    """Locally the hints stay honest, so a client asks before a tool that can write a
    file or file an issue."""
    from legumista_agent import tools_report

    monkeypatch.delenv("LEGUMISTA_DEPLOYMENT", raising=False)
    monkeypatch.setattr(tools_report, "_app_configured", lambda: True)
    hints = _hints(allow_write=True, allow_report=True)
    for name in ("samtools", "bcftools", "tabix_index", "extract_features",
                 "report_data_issue"):
        assert hints[name] is False, name
    assert hints["browser_link"] is True and hints["lis_gene"] is True


def test_a_public_deployment_advertises_every_tool_read_only(monkeypatch):
    """A public host's users are not prompted; the server's own checks stand in."""
    from legumista_agent import tools_report

    monkeypatch.setenv("LEGUMISTA_DEPLOYMENT", "public")
    monkeypatch.setattr(tools_report, "_app_configured", lambda: True)
    hints = _hints(allow_write=True, allow_report=True)
    assert all(hints.values()), [n for n, v in hints.items() if not v]


def test_an_unknown_deployment_value_fails_safe_toward_prompting(monkeypatch):
    monkeypatch.setenv("LEGUMISTA_DEPLOYMENT", "pubilc")       # a typo
    assert _hints(allow_write=True)["samtools"] is False


def test_report_data_issue_is_served_only_with_the_flag_and_an_app(monkeypatch):
    from legumista_agent import tools_report

    monkeypatch.setattr(tools_report, "_app_configured", lambda: False)
    assert "report_data_issue" not in _hints(allow_report=True)
    monkeypatch.setattr(tools_report, "_app_configured", lambda: True)
    assert "report_data_issue" not in _hints(allow_report=False)
    assert "report_data_issue" in _hints(allow_report=True)
