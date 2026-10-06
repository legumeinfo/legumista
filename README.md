<!-- mcp-name: io.github.legumeinfo/legumista -->

![Legumista bean mascot](./legumista_assets/legumista.png)

# Legumista — an MCP server for legume genomics

**Legumista** gives a model working access to legume genomics. It serves 33 tools,
read-only by default, over the [Model Context Protocol](https://modelcontextprotocol.io): a resident
snapshot of the [LIS Data Store](https://data.legumeinfo.org) catalog, InterMine queries
against the LIS mines, scholarly literature search and full-text retrieval, NCBI
datasets/EDirect, and a samtools/bcftools/tabix suite that reads indexed genomics files
over HTTP without downloading them.

Point any MCP client at it — Claude Desktop, Claude Code, an IDE, another agent — and ask
questions like *"does chickpea have a counterpart of this soybean gene, and what's the
evidence?"* The model does the reasoning; legumista makes sure every answer traces back to
a real accession, a real file, or a real DOI.

---

## Start here: the hosted server

**The easiest way to use legumista is the public instance — nothing to install, build, or
keep up to date:**

```
https://mcp.legumeinfo.org
```

Add it to any MCP client's `mcpServers` config:

```jsonc
{
  "mcpServers": {
    "legumista": {
      "url": "https://mcp.legumeinfo.org"
    }
  }
}
```

Or, for Claude Code, in one command:

```bash
claude mcp add --transport http legumista https://mcp.legumeinfo.org
```

### Running your own

Everything below covers self-hosting: a container, a local checkout, or an internal
deployment with its own catalog.

```bash
cp .env.example .env            # optional — defaults are fine
docker compose up -d --build    # http://127.0.0.1:8000/mcp
```

A `pip install` gives you the server and the tools, but DSCensor is not on PyPI yet — see
[Requirements](#requirements). The catalog is downloaded on startup, so there is nothing to
fetch by hand.

---

## Why it's built this way

**The catalog is resident, not crawled.** Every `lis_*` tool answers from a `catalog.json`
held in memory — ~1,048 collections, 6,316 files, 938 DOIs, 70 taxa — rather than walking
`data.legumeinfo.org` over HTTP. A question like *"which soybean assemblies exist and which
of their files are indexed?"* is a dictionary lookup, not a dozen round trips, and it works
the same when the store is slow or unreachable. The catalog is built by
[LIS-autocontent](https://github.com/legumeinfo/LIS-autocontent) from `datastore-metadata`
and read here through DSCensor's `CatalogController`.

**The catalog is data, and it ships on its own schedule.** It is not vendored in this
repository and not baked into the image: the server downloads it at startup and caches it,
so republishing a catalog reaches running servers without a commit, a release, or a
redeploy. See [Keeping the catalog current](#keeping-the-catalog-current).

**Random access instead of downloads.** The store publishes `.fai`, `.tbi`, `.csi` and
`.bai` siblings for a large share of its files, and htslib can range-request against them.
`fasta_fetch` pulls one protein out of a remote FASTA; `tabix_query` pulls the features in
one interval out of a remote GFF3. The catalog records which files are indexed, so the
model knows what it can reach cheaply before it tries.

**A map, not the territory.** The full catalog is roughly 557,000 tokens — far too much to
hand a model. Instead the server appends a ~1,400-token *projection* of it to its MCP
`instructions`: which genera exist, what each species has, what soybean is called here.
That answers the exploratory questions that would otherwise each cost a tool call, and it
is the only place **absence** is visible — a model can see that *Medicago* has no synteny
collection of its own, rather than concluding it from an empty result.

**Provenance is a first-class result.** Every LIS collection carries a `publication_doi`
by specification, so `lis_lineage` traces a dataset back through what it was derived from
and returns every paper the result depends on — which the literature tools then resolve
and read.

**A failure is never a finding.** A tool that could not answer says so with `isError: true`
and an `error:` line. A partial answer is labelled `PARTIAL RESULTS`, a route that did not
run is `NOT CHECKED`, and a capped list says "showing N of M". "No match" appears only
when every route that could have matched actually ran. Language models repeat what their
tools tell them, so the server never words an outage as an absence.

---

## The toolset

Started with `--allow-write`, `samtools`, `bcftools` and `tabix_index` also permit their
write operations. Without it (the default) they run reads only and refuse writes. A read
is an allowlist, not a guess: a read subcommand used with only the options known to have
no filesystem side effect. Any other option — `-o`, `view -U`, `fastq -1`,
`--write-index`, one the list does not know — makes the call a write. Each
`samtools`/`bcftools` call runs in its own child process with a time limit and a per-file
size limit, and reaches the network only through a built-in egress proxy. The proxy
refuses any destination that is not a public address — checked on every connection,
so a redirect from a public URL to a private one (a cloud metadata endpoint, say) is
refused too — and connects to the exact address it checked. Any public host stays
readable. Nothing is ever written to a remote host: an output URL is refused, and the
proxy caps what a connection may send far below any useful upload.

### LIS Data Store — the resident catalog

| Tool | What it answers |
| --- | --- |
| `lis_survey` | What exists across the whole store: genera, collection counts, types |
| `lis_find` | Discover collections — genomes, annotations, diversity, GWAS, synteny |
| `lis_files` | A collection's files, and which are randomly accessible over HTTP |
| `lis_gene` | A gene's locus and description, plus ready-to-run calls for its protein/CDS and neighbourhood; or, given a selector, one row per gene for a whole selection |
| `lis_synteny` | Syntenic blocks and whole-genome alignments between assemblies |
| `lis_lineage` | What a collection was derived from, and every DOI the result depends on |

### LIS InterMine — annotation and expression

| Tool | What it answers |
| --- | --- |
| `legumemine_gene_symbol` | Resolve a gene symbol to identifiers |
| `legumemine_gene_proteins` | Protein records: identifier, length, molecular weight |
| `legumemine_gene_families` | Gene-family assignments |
| `legumemine_gene_ontology` | GO and other ontology annotations |
| `legumemine_gene_expression` | Expression across samples, highest first |
| `legumemine_gene_family_members` | A gene's homologs across species via its family, optionally one target species (homology, not an orthology call) |
| `lis_trait_qtls` | QTLs for a trait: linkage group, LOD, marker R², source study |
| `lis_trait_gwas` | GWAS associations for a trait, most significant first |
| `lis_marker_position` | A marker's physical position on each assembly that carries it |

### Genomics files (pysam/htslib)

| Tool | What it does |
| --- | --- |
| `samtools` | Any samtools subcommand on SAM/BAM/CRAM/FASTA |
| `bcftools` | Any bcftools subcommand on VCF/BCF |
| `fasta_fetch` | Extract a subsequence from an indexed FASTA — read-only by construction |
| `tabix_query` | Query a bgzip+tabix table; with no region, list its indexed contigs |
| `tabix_index` | Build a bgzip+tabix index so a local file becomes region-queryable |

### Gene selections — sequence, browser links, defect reports

`extract_features` and `browser_link` take a **gene selector**, one short argument the
server resolves on every call (nothing is stored, so a selector copied from an old chat
still works): `{"ids": [...]}` (up to 200 IDs, symbols or superseded IDs),
`{"region": "contig:start-end"}` or `{"family": "legume.fam3.…", "collection": …}`, with
an optional `translate_to` another annotation (by gene name and synonym files within a
species, through shared gene families across species). Every input is accounted for.

| Tool | What it does |
| --- | --- |
| `extract_features` | Strand-correct protein, CDS, mRNA, gene, upstream, downstream or UTR sequence for a selection, spans taken from the GFF3, headers carrying locus and source. Large output becomes a workspace FASTA + BED with `--allow-write` |
| `browser_link` | A link into an LIS JBrowse 2 instance for a selection or region, genes highlighted on the gene models track; or a dotplot of two genomes. Names come from the catalog's record of each instance's deployed config, so it can say when no instance serves an assembly |
| `report_data_issue` | Files a data defect in the Data Store or a mine as a GitHub issue, after re-reading the field from its source and only with the user's confirmation. Authenticates as a GitHub App; served only with `--allow-report` and an App configured |

### Literature, NCBI and the web

| Tool | What it does |
| --- | --- |
| `paper_search` | Broad search across OpenAlex, Crossref and Europe PMC, fused by rank, with retraction/preprint flags and per-source status |
| `openalex_by_doi` | One work by DOI: full abstract, identifiers, retraction status (OpenAlex + Crossref) |
| `europepmc_search` | Europe PMC field queries (`ORGANISM:`, `SRC:AGR`); preprints flagged |
| `read_paper` | Open-access full text by DOI or URL, page-labelled; pattern hits cite their page |
| `ncbi_datasets` | The NCBI `datasets` CLI — genome, gene and taxonomy data |
| `ncbi_assembly_status` | Does a reference genome exist for this taxon, and how good is it? |
| `sra_runs` | Public sequencing data: runs, platform, spots, bases |
| `edirect` | Raw Entrez — `esearch` piped to `esummary`/`efetch` |
| `web_search` | Keyless open-web search |
| `web_fetch` | Fetch a URL as readable text |

### Checking an answer

| Tool | What it does |
| --- | --- |
| `verify_ids` | Check a draft's DOIs, LIS gene and collection IDs, and GCA_/GCF_ accessions against their sources before answering |

---

## Wiring it into an MCP client

This section is about pointing a client at a server *you* run. For the hosted instance,
one URL is the whole configuration — see
[Start here](#start-here-the-hosted-server).

legumista is one command, so a self-hosted server is just `legumista mcp` — the same
everywhere. Any client can launch it with **`uvx`** (the [uv](https://docs.astral.sh/uv/) runner, the
Python analogue of `npx`) with no manual install. Drop this into the client's standard
`mcpServers` config:

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

Add `"--allow-write"` to `args` to permit genomics writes. Some GUI clients don't see
`uvx` on `PATH` — give the absolute path (`which uvx`) if so. From a source checkout it's
`legumista mcp` after `pip install -e .`.

Failures arrive as tool errors (`isError: true`). A programmatic client built on FastMCP's
`Client.call_tool` raises `ToolError` on them unless it passes `raise_on_error=False`.

### Transports

```bash
legumista mcp                          # stdio — how clients spawn a server
legumista mcp -t http --port 8000      # long-running HTTP endpoint at /mcp
legumista mcp -C /data                 # confine local file arguments to /data
legumista mcp --allow-write            # permit genomics write ops (sort/index/call/…)
legumista mcp --allow-report           # serve report_data_issue (needs a GitHub App)
```

The server needs no project, config file, or particular working directory. Local file
arguments resolve within the launch directory unless `-C` says otherwise.

### Container

`compose.yaml` is the deployment path: it builds the image from this checkout, runs the
server as a long-lived HTTP endpoint on `127.0.0.1:8000`, persists the catalog cache in a
named volume, and health-checks the endpoint.

```bash
cp .env.example .env            # optional; set a webhook secret here if you want one
docker compose up -d --build
docker compose logs -f
docker compose down
```

The image additionally bakes in the external CLIs that four tools shell out to (NCBI
`datasets` and EDirect) and the DSCensor catalog reader, so the whole advertised toolset
works out of the box. The catalog is **not** baked in — it is downloaded at startup — so
the image rebuilds only when the code changes.

Everything tunable lives in `.env`, which is gitignored because the webhook secret belongs
in it; `.env.example` documents every option. An empty `.env` is a working configuration.
The most useful ones:

| `.env` setting | Effect |
| --- | --- |
| `LEGUMISTA_WEBHOOK_SECRET` | Enables `POST /catalog/refresh`. Unset, the route does not exist |
| `LEGUMISTA_BIND` | Host interface to publish on. Default `127.0.0.1` — this machine only |
| `LEGUMISTA_PORT` | Host port. Default `8000` |
| `LEGUMISTA_CATALOG_POLL` | Seconds between freshness checks. Default `86400`; `0` disables |

To pin a catalog rather than downloading one, uncomment the `./catalog.json` mount in
`compose.yaml` and set `LEGUMISTA_CATALOG_PATH=/etc/legumista/catalog.json` in `.env`.

The compose service runs as an unprivileged user on a read-only root filesystem with
every capability dropped. Its only writable paths are the catalog cache volume and two
size-capped tmpfs mounts: `/tmp`, and the workspace `/work`, which is wiped on restart.

Without compose, the equivalent is:

```bash
docker build -t legumista .
docker run --rm -i legumista                                  # stdio; -i keeps stdin open
docker run --rm -d -p 127.0.0.1:8000:8000 legumista -t http --host 0.0.0.0
```

`--host 0.0.0.0` is required there: bound to the default `127.0.0.1` the server listens
only on the container's own loopback and is unreachable from the host.

---

## Keeping the catalog current

The catalog is a build artifact of `lis-autocontent populate-catalog`, published as a
release asset on
[datastore-metadata](https://github.com/matthewwiese/datastore-metadata/releases) and
fetched by the server. Three things keep a running server current, in
increasing order of immediacy.

**On startup** the server fetches the catalog, sending the `ETag` and `Last-Modified` it
last saw. An unchanged catalog answers `304` and costs nothing; a changed one is
downloaded, validated, and written to the cache atomically.

**On a timer** — every 24 hours by default (`LEGUMISTA_CATALOG_POLL`) — the same
conditional check runs in the background. Set it to `0` to switch polling off.

**On demand**, via a webhook, so a newly published catalog lands in seconds instead of
waiting for the next poll:

```bash
LEGUMISTA_WEBHOOK_SECRET=$(openssl rand -hex 32) \
  legumista mcp -t http --host 0.0.0.0
```

That mounts `POST /catalog/refresh`, authenticated with GitHub's
`X-Hub-Signature-256` scheme — an HMAC-SHA256 of the request body under the shared secret,
compared in constant time. **With no secret set the route is not registered at all**, so
refresh-on-demand is strictly opt-in.

Point a GitHub webhook at it (repository → Settings → Webhooks), with the same value as
the secret and `application/json` as the content type. A `release` event then refreshes
every server the moment a catalog is published; GitHub's `ping` is answered without
fetching, so the hook shows green immediately.

To trigger it by hand, or from CI that is not GitHub:

```bash
BODY='{"action":"published"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$LEGUMISTA_WEBHOOK_SECRET" | awk '{print $2}')
curl -fsS -X POST http://127.0.0.1:8000/catalog/refresh \
  -H "X-Hub-Signature-256: sha256=$SIG" \
  -H 'Content-Type: application/json' \
  -d "$BODY"
```

The response reports what happened — `updated`, `unchanged`, `pinned`, or `error` with a
reason — and a failed refresh answers `502`, so a broken publish shows as a failed delivery
rather than disappearing behind a `200`.

### What a refresh cannot break

A refresh **replaces the loaded catalog only after** the download has parsed, passed a
structural check (a non-empty `collections` array and a `stats` object), and been accepted
by DSCensor's reader. Anything short of that leaves the server on the catalog it already
had. The failure this guards against is not a corrupt file but a *plausible* one — a login
page, an S3 error document, or a truncated transfer — which would otherwise be swapped in
and leave every `lis_*` tool answering confidently from nothing.

### Pinning a specific catalog

`LEGUMISTA_CATALOG_PATH` names a local `catalog.json` that wins over everything above: it
is used verbatim, nothing is downloaded, and polling is switched off. That is the offline
and reproducibility path. The webhook reports `pinned` rather than overriding it.

```bash
docker run --rm -i -v /path/to/catalog.json:/etc/legumista/catalog.json:ro \
    -e LEGUMISTA_CATALOG_PATH=/etc/legumista/catalog.json legumista
```

The pin is never discovered from the working directory. That directory is, by default,
the workspace the genomics tools may write into, so a file a tool call wrote there could
otherwise take over every `lis_*` answer.

### One wrinkle worth knowing

The resident map is part of the MCP `instructions`, which the protocol sends once at
initialize. A hot swap updates the catalog every tool reads, and every tool answer carries
the new build stamp — but a client connected across the swap keeps the map from the
catalog it connected under. The map is a species-level census that changes only when LIS
adds a species, so this is a cosmetic lag rather than a correctness one; reconnecting the
client refreshes it.

---

## Requirements

- **Python ≥3.11.** `pip install legumista` brings in the server and every tool's code.
- **Outbound HTTPS to the catalog URL** at startup. The catalog is downloaded, not
  shipped; a cached copy covers later restarts.
- **DSCensor** is not on PyPI yet. Point `LEGUMISTA_DSCENSOR_PATH` at the `dscensor/`
  directory of a checkout of the `legumista-interop` branch of `legumeinfo/microservices`.
  The container clones it for you.

  Either one missing degrades cleanly rather than failing: the six `lis_*` tools report
  the catalog as unavailable and name the URL they tried, and the other 27 tools are
  unaffected (`verify_ids` reports collection IDs as UNCHECKED).
- **NCBI CLIs** (optional, for `ncbi_datasets`/`ncbi_assembly_status`/`edirect`/`sra_runs`):
  NCBI `datasets` and EDirect on `PATH`. An `NCBI_API_KEY` raises the Entrez rate limit
  from 3 to 10 requests/second.

### Environment variables

| Variable | Effect |
| --- | --- |
| `LEGUMISTA_CATALOG_URL` | Where to download the catalog. Default: the published datastore-metadata release asset |
| `LEGUMISTA_CATALOG_PATH` | Pin to this local `catalog.json` instead: nothing is downloaded and polling is off |
| `LEGUMISTA_CACHE_DIR` | Where the download is cached. Default: `~/.cache/legumista` (`/var/cache/legumista` in the image) |
| `LEGUMISTA_CATALOG_POLL` | Seconds between background freshness checks. Default `86400`; `0` disables |
| `LEGUMISTA_WEBHOOK_SECRET` | Enables `POST /catalog/refresh`. Unset, the route does not exist |
| `LEGUMISTA_HOME` | Sandbox root for local file arguments. Default: the launch directory (same as `-C`) |
| `LEGUMISTA_DSCENSOR_PATH` | Where to find the DSCensor package |
| `LEGUMISTA_CONTACT_EMAIL` | Polite-pool mailto sent to OpenAlex, Crossref and Unpaywall. Set it on any shared server: retraction checks and `verify_ids` query Crossref once per DOI |
| `LEGUMISTA_PYSAM_ALLOWED_URLS` | Optional allowlist of `scheme://host[/path]` prefixes the genomics tools may open, matched by host. Unset, any public host is allowed |
| `LEGUMISTA_DEPLOYMENT` | `public` advertises every tool as read-only, so a public host's users are not prompted. Anything else (the default) keeps the hints honest: local users are asked before a tool writes or files an issue |
| `LEGUMISTA_JBROWSE_INSTANCE` | The JBrowse instance `browser_link` prefers when several serve an assembly. Default `all-genera` |
| `LEGUMISTA_JBROWSE_URL` | The instance `browser_link` targets when the catalog records no JBrowse placements (names are then predicted). Default: LIS all-genera |
| `LEGUMISTA_GITHUB_APP_ID` | The GitHub App `report_data_issue` files as (with `--allow-report`). Give the App only the Issues read & write permission and install it only on the target repository |
| `LEGUMISTA_GITHUB_APP_KEY_FILE` | Path to the App's private key (PEM). Mount it read-only; it never leaves the server |
| `LEGUMISTA_GITHUB_APP_INSTALLATION_ID` | Optional: the App's installation ID. Unset, it is looked up from the target repository |
| `LEGUMISTA_REPORT_REPOS` | Where reports go, as `service=owner/repo#label` pairs. Default: the development fork, `datastore-issue` and `mine-issue` labels |
| `LEGUMISTA_METADATA_REPO` | The datastore-metadata repository READMEs are re-read from, at the catalog's commit. Default `matthewwiese/datastore-metadata` |
| `LEGUMISTA_REPORT_DAILY_LIMIT` | Issues one server may file per day. Default `20` |
| `LEGUMISTA_INDEX_TTL` | Seconds a downloaded remote index (`.tbi`/`.bai`/`.csi`) is reused before it is fetched again. Indexes are cached per URL. Default `3600` |
| `LEGUMISTA_EGRESS_PORTS` | Ports the genomics tools may connect to. Default `80,443` |
| `LEGUMISTA_EGRESS_MAX_SEND_BYTES` | Most a genomics connection may send. Reads send about 1 KB; this bounds an upload. Default `65536` |
| `LEGUMISTA_EGRESS_IDLE_SECONDS` / `LEGUMISTA_EGRESS_MAX_SECONDS` | When a genomics connection is dropped: idle, and in total. Defaults `60` / `900` |
| `LEGUMISTA_PYSAM_TIMEOUT` | Seconds before a `samtools`/`bcftools` call is killed. Default `300` |
| `LEGUMISTA_PYSAM_WORKERS` | How many `samtools`/`bcftools` calls may run at once. Default `4` |
| `LEGUMISTA_PYSAM_MAX_FILE_BYTES` | Largest file one write may produce. Default 4 GiB; `0` disables (reads are capped at 64 MiB) |
| `NCBI_API_KEY` | Raises the Entrez rate limit |

---

## Caveats worth knowing

- **The catalog is a snapshot.** It carries a build timestamp and the `datastore-metadata`
  commit it came from, and every tool repeats that stamp, so staleness is visible rather
  than discovered. A collection published after the snapshot is invisible until a newer
  catalog is published and picked up — see
  [Keeping the catalog current](#keeping-the-catalog-current).
- **Predicted file lists.** Roughly 45% of collections publish no CHECKSUM, which is the
  only authoritative enumeration of a collection's files. For those, filenames are
  constructed from the documented naming convention and confirmed against the store at
  build time. Every such file is labelled, and never presented as authoritative.
- **Synteny lags the current assemblies.** Synteny is published for one, usually older,
  assembly per species — soybean has it on `Wm82.gnm2` only, and *Medicago truncatula* has
  none of its own. `lis_synteny` routes you to the assembly that has it rather than
  returning an empty result you'd read as "no synteny".
- **Results are size-capped** (~20,000 characters) and network calls time out at ~30 s.
  A capped result says so.

---

## Development

`pip install -e . pytest`, then `python -m pytest -q`. The whole suite is offline — every
network and PDF entry point is stubbed. [`AGENTS.md`](AGENTS.md) is the repository guide.

### Publishing to the official MCP Registry

Metadata for the [official MCP Registry](https://registry.modelcontextprotocol.io) lives
in [`server.json`](server.json) (reverse-DNS name `io.github.legumeinfo/legumista`,
pointing at the PyPI package). The registry is a *metaregistry* — it stores that manifest,
not the code, and verifies namespace ownership via the `<!-- mcp-name: … -->` marker at the
top of this README, which becomes the PyPI description.

```bash
uv build && uv publish                       # 1. push the package to PyPI
mcp-publisher login github                   # 2. verify the io.github.legumeinfo namespace
mcp-publisher publish                        # 3. submit server.json to the registry
```

Keep `version` in `server.json` in step with `pyproject.toml`; `tests/test_distribution.py`
enforces it. The `Dockerfile` carries the `io.modelcontextprotocol.server.name` label for
OCI-image verification.

## License

MIT — see [`LICENSE`](LICENSE).
