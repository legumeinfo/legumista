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

```bash
docker build -t legumista . && docker run --rm -i legumista    # everything included
```

The container is the path that works out of the box. A `pip install` gives you the
server and the tools, but two pieces are not on PyPI yet — see
[Requirements](#requirements).

---

## Why it's built this way

**The catalog is resident, not crawled.** Every `lis_*` tool answers from a bundled
`catalog.json` — 1,048 collections, 6,316 files, 938 DOIs, 70 taxa — rather than walking
`data.legumeinfo.org` over HTTP. A question like *"which soybean assemblies exist and which
of their files are indexed?"* is a dictionary lookup, not a dozen round trips, and it works
the same when the store is slow or unreachable. The catalog is built by
[LIS-autocontent](https://github.com/legumeinfo/LIS-autocontent) from `datastore-metadata`
and read here through DSCensor's `CatalogController`. It is data, and it ships separately
from the code — mount a newer one and the server picks it up with no rebuild.

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

legumista is one command, so the server is just `legumista mcp` — the same everywhere.
Any client can launch it with **`uvx`** (the [uv](https://docs.astral.sh/uv/) runner, the
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

The image additionally bakes in the external CLIs that four tools shell out to (NCBI
`datasets` and EDirect) and the DSCensor catalog reader, so the whole advertised toolset
works out of the box:

```bash
docker build -t legumista .
docker run --rm -i legumista                                  # stdio; -i keeps stdin open
docker run --rm -d -p 8000:8000 legumista -t http --host 0.0.0.0
```

`--host 0.0.0.0` is required: bound to the default `127.0.0.1` the server is reachable
only from inside the container. To swap in a fresher catalog without rebuilding:

```bash
docker run --rm -i -v /path/to/catalog.json:/work/catalog.json:ro legumista
```

---

## Requirements

- **Python ≥3.11.** `pip install legumista` brings in the server and every tool's code.
- **The catalog** is a 2.4 MB build artifact and is **not** shipped in the wheel. The
  `lis_*` tools look for `catalog.json` at the top of a source checkout or in the working
  directory. Working from a clone gives you the committed one; otherwise put a copy in the
  directory you start the server from. The container bakes one in at `/work/catalog.json`.
- **DSCensor** is not on PyPI yet. Point `LEGUMISTA_DSCENSOR_PATH` at the `dscensor/`
  directory of a checkout of the `legumista-interop` branch of `legumeinfo/microservices`.
  The container clones it for you.

  Missing either one degrades cleanly rather than failing: the six `lis_*` tools report
  the catalog as unavailable and print the command that produces one, and the other 24
  tools are unaffected.
- **NCBI CLIs** (optional, for `ncbi_datasets`/`ncbi_assembly_status`/`edirect`/`sra_runs`):
  NCBI `datasets` and EDirect on `PATH`. An `NCBI_API_KEY` raises the Entrez rate limit
  from 3 to 10 requests/second.

### Environment variables

| Variable | Effect |
| --- | --- |
| `LEGUMISTA_HOME` | Sandbox root for local file arguments. Default: the launch directory (same as `-C`) |
| `LEGUMISTA_DSCENSOR_PATH` | Where to find the DSCensor package |
| `LEGUMISTA_CONTACT_EMAIL` | Polite-pool mailto sent to OpenAlex/Crossref — set yours for better rate limits |
| `LEGUMISTA_PYSAM_ALLOWED_URLS` | Optional URL-prefix allowlist for the genomics tools |
| `NCBI_API_KEY` | Raises the Entrez rate limit |

---

## Caveats worth knowing

- **The catalog is a snapshot.** It carries a build timestamp and the `datastore-metadata`
  commit it came from, and every tool reports that stamp. A collection published after the
  snapshot is invisible until the catalog is rebuilt. Newer catalog, no rebuild: mount it
  over `catalog.json`.
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
