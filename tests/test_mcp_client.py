"""MCP client helper tests — namespacing external tool names and flattening a
CallToolResult into the text the agent sees. Result objects are lightweight fakes
matching the mcp SDK's duck-typed shape; no server is spawned."""
import re
from types import SimpleNamespace

import pytest

from legumista_agent.mcp_client import _result_text, _safe_name

NAME_GRAMMAR = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")   # MCP / OpenAI tool-name grammar


def test_safe_name_namespaces_and_sanitizes():
    assert _safe_name("weather", "get_forecast") == "mcp__weather__get_forecast"
    assert _safe_name("my server", "a.b/c") == "mcp__my_server__a_b_c"   # illegal chars -> _


@pytest.mark.parametrize("server,tool", [
    ("emoji🔥srv", "do!@#$%^&*()thing"),   # non-ascii + punctuation
    ("s" * 80, "t" * 80),                  # over-long -> capped
    ("", ""),                              # degenerate
])
def test_safe_name_is_grammar_valid_and_capped(server, tool):
    """Whatever an external server calls itself, the namespaced name handed to the model
    must satisfy the tool-name grammar and stay within the length cap."""
    name = _safe_name(server, tool)
    assert NAME_GRAMMAR.match(name) and len(name) <= 64


def _result(blocks, is_error=False):
    return SimpleNamespace(content=blocks, isError=is_error)


def _text(t):
    return SimpleNamespace(type="text", text=t)


def test_result_text_joins_and_marks_non_text():
    r = _result([_text("caption"), SimpleNamespace(type="image", data="…")])
    assert _result_text(r) == "caption\n[image content omitted]"


def test_result_text_error_prefix():
    assert _result_text(_result([_text("boom")], is_error=True)) == "[tool error] boom"


def test_result_text_empty():
    assert _result_text(_result([])) == "(empty result)"
    assert _result_text(_result(None)) == "(empty result)"
