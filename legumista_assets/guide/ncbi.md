# ncbi — NCBI genomes, runs and records: ncbi_assembly_status, sra_runs, ncbi_datasets, edirect

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
- NCBI Gene IDs (`LOC…`) onto LIS genes: a selector's `ncbi` form (`guide("genes")`).
  RefSeq names can separate paralogs that LIS descriptions do not.
