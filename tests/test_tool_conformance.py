"""Spec-conformance for the *entire* exposed toolset: every tool legumista ships must
produce a valid OpenAI function-tool spec (which also feeds the MCP schema). Parametrized
over the real tools, so adding one with a bad name or malformed schema fails the build."""
import json
import re

import pytest

from legumista_agent.tools_local import local_read_tools
from legumista_agent.tools_native import native_tools
from legumista_agent.tools_pysam import bio_tools

NAME_GRAMMAR = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")   # OpenAI / MCP tool-name grammar
ALL_TOOLS = local_read_tools() + native_tools() + bio_tools(allow_write=True)


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=[t.name for t in ALL_TOOLS])
def test_tool_produces_valid_openai_spec(tool):
    assert NAME_GRAMMAR.match(tool.name), f"{tool.name!r} violates the tool-name grammar"
    spec = tool.to_openai()
    assert spec["type"] == "function"
    fn = spec["function"]
    assert fn["name"] == tool.name
    assert isinstance(fn["description"], str) and fn["description"].strip()
    params = fn["parameters"]
    assert params["type"] == "object" and isinstance(params["properties"], dict)
    json.dumps(spec)                                    # goes verbatim into the request body
    required = set((tool.parameters or {}).get("required") or [])
    assert required <= set(params["properties"]), \
        f"{tool.name}: required {required - set(params['properties'])} absent from properties"


def test_tool_names_are_unique():
    names = [t.name for t in ALL_TOOLS]
    assert len(names) == len(set(names)), "duplicate tool names across the toolset"
