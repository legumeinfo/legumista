"""legumista_agent — a small OpenAI tool-calling agent harness.

Everything needed to let a model use tools and iterate to an answer, and nothing more
(no UI, telemetry, or sub-agents):

  tool.py         Tool interface + OpenAI function-spec conversion
  permissions.py  per-tool allow/deny gate (modes: read_only | allow_all | deny_all)
  mcp_client.py   connect to optional MCP servers (official `mcp` SDK) and expose them
  loop.py         the agent loop: messages + tools -> /chat/completions -> run
                  tool_calls -> feed results back -> repeat until a final answer
  agent.py        blocking entry point that assembles the toolset and runs the loop

The model path is `llm.complete` (OpenAI /chat/completions). The deterministic
discovery/ideation pipelines do NOT use this package — it backs only the tool-using
`legumista research` command (and, when enabled, the per-phase tool loop).
"""
