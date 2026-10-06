# Legumista

Legume genomics tools over the LIS Data Store (a resident catalog snapshot, plus its
indexed data files), the LIS InterMine mines, NCBI, the scholarly literature, and htslib.
Every tool returns plain text. Nothing writes, files or spends unless the server was
started to allow it. `guide(topic)` has the detail behind each tool family.

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
| Literature | `paper_search`, then `openalex_by_doi`, `read_paper`; `europepmc_search` for field queries |
| NCBI genomes, runs, records | `ncbi_assembly_status`, `sra_runs`; raw: `ncbi_datasets`, `edirect` |
| Anything else on the web | `web_search`, `web_fetch` |
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
  GWAS and markers and may spell IDs differently. `guide("mines")` says what each holds.
- **NCBI** annotates some LIS assemblies directly; its RefSeq chromosomes then name the
  LIS assembly in their titles. A selector's `ncbi` form places NCBI Gene IDs onto LIS
  genes, after checking the sequences are the same.
- **The catalog is a snapshot**: every LIS reply carries its build date and commit.
