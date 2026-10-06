# datastore — the LIS Data Store tools: lis_find, lis_files, lis_gene, lis_synteny, lis_survey, lis_lineage

All six read the resident catalog: instant, no network for metadata. They resolve, they
do not retrieve: they hand back URLs, sequence names and region strings for `fasta_fetch`,
`tabix_query`, `samtools` and `bcftools` to read.

## lis_find
- No arguments: the genera. `{taxon}`: that species' data types. `{taxon, type}`: its
  collections, with synopsis, genotype, expression unit and `publication_doi`.
- `{query}` matches collection ids as a case-insensitive substring, across the whole
  catalog unless `taxon`/`type` narrow it. An exact id is listed first.

## lis_files
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

## lis_gene
- `{gene, collection?}` accepts an exact ID, a curated symbol (`GmNARK`) or a superseded
  ID from the collection's synonym file (`Glyma01g00210`); the reply says which route
  resolved it, and a miss lists every route and whether it ran. An ID from another
  assembly is not a synonym.
- `{genes}` lists a selection (see `guide("genes")`).

## lis_survey
- Coverage across the whole store: genera and counts, a species' data types, or with
  `needs` which species hold several types at once. A zero here is a finding.

## lis_synteny
- `{genome}` or `{gene}` lists every partner assembly, including pairs stored under the
  partner's collection. Add `partner`, and optionally `region` (on the requested
  genome), for blocks with score and `median_Ks`; the requested genome is always on the
  left.
- Ks scales with divergence between the pair: never turn it into a confidence tier.
- Asking about an assembly without synteny returns the one that has it.

## lis_lineage
- Walks `derived_from` to the assembly and returns every DOI the result depends on,
  de-duplicated, with retraction status. A collection with no publication of its own is
  marked so: the DOIs listed are its sources'.
