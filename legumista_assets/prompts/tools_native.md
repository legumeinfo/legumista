# Native research tools (in-package reference)

These are the tools the harness exposes to you. They are implemented in-package against
keyless public APIs and local utilities — no external MCP server is required. **Every
tool is read-only** (nothing here writes files, mutates state, or spends money) and
**every tool returns plain text**, so you can read a result and decide the next call.

Conventions that apply to all tools below:
- **Errors are returned as text, not thrown.** A failed call returns a string beginning
  `error:` (or an `[exit N]` line for a CLI). Read it and adapt — retry with a simpler
  query, a different source, or report the gap. A tool never silently succeeds with junk.
- **Results are size-capped** (~20,000 characters). List views truncate each abstract to
  ~400 characters; long full-text/CLI output is cut with a `… [truncated]` marker. When a
  result is capped, say so rather than implying you saw the whole thing.
- **Network calls time out at ~30 s.** If a source is slow or down, prefer another rather
  than looping on the same failing call.
- **Identifiers are ground truth.** Carry DOIs / accessions forward verbatim from tool
  output. Never edit, complete, or invent one. If a tool didn't return it, you don't have it.

The doctrine throughout: **search → verify by identifier → only then assert; fan out and
deduplicate on DOI; when a search returns nothing, say so exactly and broaden the query.**

---

## openalex_search — the primary DOI-discovery tool

Search [OpenAlex](https://openalex.org) (a free, keyless index of ~250M scholarly works)
by topic and get back **DOI-anchored** metadata for each hit: title, authors, publication
year, venue, citation count, and a reconstructed abstract. Results are ranked by OpenAlex
relevance score.

**When to use:** first, for almost any `{{SUBJECT}}`-type question. It is the fastest way
to turn a topic into real, resolvable DOIs you can trust and carry into a corpus. Start
broad, read the hits, then refine.

**When not to use:** to read a paper's contents (use `read_paper`), or to get the actual
list of a work's references/citations (OpenAlex gives you *counts*, not the DOI lists —
see `openalex_by_doi`).

- **Arguments:**
  - `query` *(string, required)* — natural-language topic or keywords.
  - `max_results` *(int, optional, default 8, capped at 25)*.
  - `from_year` / `to_year` *(int, optional)* — restrict by publication date. If either is
    set, the other defaults to the project scope ({{YEAR_MIN}}–{{YEAR_MAX}}).
- **Returns:** a numbered list; each entry is `title (year)` then a metadata line
  (`doi:… | venue | cited-by:N | src:openalex`), the first authors, and a truncated abstract.
- **Notes:** abstracts are reconstructed from OpenAlex's inverted index, so spacing/casing
  can be imperfect — fine for triage, not for quotation. Titles without a registered DOI
  show no `doi:` field; treat those as "no DOI located," not an error to patch.
- **Example:** `{ "query": "chromosome-scale genome assembly Hi-C scaffolding", "from_year": 2020 }`

## openalex_by_doi — one work, with graph counts

Fetch a single OpenAlex work by DOI: full metadata plus how many works it **references**
and how many works **cite** it.

**When to use:** to confirm a specific DOI resolves and to gauge a paper's connectivity
(is this a landmark with 800 citers, or a leaf node?) before deciding whether to expand it.

- **Arguments:** `doi` *(string, required)* — bare (`10.1101/…`) or a `doi.org` URL; it is
  normalized for you.
- **Returns:** the work (as in `openalex_search`) plus a trailing
  `referenced_works: N | cited_by: N`.
- **Important limitation:** this returns **counts, not the DOIs** of the references/citers.
  The deterministic crawl walks the citation graph for you; this tool is for verification
  and connectivity, not for harvesting a reference list.

## paper_search — breadth across sources

Run one query against **OpenAlex + Crossref**, merge the hits, and **deduplicate by DOI**
(falling back to a title key when a record has no DOI).

**When to use:** the first broad sweep of a new topic, when you want coverage across two
indexes in a single call and don't yet know which source is richest.

**When not to use:** for depth in one source (use the per-source tools), or when you need
year filtering (use `openalex_search`).

- **Arguments:** `query` *(string, required)*; `max_results` *(int, default 8, capped 20)*.
- **Returns:** the deduplicated list, with a trailing `(N unique across OpenAlex+Crossref)`.

## crossref_search / arxiv_search / europepmc_search / biorxiv_search — per-source depth

Use these when you know which corner of the literature you're in, or to cross-check a hit
found elsewhere. Each takes `query` *(string, required)* and `max_results`
*(int, default 8, capped 25)*.

