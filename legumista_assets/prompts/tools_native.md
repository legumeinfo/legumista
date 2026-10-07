# Legumista

Legume genomics tools over the LIS Data Store (a resident catalog snapshot, plus its
indexed data files), the LIS InterMine mines, NCBI, the scholarly literature, and htslib.
Every tool returns plain text. Nothing writes, files or spends unless the server was
started to allow it.

## LIS first

Legumista is an LIS project. Search LIS before anything else: the Data Store (`lis_*`,
`extract_features`, `browser_link`) and the LIS mines (`mine_*`). For a question about one
species or genus, make each mine query in both LegumeMine and that genus's own mine where
one exists, then compare the two results and present both, each with its mine. Then turn readily to NCBI and the literature: to corroborate what LIS shows,
to supply what it does not hold, or to judge which questions are worth pursuing next.
Start from them only when LIS cannot answer, and say so. Where LIS and another source
disagree, report both, each with its source.

## Where to start

| Need | Tool |
|---|---|
| What exists, or is absent, across species | `lis_survey`, and the map below |
| A species' collections; a collection id, whole or partial | `lis_find` (`query` searches ids) |
| Everything recorded about one collection, and how to read its files | `lis_files` |
| What to cite for a dataset | `lis_lineage` |
| One gene: locus, description, sequence calls | `lis_gene` (symbols also via `mine_gene_symbol`) |
| Genes or families by function | `mine_gene_search` |
| Anything a mine holds, as its search box finds it, with counts by category and organism | `mine_search` |
| A gene list: IDs, a region, a family, NCBI Gene IDs | `lis_gene` with `genes` (a selector); sequence with `extract_features`; a JBrowse view with `browser_link` |
| A gene's families, homologs, GO terms, proteins, expression | `mine_gene_families`, `mine_gene_family_members`, `mine_gene_ontology`, `mine_gene_proteins`, `mine_gene_expression` |
| Synteny between assemblies | `lis_synteny` |
| Traits, QTL, GWAS, marker positions | `mine_trait_qtls`, `mine_trait_gwas`, `mine_marker_position` |
| Reads, variants or sequence in an indexed file | `samtools`, `bcftools`, `fasta_fetch`, `tabix_query`; `tabix_index` writes |
| Literature, after LIS | `paper_search`, then `openalex_by_doi`, `read_paper`; `europepmc_search` for field queries |
| NCBI genomes, runs, records, after LIS | `ncbi_assembly_status`, `sra_runs`; raw: `ncbi_datasets`, `edirect` |
| Anything else on the web, after LIS | `web_search`, `web_fetch` |
| A defect in LIS data | `report_data_issue`, where the server offers it |

## LIS conventions

- **Collections** are `Genus/species/type/<id>`, and an id ends in an arbitrary
  4-character key (`Wm82.gnm4.ann1.T8TQ`): take ids from `lis_find`, never build them.
  `gnmN` is an assembly version and `annN` an annotation of it.
- **Gene IDs** are fully qualified as `<abbrev>.<strain>.gnmN.annN.<name>`
  (`glyma.Wm82.gnm4.ann1.Glyma.12G040000`). A bare name can exist in several assemblies
  as different loci (`Glyma.12G040000` in gnm2, gnm4 and gnm6), so name the assembly.
  Names need not survive re-annotation (peanut's Tifrunner gnm2 ann2 renamed every
  gene): move between annotations with a selector's `translate_to`. A genus mine may drop
  the name's first token (ArachisMine's `arahy.Tifrunner.gnm2.ann1.GHMM2H` is
  `...ann1.Arahy.GHMM2H` elsewhere); `lis_gene` and `verify_ids` accept both. The letters
  of a name carry no meaning.
- **Coordinates** are 1-based, inclusive, and belong to one assembly; contig names carry
  the assembly prefix (`glyma.Wm82.gnm4.Gm12`). Never compare them across assemblies.
  `gene_models_main.bed` holds coding extents; the GFF3's gene row adds the UTRs, and
  `lis_gene` reports both.
- **Descriptions** (GFF3 `Note`, mine descriptions) are automated, transferred from
  homologs; a bracketed species (`[Glycine max]`) names that homolog's species, not the
  gene's. Close paralogs share them, so a description makes a candidate, never a
  function.
