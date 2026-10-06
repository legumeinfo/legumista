# genes — gene selectors, and the tools that take them: lis_gene, extract_features, browser_link

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

## lis_gene with `genes`
One row per gene: gene span (UTRs included), protein length, and the first clause of the
GFF3 `Note`. A length far below the rest of a family usually marks a partial model.

## extract_features
- `feature`: `protein`, `cds`, `mrna` (by model name; primary model unless
  `isoforms:"all"`), or `gene`, `upstream`, `downstream`, `utr5`, `utr3` (from the genome,
  strand-corrected: upstream on the minus strand lies at higher coordinates).
- `flank` (default 2,000, max 10,000), `anchor` (`gene_start` or `start_codon`),
  `stop_at_neighbor`, `dry_run` (counts and bases, nothing fetched).
- Headers carry locus, source and notes such as clipping. CDS notes (no ATG, no stop,
  internal stop) are information: partial models are normal, not data defects.
- Large output goes to a workspace FASTA + BED only with write access; otherwise the reply
  says how many records it left out.

## browser_link
- A JBrowse 2 link for a selector or a region: gene models track with the genes
  highlighted, `mark:"features"` for an inline track, or `view:"dotplot"` with `compare`.
- Track and assembly names come from the catalog's record of each instance (`instance`
  picks one; all-genera first), so "no instance serves X (checked: …)" is a finding. It
  reads no data; give the user the link.
