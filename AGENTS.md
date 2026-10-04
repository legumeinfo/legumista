<!-- Repository guide for AI coding assistants and contributors. -->

# Legumista — repository guide

**Legumista is an MCP server for legume genomics.** It serves a resident snapshot (downloaded at startup) of the
[LIS Data Store](https://data.legumeinfo.org) catalog, InterMine queries against the LIS
mines, scholarly literature search and full-text read, NCBI datasets/EDirect, and a
samtools/bcftools/tabix suite — to any MCP-speaking client. `legumista mcp` is the only
command. See [`README.md`](README.md) for setup and the tool list.

The repository used to also hold an agentic research pipeline (crawl → corpus → ideation)
that drove these same tools from an internal loop. That is gone; the tools now have
exactly one consumer, the MCP server. If you find a reference to `legumista research`, a
`legumista.yml` project file, or a phase prompt, it is stale — delete it.

## Layout

- `legumista_cli.py` — the `legumista` entry point. One command, `mcp`.
- `config.py` — three things the server needs: `WORKSPACE` (the sandbox root for local
  file arguments), `contact_email()` (the scholarly-API polite-pool mailto), and
  `tools_spec()` (the served MCP `instructions`). All environment-driven; no config file.
- `legumista_agent/` — the tools, one module per family:
  - `tool.py` — the `Tool` dataclass every tool is built from (name, description,
    JSON-schema parameters, `read_only`, async `run(args)` returning `str` or
    `results.ToolOutput`, optional per-call `writes(args)` classifier).
  - `mcp_server.py` — the FastMCP bridge. Assembles every family's tools and serves them,
    sending failures with `isError: true`.
  - `results.py` — result conventions every tool uses: `fail`, `count_phrase`,
    `SourceSummary`.
  - `pubstatus.py` — DOI retraction and existence status (Crossref, then doi.org).
  - `tools_verify.py` — `verify_ids`.
  - `tools_catalog.py` — owns the catalog (`lis_survey`, `lis_lineage`, and the
    `CatalogController` the LIS tools read through).
  - `catalog_source.py` — where `catalog.json` comes from: the published URL, the
    on-disk cache, conditional GETs, and the background poller.
  - `webhook.py` — `POST /catalog/refresh`, authenticated with GitHub's HMAC scheme.
  - `tools_lis.py`, `tools_mine.py`, `tools_native.py`, `tools_local.py`, `tools_pysam.py`.
  - `_pysam_worker.py` — runs one htslib operation (a `samtools`/`bcftools` call, or a
    `fasta_fetch`/`tabix_query` read) in a child process for `tools_pysam` (standalone:
    stdlib + pysam only).
  - `egress_proxy.py` — the loopback proxy every worker's HTTP goes through; refuses
    non-public destinations on every connection, redirect hops included.
- `legumista_assets/prompts/tools_native.md` — the tool-use doctrine served as the MCP
  server's `instructions`. Package data; editing it changes what every client is told.
- `compose.yaml` / `.env.example` — the deployment path: builds the image from the
  checkout and runs it as an HTTP server. `.env` is gitignored (it holds the webhook
  secret); the compose file is meant to be used unedited.
- **The catalog is not in this repository.** It is a build artifact of
  [LIS-autocontent](https://github.com/legumeinfo/LIS-autocontent)'s `populate-catalog`,
  published as a release asset and downloaded at startup (`LEGUMISTA_CATALOG_URL`), then
  cached. `LEGUMISTA_CATALOG_PATH` pins the server to a local file — useful offline. The
  pin is never discovered from the cwd or checkout root: by default those are the
  workspace the write tools can write into.

## Working in this repository

- **Every tool is a `Tool`.** Adding one means appending to the relevant `*_tools()`
  factory; the MCP server picks it up with no registration step. `read_only` becomes the
  MCP `readOnlyHint` annotation, so set it honestly.
- **Read vs write.** Read-only is the default everywhere. Write-capable tools declare a
  per-call `writes(args)` classifier, because a dispatcher like `samtools` reads on `view`
  and writes on `sort`. The MCP server has no permission gate, so `--allow-write` is the
  only control: without it, write subcommands fail closed. For the dispatchers a read is
  an **allowlist** (`_SAM_SPEC`/`_BCF_SPEC` in `tools_pysam.py`: each read subcommand's
  side-effect-free options, from the bundled CLI source); anything not listed is a write.
  Never turn it back into a denylist of output flags — `view -U`, `fastq -1` and getopt
  abbreviations like `--out` all slipped past one.
- **Local paths are sandboxed.** Every local file argument passes `_sandbox_path`
  (`tools_native.py`), which confines it to `config.WORKSPACE`, refuses dotfiles,
  secret-like names and the server's own catalog/cache, and rewrites to absolute. Its
  consumer is `tools_pysam`. Do not bypass it when adding a tool that takes a path.
  htslib's `##idx##` syntax hides a second path inside one token, so it is refused.
  Every htslib operation runs in a `_pysam_worker.py` child process, with a timeout, a
  file-size rlimit, and htslib's scratch directory as cwd, so a bare name the guard
  could not recognise as a path never resolves against the workspace or the launch
  directory. That cwd is keyed by the call's remote URLs (`_worker_cwd`), because htslib
  reuses a cached index by filename alone. **Never open a remote file with pysam in the server process:** libcurl
  follows redirects and resolves hosts itself, so only the egress proxy, which the
  workers' environment names, can keep it off private addresses.
- **No metadata HTTP to data.legumeinfo.org.** The `lis_*` tools answer from
  `catalog.json`, not by crawling the store. The two remaining reads there are *data*
  (a gene-models BED, a synonym file), not metadata. Keep it that way: a metadata question
  the catalog cannot answer is a catalog bug, to be fixed in LIS-autocontent.
- **A failure is not a finding.** Return `results.fail(...)` when a tool could not
  answer. Never catch an exception and fall through to an empty or "none found" answer.
  `grep -rn -A1 --include='*.py' -E '^\s*except\b.*:' legumista_agent/ | grep -E
  '^[^:]+-[0-9]+-\s*pass\s*$'` lists every swallowed exception; the only one left is the
  CA-bundle fallback in `tools_pysam.py`, a configuration default rather than an answer.
- **Never present a cap as a total.** Any list that can be truncated is described with
  `results.count_phrase`.
- **Names go through `resolve_taxon`.** No tool splits a taxon string by hand.
- **The instructions name every tool.** `tests/test_assets.py` enforces coverage and a
  size budget, so a new tool needs a line in `tools_native.md`.
- **Producers derive, consumers are dumb.** Anything that can be computed once at
  catalog-build time belongs in LIS-autocontent, not here — LIS-autocontent's output feeds
  other projects too, so deriving it here would duplicate the logic in the wrong place.
- **A refresh must never be able to take the tools down.** `tools_catalog.refresh()`
  downloads, validates, and builds a new controller *before* rebinding the live one; every
  failure path leaves the previous catalog serving. If you touch that ordering, the tests
  that guard it are `test_a_bad_publish_does_not_clobber_a_good_cache` and
  `test_a_failed_refresh_keeps_serving_the_previous_catalog`.
- **The refresh webhook fails closed.** No `LEGUMISTA_WEBHOOK_SECRET`, no route. Keep it
  that way: it is the only endpoint that acts on an unauthenticated request's say-so, and
  the signature check must stay constant-time.
- **One command, one package.** Everything is a subcommand of `legumista` (the only
  `[project.scripts]` entry) — there is deliberately no separate server binary. A client
  launches it via `uvx legumista mcp`, described in [`server.json`](server.json) for the
  official MCP Registry (name `io.github.legumeinfo/legumista`, verified by the
  `mcp-name:` marker in the README); the `Dockerfile` covers the container channel and
  additionally bakes in the NCBI CLIs and DSCensor, and `compose.yaml` runs it.
  `tests/test_distribution.py` keeps these in sync — notably `server.json`'s version must track `pyproject.toml`, so bump
  both on a release.

## Contributing

- **Dev install:** `pip install -e . pytest` (Python ≥3.11).
- **DSCensor** is not on PyPI yet. Point `LEGUMISTA_DSCENSOR_PATH` at a checkout of the
  `legumista-interop` branch of `legumeinfo/microservices` (the `dscensor/` subdirectory);
  without it the `lis_*` tools report the catalog as unavailable rather than failing.
- **Tests:** `python -m pytest -q` (suite in `tests/`). Every test is offline, and
  `tests/conftest.py` enforces it: a test that opens a TCP connection or resolves a
  hostname fails.
- **CI:** `.github/workflows/ci.yml` runs the tests on Python 3.11 and 3.12, against
  FastMCP 3.x and 4.x, with a DSCensor checkout, for every push and pull request. Keep
  them green.
- **Evaluations:** `evals/` measures answer quality with a real model (see
  `evals/README.md`). It is not part of the test suite.
- Licensed MIT (see [`LICENSE`](LICENSE)).