- **`crossref_search`** — the DOI registry itself. Best for exact publication metadata
  (journal, volume/issue/pages, registered abstract). Strong for published journal
  articles; weaker for grey literature.
- **`arxiv_search`** — arXiv preprints (physics, CS, math, quantitative biology).
  Returns **arXiv IDs, not DOIs** (`venue: arXiv:2401.01234`); year is the submission year.
- **`europepmc_search`** — Europe PMC, covering **PubMed + PMC + life-science preprints**,
  with abstracts and citation counts. The default choice for biology/medicine.
- **`biorxiv_search`** — **bioRxiv/medRxiv preprints** (via Crossref member 246 — keyless,
  no scraping). Adds `server` *(optional, `"biorxiv"` | `"medrxiv"`)* to restrict; omit for
  both. Use for the most recent, not-yet-peer-reviewed genomics work. Treat every hit as a
  **preprint** (unreviewed) when you cite it.

## read_paper / fulltext_grep — open-access full text

Read what a paper actually says, not just its abstract. Both resolve an open-access PDF
(via OpenAlex OA locations, then Unpaywall) and extract text with `pypdf`.

- **`read_paper`** — download and return the extracted text.
  - **Arguments:** `doi` *(string)* **or** `url` *(string)* — supply one; `max_pages`
    *(int, default 30)* caps how many pages are extracted.
  - **Returns:** a header `[N pages; extracted M] source: <url>` then the text (size-capped).
  - **Failure modes, each reported plainly:** no open-access copy found ("read the abstract
    via `openalex_by_doi` instead"); the URL served a landing page not a PDF; a
    scanned/image-only PDF (no extractable text). None of these are a reason to invent
    content — report what you could and could not read.
- **`fulltext_grep`** — same fetch, but return **only the lines matching a regex**. Use
  this to pull one fact from a long paper (a `N50`, a `2n=` chromosome count, a `p < 0.05`,
  an SRA/GenBank accession) without loading the whole thing into context.
  - **Arguments:** `pattern` *(string, required — a Python regex)*; `doi` **or** `url`;
    `ignore_case` *(bool, default true)*; `max_pages` *(int, default: all pages)*.
  - **Returns:** the matching lines (capped at 100), or a clear "No matches … in <url>".
  - **Example:** `{ "doi": "10.1371/journal.pone.0064799", "pattern": "N50|scaffold|genome size" }`

## Genomic records — NCBI (`ncbi_assembly_status`, `sra_runs`, `ncbi_datasets`, `edirect`)

Reach NCBI directly for genomes, assemblies, taxonomy, sequencing runs, and sequences —
records the bibliographic indexes don't hold. These shell out to the NCBI `datasets` CLI
and EDirect; if a CLI isn't installed the tool says so with an install link (it never hangs
or fabricates). Prefer the two purpose-built tools; drop to the raw wrappers only when they
can't express what you need.

- **`ncbi_assembly_status`** — *"Does a reference genome exist for this taxon, and how good
  is it?"* Wraps `datasets summary genome taxon`.
  - **Arguments:** `taxon` *(string, required)* — a scientific name (`"Escherichia coli"`)
    or a taxid.
  - **Returns:** a table — accession | assembly level (Contig/Scaffold/Chromosome/Complete)
    | contig N50 | release date | BioProject | organism — capped at 50 rows. Empty ⇒
    "No assemblies found," which is itself a citable finding ("no public genome as of today").
- **`sra_runs`** — *"Is there public raw sequencing data for X?"* `esearch` on SRA →
  `efetch runinfo`, parsed.
  - **Arguments:** `query` *(string, required)*; `retmax` *(int, default 20, capped 200)*.
  - **Returns:** run accession | platform/model | spots | bases | library layout | organism.
  - **Note:** a taxon query can return runs for associated organisms (symbionts, pathogens,
    contaminants) — check the organism column before asserting the data is the target species.
- **`ncbi_datasets`** — the raw `datasets` CLI for anything the above doesn't cover.
  - **Arguments:** `args` *(array of strings, required)* — the subcommand and flags, e.g.
    `["summary","genome","taxon","Danio rerio","--as-json-lines"]`. Passed without a shell.
