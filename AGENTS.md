<!-- Repository guide for AI coding assistants and contributors. -->

# Legumista — repository guide

**Legumista is an MCP server for legume genomics.** It serves a bundled snapshot of the
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
    JSON-schema parameters, `read_only`, async `run(args) -> str`, optional per-call
    `writes(args)` classifier).
  - `mcp_server.py` — the FastMCP bridge. Assembles every family's tools and serves them.
  - `tools_catalog.py` — owns the catalog (`lis_survey`, `lis_lineage`, and the
    `CatalogController` the LIS tools read through).
  - `tools_lis.py`, `tools_mine.py`, `tools_native.py`, `tools_local.py`, `tools_pysam.py`.
- `legumista_assets/prompts/tools_native.md` — the tool-use doctrine served as the MCP
  server's `instructions`. Package data; editing it changes what every client is told.
- `catalog.json` — the build artifact the `lis_*` tools read. Produced by
  [LIS-autocontent](https://github.com/legumeinfo/LIS-autocontent)'s `populate-catalog`
  from `datastore-metadata`; read here through DSCensor's `CatalogController`. It is data,
  not code — regenerate and replace it rather than patching it by hand.

## Working in this repository

- **Every tool is a `Tool`.** Adding one means appending to the relevant `*_tools()`
  factory; the MCP server picks it up with no registration step. `read_only` becomes the
  MCP `readOnlyHint` annotation, so set it honestly.
- **Read vs write.** Read-only is the default everywhere. Write-capable tools declare a
  per-call `writes(args)` classifier, because a dispatcher like `samtools` reads on `view`
  and writes on `sort`. The MCP server has no permission gate, so `--allow-write` is the
  only control: without it, write subcommands fail closed.
- **Local paths are sandboxed.** Every local file argument passes `_sandbox_path`
  (`tools_native.py`), which confines it to `config.WORKSPACE`, refuses dotfiles and
  secret-like names, and rewrites to absolute so the process cwd is irrelevant. Its
  consumer is `tools_pysam`. Do not bypass it when adding a tool that takes a path.
- **No metadata HTTP to data.legumeinfo.org.** The `lis_*` tools answer from
  `catalog.json`, not by crawling the store. The two remaining reads there are *data*
  (a gene-models BED, a synonym file), not metadata. Keep it that way: a metadata question
  the catalog cannot answer is a catalog bug, to be fixed in LIS-autocontent.
- **Producers derive, consumers are dumb.** Anything that can be computed once at
  catalog-build time belongs in LIS-autocontent, not here — LIS-autocontent's output feeds
  other projects too, so deriving it here would duplicate the logic in the wrong place.
- **One command, one package.** Everything is a subcommand of `legumista` (the only
  `[project.scripts]` entry) — there is deliberately no separate server binary. A client
  launches it via `uvx legumista mcp`, described in [`server.json`](server.json) for the
  official MCP Registry (name `io.github.legumeinfo/legumista`, verified by the
  `mcp-name:` marker in the README); the `Dockerfile` covers the container channel and
  additionally bakes in the NCBI CLIs and DSCensor. `tests/test_distribution.py` keeps
  these in sync — notably `server.json`'s version must track `pyproject.toml`, so bump
  both on a release.

## Contributing

- **Dev install:** `pip install -e . pytest` (Python ≥3.11).
- **DSCensor** is not on PyPI yet. Point `LEGUMISTA_DSCENSOR_PATH` at a checkout of the
  `legumista-interop` branch of `legumeinfo/microservices` (the `dscensor/` subdirectory);
  without it the `lis_*` tools report the catalog as unavailable rather than failing.
- **Tests:** `python -m pytest -q` (suite in `tests/`). Every test is offline — network
  and PDF entry points are stubbed. Keep it that way.
- **CI:** `.github/workflows/ci.yml` runs the tests on Python 3.11 and 3.12 for every push
  and pull request. Keep them green.
- Licensed MIT (see [`LICENSE`](LICENSE)).