- **Gene families** (`legume.fam3`, `legfed_v1_0`) show homology, not orthology: they
  hold paralogs. Claim orthology only with synteny or phylogeny behind it.
- **Expression** values carry one unit per study (`expression_unit` in the record), set
  by how LIS processed them, which can differ from the original paper.
- **Publications**: every collection should carry `publication_doi`, but some do not.
  A parent's DOI is the parent's, not the collection's.
- **Synteny** is usually published for one, often older, assembly per species
  (soybean's is gnm2): `lis_synteny` names the assembly that has it.
- **Mines**: every `mine_*` tool queries any LIS mine. `legumemine`, the default, spans
  all legumes with the most annotations; `taxon` picks a genus mine, which adds QTL,
  GWAS and markers and may spell IDs differently. The Mines section below says what each holds.
- **NCBI** annotates some LIS assemblies directly; its RefSeq chromosomes then name the
  LIS assembly in their titles. A selector's `ncbi` form places NCBI Gene IDs onto LIS
  genes, after checking the sequences are the same.
- **The catalog is a snapshot**: every LIS reply carries its build date and commit.

## Reading a result

- **`error:` means the tool could not answer** (outage, missing CLI, bad argument). It is
  never evidence of absence: fix the call, try another source, or say you could not check.
- **`PARTIAL RESULTS`, `INCOMPLETE SEARCH`, `NOT CHECKED`**: a source or route did not
  run. What it would have covered is unverified, not absent.
- **"none" / "no match" is a finding only with what was checked**, which the result
  names. Report it with that scope.
- **"showing N of M" or a named `offset`**: you have not seen everything. Page, or say so.
- **An unrecognised taxon is about the name**, with suggestions; common names and
  abbreviations (`soybean`, `glyma`) are accepted.
- Replies are capped near 20,000 characters; network calls time out near 30 s.

## Answering

- State only what a tool result supports. Carry identifiers, DOIs and coordinates
  verbatim, with their source and assembly. Never complete or invent an identifier.
- A wrong claim costs far more than an admitted gap: unsure means unverified.
- Before an answer that cites identifiers, run your draft through `verify_ids`; drop NOT
  FOUND and MISMATCH, flag UNCHECKED. A RETRACTED work is never support.

## Data Store tools

All six read the resident catalog: instant, no network for metadata. They resolve, they
do not retrieve: they hand back URLs, sequence names and region strings for `fasta_fetch`,
`tabix_query`, `samtools` and `bcftools` to read.

### lis_find
- No arguments: the genera. `{taxon}`: that species' data types. `{taxon, type}`: its
  collections, with synopsis, genotype, expression unit and `publication_doi`.
- `{query}` matches collection ids as a case-insensitive substring, across the whole
  catalog unless `taxon`/`type` narrow it. An exact id is listed first.

### lis_files
- The collection's whole catalog record, then its files.
- How the file list was obtained matters. It is either authoritative (the collection's
  CHECKSUM), **CONFIRMED** (built from the filename convention and checked to exist), or
  **PREDICTED** (built from the convention, unchecked: a file may 404).
  **FILE LIST UNAVAILABLE** means the metadata is missing, not that the collection is
  empty.
- **NOT INDEXED** files have no `.fai`/`.tbi`/`.csi`: whole-file downloads only, not
  readable through these tools.
- Protein and CDS FASTAs are indexed by model name, so `fasta_fetch` with that name as the
  region returns one sequence without a download.

### lis_gene
- `{gene, collection?}` accepts an exact ID, the same name without its first token as a
  genus mine spells it (`GHMM2H` for `Arahy.GHMM2H`, resolved only when one gene
  matches), a curated symbol (`GmNARK`) or a superseded ID from the collection's synonym
  file (`Glyma01g00210`); the reply says which route
  resolved it, and a miss lists every route and whether it ran. An ID from another
  assembly is not a synonym.
- `{genes}` lists a selection (see Gene selections).

### lis_survey
- Coverage across the whole store: genera and counts, a species' data types, or with
  `needs` which species hold several types at once. A zero here is a finding.

### lis_synteny
- `{genome}` or `{gene}` lists every partner assembly, including pairs stored under the
  partner's collection. Add `partner`, and optionally `region` (on the requested
  genome), for blocks with score and `median_Ks`; the requested genome is always on the
  left.
