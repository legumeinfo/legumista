# literature — paper_search, europepmc_search, openalex_by_doi, read_paper, web_search, web_fetch

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
