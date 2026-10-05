# Legumista tools

The tools this server exposes, implemented against keyless public APIs, a resident
snapshot of the LIS Data Store catalog (downloaded at startup), the LIS InterMine
instances, and local htslib. **Every tool is read-only by default**
(nothing writes files, mutates state, or spends money unless the server was started with
`--allow-write`) and **every tool returns plain text**, so you can read a result and
decide the next call.

## How to read a result

- **`error:` (sent as a tool error) means the tool could not answer** — an outage, a
  missing CLI, a bad argument. It is never evidence that something does not exist. Fix the
  call or try another source; if you still cannot check, say "I could not verify X", not
  "X does not exist".
- **`PARTIAL RESULTS` means a source failed**: anything missing may still exist.
- **`INCOMPLETE SEARCH` / `NOT CHECKED` means a route did not run**: report the item as
  unverified, not absent.
- **"none" / "no match" is a finding only together with what was checked** — the result
  says ("no gene matching X in legumemine"). Say what was checked when you rely on it.
- **"showing N of M" / "hit its cap"** means you have not seen everything. Say so, or page.
- **An unrecognised taxon is a statement about the name**, with suggestions — not
  evidence that LIS lacks data. Common names and abbreviations are accepted.
- **Results are size-capped** (~20,000 characters); network calls time out at ~30 s.

## How to answer

- **State only what a tool result in this conversation supports.** Carry DOIs,
  accessions, gene IDs and coordinates verbatim, with their source and assembly. Never
  edit, complete, or invent an identifier: if a tool didn't return it, you don't have it.
- **A wrong claim costs far more than an admitted gap.** Treat a wrong factual claim as
  costing nine times what a correct one earns: state a fact only if you are at least 90%
  sure a tool result supports it; otherwise say it is unverified or leave it out.
- **Before a final answer that cites identifiers, call `verify_ids`** with your draft.
  Cite nothing it reports NOT FOUND or MISMATCH; call UNCHECKED items unverified.
- **A RETRACTED work is never support.** Cite an EXPRESSION OF CONCERN work only with the
  flag stated.
- **Coordinates are 1-based, inclusive, and belong to one assembly**; never compare them
  across assemblies.

The doctrine throughout: **search → verify by identifier → only then assert**; when a
search finds nothing, say exactly what was searched, then broaden the query.

---

## paper_search — the primary discovery tool

Searches OpenAlex, Crossref and Europe PMC in parallel and merges them by rank,
deduplicated by DOI/PMID. Hits show DOI/PMID/PMCID and are flagged RETRACTED, EXPRESSION OF
CONCERN or PREPRINT from source metadata; the footer lists each source's outcome.

- **Args:** `{query, max_results?, from_year?, to_year?, include_preprints?}`.
- `include_preprints` adds bioRxiv/medRxiv posted-content. Off by default: preprints are
  unreviewed, and a reading list built from them reads as settled literature unless asked for.
- Hits that came only from Europe PMC have unchecked retraction status — `verify_ids`
  them before citing.

## europepmc_search — Europe PMC's own query syntax

Only for field queries paper_search cannot express (`ORGANISM:`, `SRC:AGR` for Agricola).
Preprints are included and flagged. Args: `{query, max_results?}`.

## openalex_by_doi — one work, by identifier

The verification path: the full abstract (search hits show only ~400 characters of it),
DOI/PMID/PMCID, work type, open-access status, and retraction status checked against both
OpenAlex and Crossref. Args: `{doi}`. Every LIS collection publishes a `publication_doi`,
so you will use this constantly.

## read_paper — open-access full text, whole or filtered

Download an OA PDF (by DOI or URL) and extract its text, so you read the actual
methods/results rather than an abstract. Args: `{doi?, url?, pattern?, ignore_case?,
max_pages?, start_page?, context?}`.

Pass `pattern` to get back only the lines matching a regex — pull one figure (an N50,
`2n=`, `p<0.05`, an accession) without loading the whole paper into context. Same fetch
either way; `pattern` only changes what is returned.

