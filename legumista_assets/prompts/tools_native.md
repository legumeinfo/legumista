# Legumista tools

The tools this server exposes, implemented against keyless public APIs, a bundled
snapshot of the LIS Data Store, and local htslib. **Every tool is read-only by default**
(nothing writes files, mutates state, or spends money unless the server was started with
`--allow-write`) and **every tool returns plain text**, so you can read a result and
decide the next call.

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

## paper_search — the primary discovery tool

Fans out across OpenAlex and Crossref and dedupes by DOI, querying each source wider than
the result set so the merge is genuinely multi-source rather than one index with a second
one truncated off the end.

- **Args:** `{query, max_results?, from_year?, to_year?, include_preprints?}`.
- `include_preprints` adds bioRxiv/medRxiv posted-content. Off by default: preprints are
  unreviewed, and a reading list built from them reads as settled literature unless asked for.
- **Always run `europepmc_search` alongside it.** Measured on legume queries, Europe PMC's
  hits and `paper_search`'s barely overlap — Europe PMC is not one of the indexes it fans
  out to, so running only `paper_search` silently leaves out most of the life-sciences
  literature.

## europepmc_search — life-sciences coverage nothing else reaches

PubMed/PMC/preprints, with abstracts. Args: `{query, max_results?}`. For this domain it is
not an alternative to `paper_search`, it is the other half.

## openalex_by_doi — one work, by identifier

Fetch a single work's metadata plus reference/citation counts. Args: `{doi}`. This is the
verification path, not a discovery path — and the one you want constantly, because every
LIS collection publishes a `publication_doi`.

## read_paper — open-access full text, whole or filtered

Download an OA PDF (by DOI or URL) and extract its text, so you read the actual
methods/results rather than an abstract. Args: `{doi?, url?, pattern?, ignore_case?,
max_pages?}`.

Pass `pattern` to get back only the lines matching a regex — pull one figure (an N50,
`2n=`, `p<0.05`, an accession) without loading the whole paper into context. Same fetch
either way; `pattern` only changes what is returned.

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
  through `paper_search` or `openalex_by_doi` to get a real DOI before you carry it forward.

## web_fetch — arbitrary URLs

Fetch an http(s) URL and return its readable text (HTML stripped, size-capped). For a
scholarly paper prefer `read_paper`, which resolves the open-access PDF.

**There is no `read_file` or `grep` here.** Reading and searching local files is your
client's job — it almost certainly has better tools for it than a sandboxed duplicate
would be. If you need a file's contents, use the client's own file tools.

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

## LIS Data Store (`lis_find`, `lis_files`, `lis_gene`, `lis_synteny`, `lis_survey`, `lis_lineage`)

Legume genomes, annotations, diversity panels, GWAS and more, for ~21 genera. All five
tools read a **resident catalog** — the whole datastore in one document, held in memory —
so they answer instantly and make no network requests. They **resolve; they do not
retrieve**: they hand you URLs and region strings that the bioinformatics tools above
then read.

1. **`lis_find`** — discovery. No arguments lists the genera; `{taxon:"Glycine max"}`
   lists that species' data types; `{taxon, type:"annotations"}` lists collections with
   their synopsis, genotype and **`publication_doi`**. Always start here: a collection
   name ends in an arbitrary four-character key (`Wm82.gnm4.ann1.T8TQ`) you cannot guess.
2. **`lis_files`** — which of a collection's files are **randomly accessible**, and how.
   Read the provenance line before trusting a path. A file list is either authoritative
   (from the collection's published CHECKSUM), **CONFIRMED** (built from the datastore's
   filename convention and checked to exist), or **PREDICTED** (built from that
   convention and *not* checked — a listed file may 404). A collection with neither
   reports **FILE LIST UNAVAILABLE**, which means the metadata is missing, not that the
   collection is empty. Within a list, **NOT INDEXED** means no `.fai`/`.tbi` is
   published, so the file is a whole-file download rather than region-queryable.
3. **`lis_gene`** — a gene to its locus plus ready-made `fasta_fetch`/`tabix_query`
   calls. Accepts an exact ID (`Glyma.12G040000`), a **curated symbol** (`GmNARK`), or a
   **superseded ID** (`Glyma01g00210`); the reply says which route answered. An ID from a
   *different assembly* is not a synonym and will not resolve — use `lis_find` to pick the
   matching collection.
4. **`lis_survey`** — coverage across the whole store: genera and counts, the data types
   a species has, or with `needs` which species hold several types **at once**. Reach for
   this when the question is about **coverage or absence** — "which species lack
   expression data" has no link to follow and is answerable only from a complete catalog.
   A zero here is a finding, not a failed lookup.
5. **`lis_synteny`** — syntenic blocks and whole-genome alignments between assemblies.
   With just `{genome}` or `{gene}` it lists every partner, **including pairs stored under
   the other genome's collection** — a pairwise file lives under whichever genome is the
   reference, so a genome with no collection of its own still has partners. Add
   `{partner}` and optionally `{region}` (A-side, samtools-style) for the blocks, with
   score and `median_Ks`. Do not turn Ks into a confidence tier: it scales with lineage
   divergence, so a cutoff meaningful for one pair is wrong for another.

   Synteny is published for **one, usually old, assembly per species** — soybean has it on
   `Wm82.gnm2`, not the `gnm4` that `lis_gene` uses. Asking about an assembly without it
   returns the one that has it; follow that rather than concluding there is no synteny.

6. **`lis_lineage`** — what a collection was derived from, and every publication the
   result depends on, de-duplicated. "Cite everything this rests on" in one call.

Every reply carries the catalog's build date and source commit. A catalog is a snapshot:
if the stamp looks old, say so rather than presenting it as current. If no catalog is
configured, all five report that and explain how to enable one.

Two things follow from the store's design and are worth exploiting. Every collection
carries a `publication_doi` **by specification**, so any dataset can be traced to its
paper with `openalex_by_doi`/`read_paper`. And the protein/CDS FASTAs are indexed **by
gene ID**, so `fasta_fetch` with the sequence name as the region returns one protein
without downloading the file.