- Ks scales with divergence between the pair: never turn it into a confidence tier.
- Asking about an assembly without synteny returns the one that has it.

### lis_lineage
- Walks `derived_from` to the assembly and returns every DOI the result depends on,
  de-duplicated, with retraction status. A collection with no publication of its own is
  marked so: the DOIs listed are its sources'.

## Gene selections

A selector names a gene list by definition. The server resolves it on every call, so
the same selector can be reused later. Exactly one of:

- `{"ids": [...], "collection"?: "<annotation>"}`: up to 200 IDs, curated symbols or
  superseded IDs. `collection` is needed unless an ID is fully qualified.
- `{"region": "glyma.Wm82.gnm4.Gm12:2800000-3000000"}`: genes whose coding extent overlaps
  it. If the assembly has several annotations, the reply lists them for you to choose.
- `{"family": "legume.fam3.10524", "collection": "<annotation>"}`: the family's members
  in that annotation, read from its own assignment file, so the list is complete.
- `{"ncbi": ["LOC112749796"], "collection": "<annotation>"}`: NCBI Gene IDs placed by
  locus where NCBI annotates the same assembly (matched by identical sequence length;
  otherwise "not placed"). Each row keeps NCBI's name, which can separate paralogs that
  LIS describes alike.

Options on every form:
- `"translate_to": "<annotation>"`. Same assembly: by locus overlap, so renamed genes
  map. Same species, other assembly: by gene name or synonym file. Across species:
  through shared gene families, one-to-many, so a match can be a paralog.
- `"offset": N` pages. The reply names the next offset.

The reply accounts for every input: resolved directly, via symbol, via superseded ID,
not found, or refused (one annotation per selection). Report a miss with the routes it
names.

### lis_gene with `genes`
One row per gene: gene span (UTRs included), protein length, and the first clause of the
GFF3 `Note`. A length far below the rest of a family usually marks a partial model.

### extract_features
- `feature`: `protein`, `cds`, `mrna` (by model name; primary model unless
  `isoforms:"all"`), or `gene`, `upstream`, `downstream`, `utr5`, `utr3` (from the genome,
  strand-corrected: upstream on the minus strand lies at higher coordinates).
- `flank` (default 2,000, max 10,000), `anchor` (`gene_start` or `start_codon`),
  `stop_at_neighbor`, `dry_run` (counts and bases, nothing fetched).
- Headers carry locus, source and notes such as clipping. CDS notes (no ATG, no stop,
  internal stop) are information: partial models are normal, not data defects.
- Large output goes to a workspace FASTA + BED only with write access; otherwise the reply
  says how many records it left out.

### browser_link
- A JBrowse 2 link for a selector or a region: gene models track with the genes
  highlighted, `mark:"features"` for an inline track, or `view:"dotplot"` with `compare`.
- Track and assembly names come from the catalog's record of each instance (`instance`
  picks one; all-genera first), so "no instance serves X (checked: …)" is a finding. It
  reads no data; give the user the link.

## Mines

Every tool here queries any LIS mine and every reply names the mine that answered.
`taxon` picks a species' or genus's own mine, `mine` names one, and with neither
`legumemine` answers. `mine_gene_family_members` is the exception: no routing `taxon`,
since it is cross-species by purpose. `target_taxon` filters rows; it never picks a mine.
For one species or genus, make the same query in `legumemine` and in its genus mine,
compare, and present both; a reply about one genus ends with a tip naming the other mine
whenever that mine can answer the same query. A fully qualified gene ID that a mine
spells differently is retried in its other spelling, and the reply says so.

### Which mine

