<!-- mcp-name: io.github.legumeinfo/legumista -->

![Legumista bean mascot](./legumista_assets/legumista.png)

# Legumista — a citation-grounded research discovery, synthesis & ideation pipeline using LLMs

**Legumista** turns a citation graph into a reviewed corpus and then into grounded
research ideas. It speaks the OpenAI-compatible Chat Completions API, so it runs against
any model you choose — a local one via ollama, or any hosted endpoint. It does the
deterministic work (citation-graph crawl, scope filtering, PDF retrieval, ledger commits)
in Python and uses the model only as a bounded judge, synthesizer, and idea generator, so
every claim traces back to a real, tool-retrieved source. The repository root is the
tool plus a clean scaffold; a complete, runnable *Lupinus* (Fabaceae) genomics demo lives
in [`examples/lupinus/`](examples/lupinus/). The pipeline is topic-agnostic — one
`legumista.yml` and a set of anchor DOIs re-point it at any field (see PIPELINE.md
§"Adapting the pipeline to a new research topic").

> **New here?** [`PIPELINE.md`](PIPELINE.md) is the full user guide to the
> three-phase discovery → synthesis → ideation arc and the rationale behind each
> phase. This README covers setup and the model plumbing.

## Why Legumista

Most "AI researcher" tools take a query, search the web, scrape the top results, and write
a one-shot report with a hosted model. Legumista is built for a different job — maintaining
a rigorous, reproducible evidence base for an ongoing research program:

- **Citation-graph discovery, not web scraping.** It grows a corpus by walking OpenAlex
  reference/citation edges out from *your* verified anchor DOIs, with anti-drift distance
  scoring (similarity-to-anchors plus a hop cap). That is how a systematic review is
  actually done — backward/forward citation chasing — yielding a bounded, on-topic,
  reproducible corpus rather than a report stitched from whatever a search engine ranked.
- **Provider-agnostic and keyless.** It targets the OpenAI-compatible Chat Completions API,
  so point it at anything — a local model or a hosted endpoint — and the scholarly sources
  (OpenAlex, Crossref, arXiv, Europe PMC, bioRxiv) need no API keys. Nothing has to leave
  your machine, which matters for unpublished hypotheses and governed data.
- **Deterministic engine; the model is a bounded worker.** Python owns all state, file
  writes, and control flow; the model only ever returns a bounded judgement or synthesis.
  A malformed or hallucinated reply skips a step instead of corrupting the corpus — which is
  what makes thousand-paper, multi-hour runs safe and resumable.
- **Evidential integrity by construction.** The model is fed only retrieved text and cannot
  write the ledgers, so "cite only what a tool returned; never invent a DOI" is an
  architectural guarantee, not a hopeful instruction.
- **It goes past the review to ideation.** Phase 3 grows and matures concrete research
  directions grounded in the corpus and its synthesis, then expands them into
  citation-grounded proposals — the "what should we do next?" step most tools skip.
- **Durable infrastructure, not a chat answer.** The corpus, review, and ideas are
  versioned artifacts on disk with provenance, re-runnable on a schedule and steerable via
  `context_inputs/`.

**Scope, honestly.** This is tuned for *depth in a bounded scientific domain*, not speed on
general-interest topics; it favours scholarly indexes over open-web coverage; and synthesis
quality tracks the model you point it at. It is infrastructure for driving a research
program — not a fast general-purpose report generator.

## How the pieces fit

The **autonomous pipeline** (`legumista discover` / `legumista ideate`) talks the
**OpenAI-compatible Chat Completions API directly** — no proxy, no external runtime:

```
  legumista discover / ideate  (Python)
          │  POST /v1/chat/completions   (OpenAI format)
          ▼
  the endpoint in legumista.yml `llm`
     • OpenRouter   https://openrouter.ai/api/v1   (default; free demo model)
     • local ollama http://localhost:11434/v1      (no proxy, no key)
     • any OpenAI-compatible server
```