- **`edirect`** — the raw Entrez pipeline: `esearch -db <db> -query <q>` piped into
  `esummary` (default) or `efetch`.
  - **Arguments:** `db` *(string, required — e.g. `assembly`, `nuccore`, `sra`, `taxonomy`,
    `pubmed`)*; `query` *(string, required — Entrez syntax, e.g. `"Homo sapiens[Organism]"`)*;
    `action` *(optional, `"esummary"` | `"efetch"`)*; `format` *(optional, for `efetch`, e.g.
    `runinfo`, `fasta`, `docsum`)*; `retmax` *(int, default 20, capped 200)*.

## web_search — non-bibliographic context

Keyless web search (DuckDuckGo) for things the scholarly APIs don't index: a lab's website,
a database/portal, a software repo, a news item, an author's affiliation.

- **Arguments:** `query` *(string, required)*; `max_results` *(int, default 8)*.
- **Returns:** title / URL / snippet per hit.
- **Rule:** never cite a *paper* from web search. If a result looks like a paper, confirm it
  through `openalex_search`/`crossref_search` to get a real DOI before you carry it forward.

## read_file / grep / web_fetch — local files and arbitrary URLs

- **`read_file`** — read a local text file. Arguments: `path` *(string, required)*. Use for
  project files (`corpus/corpus_digest.md`, ledgers, context inputs). Not for URLs (use
  `web_fetch`) or scholarly PDFs (use `read_paper`).
- **`grep`** — regex-search local files. Arguments: `pattern` *(string, required)*; `path`
  *(dir or file, default `.`)*; `glob` *(e.g. `**/*.md`)*; `ignore_case` *(bool, default true)*.
  Returns `file:line: match`, capped at 200 hits.
- **`web_fetch`** — fetch a URL and return readable text (size-capped). Arguments: `url`
  *(string, required)*. For a scholarly PDF prefer `read_paper`, which resolves the OA copy.

---

## Bioinformatics tools (pysam / htslib) — query indexed genomics files

For when the evidence is a genome, alignment, or variant file rather than a paper. These
bind htslib via `pysam`; they need the optional `bio` extra — if a tool says it's absent,
treat that as "unavailable", not an error to retry. File arguments are a project path or an
http(s) URL whose **sibling index must exist** (`.fai`/`.bai`/`.csi`/`.tbi`); a
"missing index" message means the file simply isn't queryable — say so, don't guess.
Regions are **samtools-style: 1-based, inclusive** — `seqid`, `seqid:start`, or
`seqid:start-end` (e.g. `chr1:1000-2000`).

**`samtools` and `bcftools` are general dispatchers** — you pass an argv list (subcommand
then flags), exactly like `ncbi_datasets`, and get the tool's stdout back. This is the main
surface; nearly all of samtools/bcftools is available.

- **`samtools`** — SAM/BAM/CRAM/FASTA. Args: `{args: [subcommand, ...]}`. e.g.
  `["view","-c","aln.bam","chr1:1-1000"]` (count), `["idxstats","aln.bam"]`,
  `["coverage","-r","chr1:1-1000","aln.bam"]`, `["depth","-r","chr1:100-200","aln.bam"]`,
  `["stats","aln.bam"]`, `["view","-H","aln.bam"]` (header → reference names).
- **`bcftools`** — VCF/BCF. Args: `{args: [subcommand, ...]}`. e.g.
  `["view","-H","v.vcf.gz","chr1:1-1000"]`, `["query","-f","%CHROM\\t%POS\\t%REF\\t%ALT\\n","v.vcf.gz"]`,
  `["stats","v.vcf.gz"]`, `["view","-h","v.vcf.gz"]` (header → contigs/samples).
- **`fasta_fetch`** — extract a subsequence from an indexed FASTA. Args: `path`, `region`.
- **`tabix_query`** — a bgzip+tabix table (GFF/GTF/BED/…): no `region` lists contigs, a
  `region` returns feature lines. Args: `path`, `region?`, `max_records?`.

**Reads vs writes.** Read subcommands (samtools `view`/`flagstat`/`idxstats`/`stats`/
`depth`/`coverage`, bcftools `view`/`query`/`stats`, and the two helpers) work by default.
Operations that write a file — samtools `sort`/`index`/`markdup`, bcftools `call`/`norm`/
`index`, `tabix_index`, or any read subcommand given an output flag like `-o` — are refused
unless the run was started with write access (`--allow-write`). If you get a "writes are
disabled" message, don't retry; report that the step needs write access. Path arguments are
confined to the project workspace.

---

## Optional MCP tools

If the researcher has configured MCP servers in `.mcp.json`, those servers' tools appear
alongside the native ones, namespaced `mcp__<server>__<tool>`, and follow the same doctrine.
None are required — the native tools above cover the entire pipeline on their own.