| | `legumemine` | a genus mine (`arachismine`, `glycinemine`, …) |
|---|---|---|
| Genes, descriptions, families | every legume | its genus only |
| Expression | yes | most; none in `lupinusmine`, `aeschynomenemine`, `lensmine` |
| Curated symbols | yes | `glycinemine` only |
| QTL, GWAS, markers | none | `arachismine`, `glycinemine`, `phaseolusmine`, `vignamine` only |
| Gene ID spelling | the Data Store's | may differ: ArachisMine writes `…ann1.GHMM2H` for `…ann1.Arahy.GHMM2H` |

Counts differ between mines for the same question, and a genus mine's counts are the
ones its web pages show. Name the mine with any count you report.

### Reading a reply
- **A bare gene name matches every assembly that has it** (`Glyma.12G040000` in gnm2,
  gnm4, gnm6). Read the assembly column and pass `assembly` to pick one.
- **Empty results say which case applies**: "no gene matching X" (wrong ID, assembly or
  species) versus "X exists … but has no <kind> there", a real absence for that gene.
- Results are capped by `max_results` (up to 500); the header says when there are more.

### Tools
- `mine_gene_search`: genes, or with `search:"families"` gene families, whose
  description contains a phrase. Use full product names, not abbreviations: matching
  is by substring, and the reply flags rows where the phrase occurs only
  inside a longer word. Genes take `target_taxon`; families span species, so they do not.
  Families come largest first.
- `mine_search`: a mine's own keyword search, as its search box runs it: whole words in
  every indexed class and field (quoted phrases, OR, AND NOT, trailing `*`), with the
  total and counts by category and organism. `taxon` routes to the genus mine;
  `category` and `organism` narrow to one of the counts; pages of up to 100 with
  `offset`. Its counts differ from `mine_gene_search`'s substring match, which also
  takes longer words that contain the phrase.
- `mine_gene_symbol`: symbol to gene IDs, with synopsis and DOIs. The catalog's
  curated symbols answer first; the mine is the fall-through.
- `mine_gene_proteins`, `mine_gene_families`, `mine_gene_ontology`:
  per-gene records.
- `mine_gene_expression`: values per sample with each study's unit. Compare only
  within one study.
- `mine_gene_family_members`: a gene's family, or a named family, listed across
  species. `target_taxon` keeps one species; `member_assembly`/`member_annotation` keep
  one genome's members (`assembly`/`annotation` pick the gene's own copy). Long lists page
  with `offset`. For one annotation's members with loci, a `family` selector in
  `lis_gene` is complete and faster.
- `mine_trait_qtls`, `mine_trait_gwas`, `mine_marker_position`: need `taxon`, because this
  data exists only in genus mines. A species with no mine is reported as such; its data,
  if any, is in the Data Store (`lis_find`).

## Indexed files

- A file argument is a workspace path or an http(s) URL whose sibling index exists
  (`.fai`, `.bai`, `.csi`, `.tbi`). "Missing index" means the file is not queryable;
  `lis_files` says which LIS files are.
- Regions are 1-based, inclusive: `seqid`, `seqid:start` or `seqid:start-end`
  (`glyma.Wm82.gnm4.Gm12:1000-2000`).
- If a tool reports pysam absent, the tools are unavailable on this server; do not retry.

### Tools
- `samtools` and `bcftools` take an argv list, `{args: [subcommand, ...]}`, and return
  stdout. Header first to learn contig and sample names: `["view","-H",<bam>]`,
  `["view","-h",<vcf>]`. Then, for example, `["view","-c",<bam>,<region>]`,
  `["coverage","-r",<region>,<bam>]`, or
  `["query","-f","%CHROM\t%POS\t%REF\t%ALT\n","-r",<region>,<vcf>]`.
- `fasta_fetch`: `{path, region}`; a region may be a whole sequence name, such as a
  protein's model name.
- `tabix_query`: `{path, region?, max_records?}`; without a region it lists contigs.
- `tabix_index` writes an index.

### Reads and writes
Read subcommands and options work by default. Anything that writes a file is refused
unless the server allows writes: `sort`, `index`, `markdup`, `call`, `norm`,
`tabix_index`, or an output option (`-o`, `view -U`, `fastq -1`, …), or any option outside
a read subcommand's allowlist. On "writes are disabled", report that the step needs write
access rather than retrying with other flags. Paths are confined to the workspace, and
`##idx##` paths are refused: pass the data file alone and its index is found beside it.