Full text is page-labelled (`--- page N of M ---`); a capped read ends with
`continue with start_page=N`. Pattern hits carry `p.N:` and a line of `context`. Cite a
passage as "p. N" of the paper.

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
    | contig N50 | release date | BioProject | organism — most complete first, at most 50
    rows; the header says "showing 50 of N" when there are more. Empty ⇒ "No assemblies
    found," which is itself a citable finding ("no public genome as of today").
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
bind htslib via `pysam`; if a tool says pysam is absent, treat that as "unavailable", not
an error to retry. File arguments are a project path or an http(s) URL whose **sibling
index must exist** (`.fai`/`.bai`/`.csi`/`.tbi`); a "missing index" message means the file
simply isn't queryable — say so, don't guess.
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
`index`, `tabix_index`, or a read subcommand given an output option (`-o`, `view -U`,
`fastq -1`, …) or any option outside its read-only list — are refused unless the run was
started with write access (`--allow-write`). If you get a "writes are disabled" message,
don't retry with another flag; report that the step needs write access. Path arguments are
confined to the project workspace; pass a data file alone (no `##idx##`) — its index is
found beside it.

---

## LIS Data Store (`lis_find`, `lis_files`, `lis_gene`, `lis_synteny`, `lis_survey`, `lis_lineage`)

Legume genomes, annotations, diversity panels, GWAS and more, for ~21 genera. All six
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
   **superseded ID** (`Glyma01g00210`, where the collection publishes a synonym file); the
   reply says which route answered, and on a miss lists exactly which routes ran. An ID
   from a *different assembly* is not a synonym and will not resolve — use `lis_find` to
   pick the matching collection.
4. **`lis_survey`** — coverage across the whole store: genera and counts, the data types
   a species has, or with `needs` which species hold several types **at once**. Reach for
   this when the question is about **coverage or absence** — "which species lack
   expression data" has no link to follow and is answerable only from a complete catalog.
   A zero here is a finding, not a failed lookup.
5. **`lis_synteny`** — syntenic blocks and whole-genome alignments between assemblies.
   With just `{genome}` or `{gene}` it lists every partner, **including pairs stored under
   the other genome's collection** — a pairwise file lives under whichever genome is the
   reference, so a genome with no collection of its own still has partners. Add
   `{partner}` and optionally `{region}` (on the requested genome, samtools-style) for the
   blocks, with score and `median_Ks`. Do not turn Ks into a confidence tier: it scales
   with lineage divergence, so a cutoff meaningful for one pair is wrong for another.

   Every partner is read, and blocks always put the requested genome on the left.

   Synteny is published for **one, usually old, assembly per species** — soybean has it on
   `Wm82.gnm2`, not the `gnm4` that `lis_gene` uses. Asking about an assembly without it
   returns the one that has it; follow that rather than concluding there is no synteny.

6. **`lis_lineage`** — what a collection was derived from, and every publication the
   result depends on, de-duplicated. "Cite everything this rests on" in one call.

Every reply carries the catalog's build date and source commit. A catalog is a snapshot:
if the stamp looks old, say so rather than presenting it as current. If no catalog is
configured, all six report that and explain how to enable one.

Two things follow from the store's design and are worth exploiting. Every collection
carries a `publication_doi` **by specification**, so any dataset can be traced to its
paper with `openalex_by_doi`/`read_paper`. And the protein/CDS FASTAs are indexed **by
gene ID**, so `fasta_fetch` with the sequence name as the region returns one protein
without downloading the file.

---

## Gene selections (`extract_features`, `browser_link`)

Both take `genes`, a **selector**: a short definition the server resolves on every call,
so it can be reused verbatim later. Exactly one of:

- `{"ids": [...], "collection"?: "<annotation>"}` — up to 200 IDs, curated symbols or
  superseded IDs; `collection` is needed unless an ID is fully qualified.
- `{"region": "glyma.Wm82.gnm4.Gm12:2800000-3000000"}` — genes whose coding extent
  overlaps it (1-based).
- `{"family": "legume.fam3.12584", "collection": "<annotation>"}` — that family's members
  in one annotation.

