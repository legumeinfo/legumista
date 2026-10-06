# mines — the LIS InterMine tools, any mine: mine_search, mine_gene_*, mine_trait_*, mine_marker_position

Every reply names the mine that answered.

- **A bare gene name matches every assembly that has it** (`Glyma.12G040000` in gnm2,
  gnm4, gnm6). Read the assembly column and pass `assembly` to pick one.
- **Empty results say which case applies**: "no gene matching X" (wrong ID, assembly or
  species) versus "X exists … but has no <kind> there", a real absence for that gene.
- Results are capped by `max_results` (up to 500); the header says when there are more.

## Tools
- `mine_gene_search`: genes, or with `search:"families"` gene families, whose
  description contains a phrase. Use full product names ("chalcone synthase", not
  "CHS"): matching is by substring, and the reply flags rows where the phrase occurs only
  inside a longer word. Genes take `target_taxon`; families span species, so they do not.
  Families come largest first.
- `mine_search`: a mine's own keyword search, as its search box runs it: whole words in
  every indexed class and field (quoted phrases, OR, AND NOT, trailing `*`), with the
  total and counts by category and organism. `taxon` routes to the genus mine;
  `category` and `organism` narrow to one of the counts; pages of up to 100 with
  `offset`. Counts differ from `mine_gene_search`'s substring match: in ArachisMine
  "chalcone synthase" is 280 genes by keyword and 342 by substring, which also takes
  "synthase-like" and "deoxychalcone".
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