## Literature and the web

- `paper_search`: OpenAlex, Crossref and Europe PMC in parallel, merged by rank and
  de-duplicated by DOI/PMID. Hits are flagged RETRACTED, EXPRESSION OF CONCERN or
  PREPRINT; the footer gives each source's outcome. `include_preprints` adds bioRxiv and
  medRxiv, which are unreviewed. Hits found only by Europe PMC have unchecked retraction
  status: `verify_ids` them before citing.
- `europepmc_search`: Europe PMC's own syntax, for field queries `paper_search` cannot
  express (`ORGANISM:`, `SRC:AGR`). Includes preprints, flagged.
- `openalex_by_doi`: one work by DOI, with its full abstract, IDs, open-access status and
  retraction status from OpenAlex and Crossref. Every LIS collection's `publication_doi`
  goes here.
- `read_paper`: open-access full text by DOI or URL, page-labelled; cite passages as
  "p. N". `pattern` (a regex) returns only matching lines with `context`, for pulling one
  figure from a paper. A capped read ends with the `start_page` to continue from.
- `web_search` finds things the scholarly indexes do not: lab pages, portals, software.
  Never cite a paper from it: confirm through `paper_search` or `openalex_by_doi` first.
- `web_fetch` returns a URL's readable text. For papers, `read_paper` is better.
- There is no file reading or grep here: use the client's own tools for local files.

## NCBI

These run the NCBI `datasets` CLI and EDirect. A missing CLI is reported with an install
link: the tool is unavailable, not failing.

- `ncbi_assembly_status {taxon}`: public assemblies for a taxon, most complete first
  (accession, level, contig N50, date, BioProject, organism), at most 50 rows with the
  total. An empty result is a finding: no public genome as of today.
- `sra_runs {query, retmax?}`: SRA runs (accession, platform, spots, bases, layout,
  organism). A taxon query also returns runs of associated organisms (symbionts,
  pathogens); check the organism column.
- `ncbi_datasets {args}`: the raw `datasets` CLI, read-only subcommands only, e.g.
  `["summary","genome","taxon","Glycine max","--as-json-lines"]`.
- `edirect {db, query, action?, format?, retmax?}`: `esearch` piped to `esummary`
  (default) or `efetch`, e.g. `{db:"gene", query:"Phaseolus vulgaris[Organism] AND
  receptor kinase[Title]"}`.
- NCBI Gene IDs (`LOC…`) onto LIS genes: a selector's `ncbi` form (see Gene selections).
  RefSeq names can separate paralogs that LIS descriptions do not.

## Checking identifiers and reporting defects

### verify_ids
Pass a draft answer as `text`, or lists of DOIs, gene IDs, collection IDs and GCA_/GCF_
accessions. `citations` as `[{doi, title}]` also catches a real DOI attached to the wrong
paper. One verdict per ID: FOUND, NOT FOUND, MISMATCH, RETRACTED or UNCHECKED. FOUND means
the identifier exists, not that it supports your claim. A gene ID is checked in
legumemine, then in its species' genus mine, which can spell the same gene differently
(ArachisMine's `arahy.Tifrunner.gnm2.ann1.GHMM2H` is legumemine's
`arahy.Tifrunner.gnm2.ann1.Arahy.GHMM2H`); NOT FOUND names the mines checked.

### report_data_issue
Served only where enabled. For defects a curator would fix in the Data Store or a mine: a
README that does not parse or names the wrong collection, a field that contradicts its
siblings, a typo'd identifier, a mine record that contradicts the store. Not for missing
files, CDS notes, site or browser configuration, or suspicions.

- One `subject` (a collection, or `<mine>/<Class>/<primaryIdentifier>`), one `field`
  (`readme`, `readme.<key>`, `catalog.<key>`, or a mine attribute) and the value you
  `observed`. The server re-reads the field and refuses if it differs, so quote exactly.
- `summary` is the issue title. `expected` and `reason` are shown as unverified.
- The user confirms before anything is filed: the server asks them, or you get a PREVIEW
  to show them, and call again with `confirm` only after they agree. Never confirm on
  their behalf. A duplicate returns the existing issue.
