<!-- Repository guide for AI coding assistants and contributors. -->

# Legumista — repository guide

**Legumista** is a research-discovery pipeline: a Python engine that crawls a citation
graph into a reviewed corpus and then into grounded research ideas, using an
OpenAI-compatible model endpoint only as a bounded judge/synthesizer. The `legumista`
CLI (`legumista_cli.py`) is the single entry point. For how it works end to end, read
[`README.md`](README.md) (setup + design) and [`PIPELINE.md`](PIPELINE.md) (the
three-phase walkthrough). The repo root is the tool plus a clean scaffold; a complete,
runnable *Lupinus* (Fabaceae) genomics demo project lives in
[`examples/lupinus/`](examples/lupinus/).

## Working in this repository
- Pure Python calling an OpenAI-compatible `/chat/completions` endpoint
  (`config.py llm`) — no external agent runtime, proxy, or MCP server in the loop. The
  agentic tools (`legumista_agent/`) are native and keyless; users may still plug their
  own MCP servers into `.mcp.json`.
- `orchestrator.py` owns the crawl and writes every canonical file. Do **not** hand-edit
  the JSON ledgers (`agent_state.json`, `library_manifest.json`, `ideation_state.json`);
  they are program-owned. Outputs live under `./reviews/`, `./corpus/`, `./ideas/`,
  `./papers/`.
- **One shared agentic base.** Every phase — crawl judge, review, ideation, idea
  write-up — runs its model calls through `legumista_agent.runtime.PhaseSession`: one
  tool-enabled runtime, opened once, every call routed through the same agentic loop
  under the shared system prompt and the same max-turns/timeout. Phases differ only in
  the user prompt (`prompts/*.md`) and context they feed. `config.system_prompt()`
  (base + tools spec) and `config.llm()["timeout"]` are the single knobs.
- **One runtime system prompt.** Every model call (all pipeline phases *and*
  `research`) uses the same base — `system-prompt.md` (scientist persona,
  evidential-integrity rules, tool doctrine) — resolved by `config.system_prompt_base()`
  as: `$LEGUMISTA_SYSTEM_PROMPT` → `<project>/system-prompt.md` → the copy shipped in the
  `legumista_assets` package (the default). The native-tools spec is appended via
  `config.tools_spec()`. `config.system_prompt()` returns the two joined.
- Topic identity and prompt wording are data: `legumista.yml` + `prompts/*.md`, read
  through `config.py` (default prompts ship in `legumista_assets/`; a project may override
  any of them). The demo's curated domain knowledge base lives under
  `examples/lupinus/knowledge/` (indexed at `examples/lupinus/knowledge/README.md`).

## Contributing
- **Dev install:** `pip install -e '.[mcp]' pytest` (Python ≥3.11). The `[mcp]` extra
  is only needed if you touch the optional MCP client.
- **Tests:** `python -m pytest -q` (suite in `tests/`).
- **CI:** `.github/workflows/ci.yml` runs the tests on Python 3.11 and 3.12 for every
  push and pull request. Keep them green.
- **Try it end to end:** run the bundled demo in [`examples/lupinus/`](examples/lupinus/)
  (`cd examples/lupinus && legumista discover`).
- Licensed MIT (see [`LICENSE`](LICENSE)).
