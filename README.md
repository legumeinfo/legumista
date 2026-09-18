<!-- mcp-name: io.github.legumeinfo/legumista -->

![Legumista bean mascot](./legumista_assets/legumista.png)

# Legumista — an MCP server for legume genomics

**Legumista** gives a model working access to legume genomics. It serves 30 read-only
tools over the [Model Context Protocol](https://modelcontextprotocol.io): a resident
snapshot of the [LIS Data Store](https://data.legumeinfo.org) catalog, InterMine queries
against the LIS mines, scholarly literature search and full-text retrieval, NCBI
datasets/EDirect, and a samtools/bcftools/tabix suite that reads indexed genomics files
over HTTP without downloading them.

Point any MCP client at it — Claude Desktop, Claude Code, an IDE, another agent — and ask
questions like *"does chickpea have an ortholog of this soybean gene, and what's the
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

---

## The toolset

Started with `--allow-write`, `samtools`, `bcftools` and `tabix_index` also permit their
write operations. Without it (the default) they run reads only and refuse writes.

### LIS Data Store — the resident catalog

| Tool | What it answers |
| --- | --- |
| `lis_survey` | What exists across the whole store: genera, collection counts, types |
| `lis_find` | Discover collections — genomes, annotations, diversity, GWAS, synteny |
| `lis_files` | A collection's files, and which are randomly accessible over HTTP |
| `lis_gene` | A gene's locus, plus ready-to-run calls for its protein/CDS and neighbourhood |
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
| `legumemine_gene_orthologs` | Counterparts in other species — "does my crop have this gene?" |
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

### Literature, NCBI and the web

| Tool | What it does |
| --- | --- |
| `paper_search` | Broad search across OpenAlex + Crossref, deduplicated by DOI |
| `openalex_by_doi` | One work by DOI, with reference and citation counts |
| `europepmc_search` | Europe PMC (PubMed/PMC/preprints), with abstracts |
| `read_paper` | Fetch an open-access PDF by DOI or URL; optionally grep it |
| `ncbi_datasets` | The NCBI `datasets` CLI — genome, gene and taxonomy data |
| `ncbi_assembly_status` | Does a reference genome exist for this taxon, and how good is it? |
| `sra_runs` | Public sequencing data: runs, platform, spots, bases |
| `edirect` | Raw Entrez — `esearch` piped to `esummary`/`efetch` |
| `web_search` | Keyless open-web search |
| `web_fetch` | Fetch a URL as readable text |

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

### Transports

```bash
legumista mcp                          # stdio — how clients spawn a server
legumista mcp -t http --port 8000      # long-running HTTP endpoint at /mcp
legumista mcp -C /data                 # confine local file arguments to /data
legumista mcp --allow-write            # permit genomics write ops (sort/index/call/…)
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
`compose.yaml`.

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
release asset and fetched by the server. Three things keep a running server current, in
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

A `catalog.json` in the working directory (or at the root of a source checkout) wins over
everything above: it is used verbatim, nothing is downloaded, and polling is switched off.
That is the offline and reproducibility path. The webhook reports `pinned` rather than
overriding it.

```bash
docker run --rm -i -v /path/to/catalog.json:/work/catalog.json:ro legumista
```

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
  the catalog as unavailable and name the URL they tried, and the other 24 tools are
  unaffected.
- **NCBI CLIs** (optional, for `ncbi_datasets`/`ncbi_assembly_status`/`edirect`/`sra_runs`):
  NCBI `datasets` and EDirect on `PATH`. An `NCBI_API_KEY` raises the Entrez rate limit
  from 3 to 10 requests/second.

### Environment variables

| Variable | Effect |
| --- | --- |
| `LEGUMISTA_CATALOG_URL` | Where to download the catalog. Default: the published LIS-autocontent release asset |
| `LEGUMISTA_CACHE_DIR` | Where the download is cached. Default: `~/.cache/legumista` (`/var/cache/legumista` in the image) |
| `LEGUMISTA_CATALOG_POLL` | Seconds between background freshness checks. Default `86400`; `0` disables |
| `LEGUMISTA_WEBHOOK_SECRET` | Enables `POST /catalog/refresh`. Unset, the route does not exist |
| `LEGUMISTA_HOME` | Sandbox root for local file arguments. Default: the launch directory (same as `-C`) |
| `LEGUMISTA_DSCENSOR_PATH` | Where to find the DSCensor package |
| `LEGUMISTA_CONTACT_EMAIL` | Polite-pool mailto sent to OpenAlex/Crossref — set yours for better rate limits |
| `LEGUMISTA_PYSAM_ALLOWED_URLS` | Optional URL-prefix allowlist for the genomics tools |
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