Add `"translate_to": "<annotation>"` to move a selection to another annotation (same
species: by gene name or synonym file; across species: through shared gene families —
one-to-many, so a match can be a paralog). Add `"offset": N` to page. The reply accounts
for every input — resolved, via symbol, via superseded ID, not found, or refused (one
annotation per selection) — so report a miss with the routes it names.

- **`extract_features`** — strand-correct sequence: `feature` = `protein`, `cds`, `mrna`
  (by model name, primary model by default; `isoforms:"all"` for every model), or `gene`,
  `upstream`, `downstream`, `utr5`, `utr3` (from the genome, using the GFF3's gene and UTR
  spans; minus-strand records are reverse-complemented, and upstream on the minus strand
  lies at higher coordinates). `flank` (default 2,000, max 10,000), `anchor`
  (`gene_start` or `start_codon`), `stop_at_neighbor`. Headers carry locus, source and
  notes such as clipping. CDS notes (no ATG, no stop, internal stop) are information:
  partial models are normal, never a data defect. `dry_run` gives record counts and bases
  without fetching. Large output is written to a workspace FASTA + BED only with
  `--allow-write`; otherwise the reply says how many records it left out.
- **`browser_link`** — a JBrowse 2 link for a selector or a `region`: the gene models
  track, genes highlighted (`mark:"features"` adds them as an inline track instead), or
  `view:"dotplot"` with `compare` (another genome). Names come from the catalog's record
  of each instance's config (`instance` picks one; all-genera first), so "no instance
  serves X (checked: …)" is a finding; an older catalog makes it predict names and say
  so. Give the user the link; it reads no data itself.

---

## LIS InterMine (`legumemine_*`, `lis_trait_*`, `lis_marker_position`)

Per-question queries over the LIS mines; every result names the mine that answered.

- **A bare gene name matches several assemblies** (`Glyma.12G040000` exists in gnm2, gnm4
  and gnm6 — different loci). Read the assembly column and pass `assembly` to pick one;
  never mix assemblies in one claim.
- **Empty results say which case applies**: "no gene matching X" (wrong ID, assembly or
  species) versus "X exists … but has no <kind> there" (a real absence for that gene).
- `legumemine_gene_symbol` — symbol → gene ID(s), synopsis and DOIs. Use it first for a symbol.
- `legumemine_gene_proteins`, `legumemine_gene_families`, `legumemine_gene_ontology` —
  per-gene records.
- `legumemine_gene_expression` — values per sample with each study's **unit**; compare
  only within one study (`source`), never across units.
- `legumemine_gene_family_members` — a gene's homologs via its family; `target_taxon`
  lists one species. **Family membership is homology, not orthology** (families contain
  paralogs): say "homolog" unless phylogeny or synteny (`lis_synteny`) supports orthology.
- `lis_trait_qtls`, `lis_trait_gwas`, `lis_marker_position` — **require `taxon`**: this
  data lives only in per-genus mines. A species with no mine is reported as such; its
  data, if any, is in the Data Store (`lis_find`).

## report_data_issue — file a data defect (when the server offers it)

Served only where enabled. For **defects in the LIS Data Store or a mine** that a curator
would fix: a README that does not parse or names the wrong collection, a field that
contradicts its siblings, a typo'd identifier, a mine record that contradicts the store.
Not for missing files, CDS notes, site or browser configuration, or your own suspicions.

Name one `subject` (a collection; or `<mine>/<Class>/<primaryIdentifier>`), one `field`
(`readme`, `readme.<key>`, `catalog.<key>`; or a mine attribute) and the value you
`observed`. The server re-reads that field and **refuses if it differs** — so quote it
exactly. `summary` is the title (the object and the defect); `expected` and `reason` are
shown as unverified. The user confirms before anything is filed: either the server asks
them, or you get a PREVIEW and must show it to them and call again with `confirm` only
after they agree. Never confirm on their behalf. A duplicate returns the existing issue.

## verify_ids — check identifiers before you answer

Pass your draft as `text` (or lists of DOIs, gene IDs, collection IDs, GCA_/GCF_
accessions; `citations` as `[{doi, title}]` catches a real DOI attached to the wrong
paper). One verdict per ID: FOUND, NOT FOUND, MISMATCH, RETRACTED, or UNCHECKED.
