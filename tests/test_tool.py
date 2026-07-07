"""Tool.to_openai() tests — the schema-normalization logic (edge cases the real toolset
doesn't exercise; test_tool_conformance covers to_openai() across the real tools)."""
from legumista_agent.tool import Tool


def _tool(parameters):
    async def run(a):  # pragma: no cover - not invoked here
        return ""
    return Tool(name="t", description="desc", parameters=parameters, read_only=True, run=run)


def test_preserves_user_schema():
    fn = _tool({"type": "object", "properties": {"x": {"type": "string"}},
                "required": ["x"]}).to_openai()["function"]
    assert fn["name"] == "t" and fn["description"] == "desc"
    assert fn["parameters"]["properties"] == {"x": {"type": "string"}}   # not dropped
    assert fn["parameters"]["required"] == ["x"]


def test_normalizes_empty_envelope():
    """An empty schema is filled to a well-formed object with additionalProperties:false
    (discourages the model inventing argument keys)."""
    params = _tool({}).to_openai()["function"]["parameters"]
    assert params == {"type": "object", "properties": {}, "additionalProperties": False}


def test_no_strict_key_emitted():
    """`strict` is deliberately omitted — it's not uniformly supported across the
    OpenAI-compatible endpoints legumista targets (local ollama, small models)."""
    assert "strict" not in _tool({"type": "object", "properties": {}}).to_openai()["function"]