Interactive, tool-using research runs on a native Python **agentic harness**
(`legumista_agent/`) with keyless in-package tools (no MCP required) — see
[Agentic tool-using research](#agentic-tool-using-research).

## Quickstart

Install the tool once (Python ≥3.11); it provides the `legumista` command:

```bash
pip install -e .          # editable, from a clone; or `pip install .` for a global install
```

Then pick a path. **The model story is simple: by default everything points at
[OpenRouter](https://openrouter.ai) with a free demo model (set one API key and go); for
real runs, point `llm` at a stronger hosted model or a local ollama server (no key, no
proxy).** Details in [Model configuration](#model-configuration).

**(a) Try the demo** — the bundled *Lupinus* project runs on a clean clone:

```bash
pip install -e .
cd examples/lupinus
export OPENROUTER_API_KEY=sk-or-...    # the demo defaults to OpenRouter's free model
legumista discover                     # Phase 1 crawl + Phase 2 synthesis
legumista ideate                       # Phase 3 ideation + Phase 3b expansion
```

The free model is tiny — enough to watch the pipeline work end to end, but synthesis/idea
quality scales with the model. `examples/lupinus/legumista.yml` carries a commented
local-ollama alternative for serious runs.

**(b) Start your own topic** — scaffold a fresh project, point it at your field:

```bash
legumista init ~/topics/mytopic        # writes just legumista.yml + agent_state.json
cd ~/topics/mytopic
# edit legumista.yml (name/subject/scope/llm); add 3–15 verified anchor DOIs to
# agent_state.json (both anchor_dois and pending_dois)
export OPENROUTER_API_KEY=sk-or-...     # or edit llm for ollama / another provider
legumista discover
```

`init` drops a bare config; steering (`context_inputs/*.md`), the Phase-3 goal
(`ideation-goal.md`), and prompt overrides (`prompts/*.md`) are all optional — the
pipeline falls back to packaged/empty defaults and runs fine without them (see PIPELINE.md
§"Adapting the pipeline to a new research topic").

The autonomous pipeline needs only Python + the configured endpoint — no conda, no
proxy, no MCP servers. The agentic harness ships native, keyless tools (scholarly
search, `read_paper`, NCBI `datasets`/EDirect, web search, grep/read).

**Something not working?** The two usual culprits are an unreachable endpoint
(connection refused / timed out — check `base_url`, key, or that ollama is running) and a
generation that times out on a big corpus (raise `llm.timeout`). Run `legumista status`
to see whether the configured endpoint is reachable; see PIPELINE.md §"Troubleshooting".

## The `legumista` CLI

The autonomous pipeline is driven by a single Typer CLI (`legumista`):

```bash
pip install -e .              # editable (dev, in-repo); or `pip install .` for a
                              # global install — bundled prompts/example ship as
                              # package data, so `legumista init` works from anywhere
```

Configure the model endpoint once (see [Model configuration](#model-configuration)),
then the whole arc is two commands:

```bash
export OPENROUTER_API_KEY=sk-...   # or point legumista.yml `llm` at local ollama
legumista discover                # Phase 1 + 2: crawl the citation graph, then synthesize
legumista ideate                  # Phase 3 (+ 3b): grow ideas, then expand to proposals
```

| Command | Does |
|---|---|
| `legumista discover` | crawl + synthesis (Phase 1+2) |
| `legumista ideate` | ideation loop + report + expansion (Phase 3/3b) |
| `legumista crawl` | Phase 1 only (`-s content`/`structural`, `--run`) |
| `legumista merge` | union namespaced crawls |
| `legumista review` | Phase 2 only (`--angle`) |
| `legumista digest` / `legumista report` | rebuild digests (no model) |
| `legumista expand` | Phase 3b only (`--include-pool`) |
| `legumista research "<topic>"` | agentic tool-using research via native tools |
| `legumista mcp` | serve the native toolset over MCP (FastMCP; `-t stdio`/`http`) |
| `legumista reset crawl` / `legumista reset ideation` | clear artifacts (`--hard` / `--dry-run`) |
| `legumista init [dir]` | scaffold a new project (bare `legumista.yml` + `agent_state.json`) |
| `legumista status` | project + corpus/idea counts + endpoint health |

The model endpoint is set in `legumista.yml `llm`` — see
[Model configuration](#model-configuration). `legumista status` shows the active
endpoint/model and whether it's reachable.

Each research topic is its **own project directory** (`legumista.yml` + data); one
installed `legumista` drives many. It finds the project git-style (nearest `legumista.yml`
up from the cwd) or via `-C <dir>`. Start a new topic with `legumista init ~/topics/foo`
(see PIPELINE.md §"Adapting the pipeline to a new research topic"). The repo root is the
tool plus a clean scaffold; the runnable demo project is [`examples/lupinus/`](examples/lupinus/).

Every command takes `--help`; budgets are flags (e.g. `legumista discover --max-loops 40
-s structural`) or the same env vars as before. The two-metric workflow:

```bash
legumista crawl -s content    --run content
legumista crawl -s structural --run structural
legumista merge content structural        # -> library_manifest.json (+ Venn)
legumista review
```

The `legumista` CLI is the single entry point — every phase, plus the agentic
`research` command, runs through it (there are no shell-script runners to maintain).

## Model configuration

The autonomous pipeline calls an **OpenAI-compatible `/chat/completions` endpoint**
directly (standard OpenAI Chat Completions format). Configure it in `legumista.yml`
under `llm` — or with `LLM_*` env overrides (e.g. `LLM_TIMEOUT` mirrors `llm.timeout`).
`legumista status` shows the active endpoint/model and whether it's reachable.

```yaml
llm:
  base_url: https://openrouter.ai/api/v1            # OpenAI-compatible endpoint
  model: meta-llama/llama-3.2-3b-instruct:free      # a model the endpoint serves
  small_model: ""                                   # optional; blank = use `model`
  api_key_env: OPENROUTER_API_KEY                   # env var holding the API key
  temperature: 0.3
  max_tokens: 0        # 0 = provider default (spec: sent as max_completion_tokens)
  timeout: 3600        # seconds; large-corpus synthesis can run long, esp. on big models
  json_mode: false     # true => request response_format {"type":"json_object"}
```

**Default — OpenRouter, free demo model.** Out of the box the pipeline points at
[OpenRouter](https://openrouter.ai) with the free
[`meta-llama/llama-3.2-3b-instruct:free`](https://openrouter.ai/meta-llama/llama-3.2-3b-instruct:free)
(3B, 131K ctx, $0). Just set the key and go — cheap to demo, no local model needed:

```bash
export OPENROUTER_API_KEY=sk-or-...
legumista discover
```

(It's a tiny model, so JSON/idea quality is modest — fine for a demo. Browse other
free models at `openrouter.ai/models?max_price=0`; any OpenAI-compatible provider
works by changing `base_url`/`model`/`api_key_env`.)

**Local ollama — no proxy, no key.** ollama serves the OpenAI API natively at
`:11434/v1`, so the pipeline talks to it directly. Point `llm` at it:

```bash
ollama serve                 # listens on :11434
ollama pull qwen3:32b        # `ollama list` shows your tags
```
```yaml
llm:
  base_url: http://localhost:11434/v1    # ollama's built-in OpenAI endpoint
  model: qwen3:32b                        # your `ollama list` tag
  api_key_env: OLLAMA_API_KEY            # unused by ollama; leave the env var unset
  timeout: 3600
  json_mode: false          # set true if your model reliably emits valid JSON
```

The demo (`examples/lupinus/legumista.yml`) ships a commented local-ollama block right
below its OpenRouter default — uncomment it to switch.

**Optional `json_mode`.** When `true`, JSON-returning calls (the crawl judge and
ideation) send `response_format: {"type":"json_object"}`. Leave it `false` for
models/providers that don't support structured outputs — the prompts already ask for
fenced JSON and the pipeline extracts it either way.

## Agentic tool-using research

Beyond the deterministic pipeline, `legumista research "<topic>"` runs an **agent** that
uses tools to answer a question:

```bash
legumista research "What does genomic evidence show about white lupin phosphorus acquisition?"
```

It's a compact, self-contained agentic harness (`legumista_agent/`) — no proxy, no
external services:

- **`legumista_agent/loop.py`** — the tool-call loop: send `messages` + `tools` to the
  OpenAI `/chat/completions` endpoint (`config llm`); when the model returns
  `tool_calls`, run them, feed the results back as `role:"tool"` messages, and repeat
  until a final answer (bounded by `LEGUMISTA_AGENT_MAX_TURNS`, default 8).
- **Native tools (`legumista_agent/tools_native.py`, `tools_local.py`)** — in-package,
  keyless, no MCP required: `openalex_search`/`openalex_by_doi` (DOI discovery),
  `crossref_search`, `arxiv_search`, `europepmc_search`, `biorxiv_search` (preprints),
  `paper_search` (fan-out + dedupe), `read_paper` (OA PDF → text via `pypdf`),
  `ncbi_datasets`/`edirect` (NCBI genomes/assemblies/SRA via the local NCBI CLIs),
  `web_search` (DuckDuckGo), and `grep`/`read_file`/`web_fetch`. Documented for the model in
  [`prompts/tools_native.md`](legumista_assets/prompts/tools_native.md), appended to the
  system prompt.
- **Bioinformatics tools (`legumista_agent/tools_pysam.py`)** — htslib access to indexed
  genomics files via `pysam`. `samtools` and `bcftools` are **general dispatchers** — pass
  an argv list (subcommand + flags), the same convention as `ncbi_datasets` — so almost the
  whole samtools/bcftools suite is available (view, sort, index, depth, coverage, stats,
  call, norm, query, …). Plus read-only helpers `fasta_fetch` and `tabix_query`, and a
  `tabix_index` builder. Path arguments are workspace-sandboxed, URL arguments SSRF-checked;
  regions are samtools-style (1-based inclusive). **Read operations run by default; write
  operations (sort/index/call/tabix_index, or any `-o` output) require the `read_write`
  permission** — pass `--allow-write` to `legumista research` or `legumista mcp`.
- **LIS Data Store tools (`legumista_agent/tools_lis.py`)** — systematic access to
  [data.legumeinfo.org](https://data.legumeinfo.org): `lis_find` (discover species, data
  types and collections, with each collection's `publication_doi`), `lis_files` (which of a
  collection's files are randomly accessible over HTTP, and the exact call to read them —
  the store's directory listing hides the `.fai`/`.tbi` siblings), and `lis_gene` (an exact
  gene/mRNA ID → locus + ready-made `fasta_fetch`/`tabix_query` calls). They **resolve
  rather than retrieve**: the URLs they return are read by the bioinformatics tools above,
  and the DOIs by `openalex_by_doi`/`read_paper`. Read-only, no disk state; base URL
  overridable with `LEGUMISTA_LIS_BASE_URL`.
- **`legumista_agent/mcp_client.py`** — *optional*: any MCP servers in
  [`.mcp.json`](.mcp.json) are discovered via the MCP SDK (bundled with FastMCP) and added
  alongside the native tools (`mcp__<server>__<tool>`).
- **`legumista_agent/permissions.py`** — a per-tool allow/deny gate (`read_only` /
  `allow_all` / `deny_all`); `research` uses `allow_all` (trusted, read-only tools).
- **System prompt** — the same shared base as every other phase (`system-prompt.md`,
  shipped with the package) + the native-tools spec. `research` behaves exactly like the
  pipeline's model calls, just with the tool loop enabled.

Output lands in `reviews/<ts>_research/` (`answer.md`, a DOI `seed.md` for
`legumista discover --seed`, and the full `transcript.json`).

**Requirements:** just the model endpoint. The native tools are stdlib + `ddgs` +
`pypdf` and need no API keys or MCP servers; `ncbi_datasets`/`edirect` additionally
use the NCBI CLIs if you've installed them. Everything else ships in the one package —
`pip install legumista` includes the FastMCP server runtime, the pysam genomics tools, and
the client SDK for plugging your own MCP servers into `.mcp.json`.

The same agentic harness backs the pipeline: `discover`/`review`/`ideate` run their model
step as a bounded tool-loop (up to `agent.max_turns`, default 8), so the model can verify
and enrich with tools before answering. The deterministic mechanics (OpenAlex citation
walk, JSON verdicts, commits) are unchanged.

### Serving the tools over MCP

The same native toolset is available to **any** Model Context Protocol client (Claude
Desktop, an IDE, another agent), not just `legumista research`. `legumista mcp` — one
subcommand of the single `legumista` app — starts a spec-compliant
[FastMCP](https://gofastmcp.com/servers/server) server
([`legumista_agent/mcp_server.py`](legumista_agent/mcp_server.py)) that exposes every
native tool (including the pysam genomics tools) with its original JSON-schema and a
`readOnlyHint` annotation reflecting whether the tool can write:

```bash
pip install legumista              # everything's included — nothing extra to add
legumista mcp                      # stdio transport (how clients spawn a server)
legumista mcp -t http --port 8000  # long-running HTTP endpoint at /mcp
legumista mcp --allow-write        # also permit genomics write ops (sort/index/call/…)
```

Unlike the pipeline commands, `legumista mcp` needs no project and doesn't care where it's
launched — just run it. (Local-file tools resolve within the launch directory; pass `-C` to
pin a different root.) The MCP server has no permission gate of its own, so `--allow-write`
is the sole switch for write operations; without it the write-capable genomics tools run
reads only and refuse writes. This is the mirror image of `.mcp.json`: that pulls *other*
servers' tools **in**; the legumista server pushes *legumista's* tools **out**.

#### Wiring it into an MCP client

legumista is one command, so the MCP server is just `legumista mcp` — the same everywhere.
Once published to PyPI, any client can launch it with **`uvx`** (the
[uv](https://docs.astral.sh/uv/) runner, the Python analogue of `npx`) with no manual
install. Drop this into the client's standard `mcpServers` config (Claude Desktop, Cursor,
Claude Code, OpenAI Agents SDK, …):

```jsonc
{
  "mcpServers": {
    "legumista": {
      "command": "uvx",
      "args": ["legumista", "mcp"]
    }
  }
}
```

`uvx legumista mcp` installs legumista into a throwaway environment and runs the server —
no extras to specify, since everything ships in the one package. Add `"--allow-write"` to
the `args` to permit genomics writes. Some GUI clients don't see `uvx` on `PATH` — give the
absolute path (`which uvx`) if so. From a source checkout it's simply `legumista mcp` after
`pip install -e .`.

#### Publishing to the official MCP Registry

Metadata for the [official MCP Registry](https://registry.modelcontextprotocol.io) lives
in [`server.json`](server.json) (reverse-DNS name `io.github.legumeinfo/legumista`,
pointing at the PyPI package). The registry is a *metaregistry* — it stores that manifest,
not the code, and verifies namespace ownership via the `<!-- mcp-name: … -->` marker in this
README (which becomes the PyPI description). To publish a release:

```bash
uv build && uv publish                       # 1. push the package to PyPI
mcp-publisher login github                   # 2. verify the io.github.legumeinfo namespace
mcp-publisher publish                        # 3. submit server.json to the registry
```

Keep the `version` in `server.json` in step with `pyproject.toml`. A `Dockerfile` (with the
`io.modelcontextprotocol.server.name` label for OCI-image verification) is provided for the
container distribution channel.

## Autonomous pipeline (discover → synthesize → ideate)

The full arc — Phase 1 citation-graph crawl, Phase 2 synthesis, Phase 3 ideation,
and the Phase 3b write-up — is driven by the `legumista` CLI and documented in full in
**[`PIPELINE.md`](PIPELINE.md)** (design rationale, per-phase behaviour, the
content-vs-structural distance metrics, budgets, and troubleshooting). In brief:

```bash
legumista discover          # Phase 1 crawl + Phase 2 synthesis
legumista ideate            # Phase 3 ideation + Phase 3b expansion
```

**Two-metric discovery** — run the complementary `content` and `structural` crawls
as independent namespaces, then union them:

```bash
legumista crawl -s content    --run content
legumista crawl -s structural --run structural
legumista merge content structural     # -> library_manifest.json (+ Venn summary)
legumista review
```

Python does all deterministic mechanics (OpenAlex edges, scope-filtering, distance
scoring, PDF download, committing the ledgers); the model is only ever a bounded
judge/generator over inlined text, so a malformed reply skips a loop instead of
corrupting state. Anti-drift drops candidates below `CRAWL_MIN_SIMILARITY` or beyond
`CRAWL_MAX_DEPTH` hops from the anchors. Budgets are CLI flags or `CRAWL_*` /
`IDEATE_*` env vars.

## Scheduled / cron

The CLI is non-interactive, so any phase runs on a schedule. Ensure the model
endpoint is configured (`OPENROUTER_API_KEY` exported, or ollama running):

```cron
# 2am daily: refresh the corpus and its review
0 2 * * * cd /path/to/project && /path/to/venv/bin/legumista discover >> reviews/cron.log 2>&1
```

## Configuration map

| Concern | File | Key |
|---|---|---|
| **Model endpoint (pipeline)** | `legumista.yml` | `llm` base_url / model / api_key_env / json_mode |
| Project identity (topic) | `legumista.yml` | name, subject, scope, years, `lexical`, `ideation` (see `legumista.example.yml`) |
| Phase prompt wording | packaged default; optional `prompts/<name>.md` in the project | drop one in to override a single template (`{{PLACEHOLDER}}` slots) |
| Tool-loop turns | `legumista.yml` `agent` / env | `max_turns` (default 8) / `LEGUMISTA_AGENT_MAX_TURNS` |
| MCP tool servers (optional) | `.mcp.json` | empty by default; add your own servers |
| System prompt (all model calls) | packaged default; optional `system-prompt.md` in the project dir, or `$LEGUMISTA_SYSTEM_PROMPT` | scientist persona + evidential-integrity rules + tool doctrine (ships with the package) |
| Crawl core / anchor set | `agent_state.json` | `anchor_dois` (distance metric reference) |
| Crawl seeds + frontier | `agent_state.json` | `pending_dois` |
| Collected-paper ledger | `library_manifest.json` | (written by orchestrator) |
| Live crawl steering (optional) | `context_inputs/*.md` | re-read every loop; absent = no extra steering |
| Anti-drift gate | env / `orchestrator.py` | `CRAWL_MIN_SIMILARITY`, `CRAWL_MAX_DEPTH` |
| Crawl budgets | env / `orchestrator.py` | `CRAWL_MAX_LOOPS`, … |
| Ideation goal (Phase 3, optional) | `ideation-goal.md` | what to discover (you author it); absent = packaged default |
| Matured-idea ledger | `ideas/ideas_manifest.json` | (written by ideation.py) |
| Ideation budgets | env / `ideation.py` | `IDEATE_MAX_LOOPS`, `IDEATE_MAX_IDEAS`, … |

## Caveats worth knowing

- **Small models = modest output quality.** A 3B free model (the demo default) or a
  local ~27B are far less consistent than a frontier model. Keep `json_mode` off
  unless the model reliably emits valid JSON; the pipeline extracts fenced JSON
  either way.
- **Context window (local ollama).** Set a generous context (e.g. a Modelfile with
  `PARAMETER num_ctx 32768`) or a large inlined digest will be truncated.
- **The pipeline sends only text.** `discover`/`ideate` never read files or send
  images — the digest/synthesis is inlined as text — so there's nothing to
  misconfigure around PDFs/multimodal.

## License

MIT — see [`LICENSE`](LICENSE).
