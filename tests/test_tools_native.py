"""Native-tool tests — the workspace confinement guard (`_sandbox_path` /
`_is_sensitive`), the `paper_search` fan-out, and `read_paper`'s two output shapes.
No network: every HTTP/PDF entry point is stubbed.

The guard is tested here because this is where it lives, but its consumer is now
tools_pysam: every local path handed to samtools/bcftools/tabix passes through it."""
import os

import config
from legumista_agent import tools_native as N
from legumista_agent.tools_native import _is_sensitive, _sandbox_path


def test_workspace_root_is_not_sensitive():
    """The workspace root itself must be allowed. `os.path.relpath(root, root)` is a
    bare '.', which used to be mistaken for a hidden component and refused — a
    regression that made the workspace root refuse itself."""
    assert _is_sensitive(config.WORKSPACE) is False
    rp, err = _sandbox_path(".")
    assert err is None and rp == os.path.realpath(config.WORKSPACE)


def test_sandbox_still_refuses_hidden_and_secret_files():
    """The fix must not weaken the guard: dotfiles, dotdirs, and secret-like names stay
    refused, and paths escaping the workspace are still rejected."""
    for p in (".env", ".git/config", "knowledge/../.hidden",
              "secrets.key", "sub/.ssh/id_rsa"):
        _, err = _sandbox_path(p)
        assert err, f"expected {p!r} to be refused"
    _, err = _sandbox_path("/etc/passwd")
    assert err, "paths outside the workspace must be refused"


# --- paper_search: the fan-out that makes the per-source tools unnecessary ------------
def _stub_get(monkeypatch, openalex=(), crossref=(), preprints=()):
    """Replace tools_native._get with a URL-routing stub. Returns the URL log."""
    urls = []

    def fake_get(url, accept="application/json"):
        urls.append(url)
        if "openalex.org" in url:
            return {"results": list(openalex)}
        if "posted-content" in url:
            return {"message": {"items": list(preprints)}}
        return {"message": {"items": list(crossref)}}

    monkeypatch.setattr(N, "_get", fake_get)
    return urls


def _crossref_item(doi, title):
    return {"DOI": doi, "title": [title], "issued": {"date-parts": [[2020]]},
            "container-title": ["J. Legumes"], "author": [{"given": "A", "family": "Bean"}],
            "abstract": "<jats:p>an abstract</jats:p>"}


def test_paper_search_fetches_wider_than_it_returns(monkeypatch):
    """paper_search asked each source for exactly max_results and then truncated the
    merged list to max_results, so almost every Crossref hit was thrown away before the
    model saw it and the 'multi-source' merge was one source in practice. Each source
    must be queried for more rows than the caller gets back."""
    urls = _stub_get(monkeypatch)
    N._paper_search({"query": "chickpea drought", "max_results": 5})
    assert len(urls) == 3, "default call must hit OpenAlex, Crossref and Europe PMC"
    assert "per-page=15" in next(u for u in urls if "openalex.org" in u)
    assert "rows=15" in next(u for u in urls if "crossref.org" in u)
    assert "pageSize=15" in next(u for u in urls if "europepmc" in u)


def test_paper_search_fanout_is_capped(monkeypatch):
    """The widened fetch must stay bounded — a large max_results must not turn into an
    unbounded per-source request that times the tool out."""
    urls = _stub_get(monkeypatch)
    N._paper_search({"query": "soybean", "max_results": 100})   # clamped to 20 -> 60 rows
    assert f"per-page={N._FANOUT_CAP}" in next(u for u in urls if "openalex.org" in u)
    assert f"rows={N._FANOUT_CAP}" in next(u for u in urls if "crossref.org" in u)


def test_paper_search_returns_only_max_results_but_dedupes_the_wide_set(monkeypatch):
    """The wide fetch must not leak into the output: the caller still gets max_results
    rows, deduplicated by DOI across sources, with the true unique count reported."""
    oa = [{"title": "Shared work", "doi": "https://doi.org/10.1/SHARED",
           "publication_year": 2021, "authorships": []}]
    cr = [_crossref_item("10.1/shared", "Shared work"),
          _crossref_item("10.1/only-crossref", "Crossref only"),
          _crossref_item("10.1/third", "Third work")]
    _stub_get(monkeypatch, openalex=oa, crossref=cr)
    out = N._paper_search({"query": "cowpea", "max_results": 2})
    assert out.count("doi:") == 2                    # only max_results rendered
    assert "(3 unique across sources; sources — OpenAlex: 1 hit(s); Crossref: 3 hit(s)" in out
    assert out.count("10.1/shared") == 1             # merged, not listed twice


def test_paper_search_omits_preprints_unless_asked(monkeypatch):
    """Preprints are unreviewed; they must never enter a corpus by default. Without
    include_preprints there must be no posted-content request at all."""
    urls = _stub_get(monkeypatch, preprints=[_crossref_item("10.1101/x", "A preprint")])
    out = N._paper_search({"query": "lentil"})
    assert not any("posted-content" in u for u in urls)
    assert "10.1101/x" not in out


def test_paper_search_include_preprints_merges_tagged_biorxiv_rows(monkeypatch):
    """include_preprints must reach Cold Spring Harbor's posted-content (Crossref member
    246) and label each hit by server, so a preprint is never mistaken for a peer-
    reviewed paper in the merged list."""
    bio = {"DOI": "10.1101/2024.01.01.500000", "title": ["A bioRxiv preprint"],
           "posted": {"date-parts": [[2024]]}, "author": [{"given": "A", "family": "Bean"}]}
    med = {"DOI": "10.1101/2024.02.02.600000", "title": ["A medRxiv preprint"],
           "posted": {"date-parts": [[2024]]}, "group-title": "medRxiv Epidemiology",
           "author": []}
    urls = _stub_get(monkeypatch, preprints=[bio, med])
    out = N._paper_search({"query": "pigeonpea", "include_preprints": True,
                           "max_results": 5})
    preprint_url = next(u for u in urls if "posted-content" in u)
    assert "filter=type:posted-content,member:246" in preprint_url
    assert "10.1101/2024.01.01.500000" in out and "src:biorxiv" in out
    assert "10.1101/2024.02.02.600000" in out and "src:medrxiv" in out
    assert "bioRxiv/medRxiv" in out              # the source list names the extra source


# --- read_paper: one fetch, two output shapes ----------------------------------------
def _stub_pdf(monkeypatch, text, npages=12):
    """Replace _fetch_pdf_text (the only network/PDF path). `text` is page 1 (a str) or
    a list of page texts. Returns the call log."""
    calls = []
    page_texts = [text] if isinstance(text, str) else list(text)

    def fake(doi, url, max_pages, start_page=1):
        calls.append({"doi": doi, "url": url, "max_pages": max_pages,
                      "start_page": start_page})
        pages = [(i, t) for i, t in enumerate(page_texts, 1)][start_page - 1:]
        if max_pages is not None:
            pages = pages[:max_pages]
        return pages, "https://example.org/paper.pdf", npages, start_page, None

    monkeypatch.setattr(N, "_fetch_pdf_text", fake)
    return calls


_PAPER = "Intro line\nThe contig N50 was 1.2 Mb\nMethods\nassembly n50 checked\nEnd"


def test_read_paper_without_pattern_returns_the_whole_text(monkeypatch):
    """Folding the grep in must not change the plain read: still a page header plus the
    full extracted text, still capped at 30 pages by default."""
    calls = _stub_pdf(monkeypatch, _PAPER)
    out = N._read_paper({"doi": "10.1/x"})
    assert out.startswith("[pages 1–1 of 12] source: https://example.org/paper.pdf")
    assert "--- page 1 of 12 ---" in out
    assert "Intro line" in out and "End" in out
    assert calls[0]["max_pages"] == 30


def test_read_paper_with_pattern_returns_only_matching_lines(monkeypatch):
    """With a pattern, read_paper must return the grep shape — matching lines only, no
    full-text dump — and search every page, since a stat can sit past page 30."""
    calls = _stub_pdf(monkeypatch, _PAPER)
    out = N._read_paper({"doi": "10.1/x", "pattern": r"n50", "context": 0})
    assert "2 match(es) for /n50/" in out
    assert "p.1: " in out                               # every hit is cited to a page
    assert "The contig N50 was 1.2 Mb" in out and "assembly n50 checked" in out
    assert "Intro line" not in out and "Methods" not in out
    assert calls[0]["max_pages"] is None, "a pattern search must not stop at page 30"


def test_read_paper_pattern_honours_ignore_case_and_max_pages(monkeypatch):
    """The two args carried over from fulltext_grep must still do something: an explicit
    max_pages must reach the fetch, and ignore_case=False must not match the wrong case."""
    calls = _stub_pdf(monkeypatch, _PAPER)
    out = N._read_paper({"doi": "10.1/x", "pattern": r"n50", "ignore_case": False,
                         "max_pages": 3, "context": 0})
    assert "1 match(es)" in out and "assembly n50 checked" in out
    assert "The contig N50 was 1.2 Mb" not in out
    assert calls[0]["max_pages"] == 3


def test_read_paper_reports_no_matches_rather_than_empty(monkeypatch):
    """A pattern that matches nothing must say so against the source, not return an
    empty string the model would read as a failed fetch."""
    _stub_pdf(monkeypatch, _PAPER)
    out = N._read_paper({"doi": "10.1/x", "pattern": "zzz-no-such-token"})
    assert "No matches for /zzz-no-such-token/" in out
    assert "https://example.org/paper.pdf" in out


def test_read_paper_rejects_a_bad_pattern_before_downloading(monkeypatch):
    """A malformed regex must fail on the argument, not after spending a PDF download."""
    calls = _stub_pdf(monkeypatch, _PAPER)
    assert N._read_paper({"doi": "10.1/x", "pattern": "["}).startswith("error: bad regex")
    assert calls == [], "no fetch may happen when the pattern cannot compile"


def test_read_paper_propagates_fetch_errors_in_both_modes(monkeypatch):
    """Whichever shape is asked for, a failed resolve/download must surface as the
    fetcher's error text rather than an empty or misleading result."""
    monkeypatch.setattr(N, "_fetch_pdf_text",
                        lambda doi, url, max_pages, start_page=1: (None, "", 0, 0, "error: provide 'doi' or 'url'"))
    assert N._read_paper({}) == "error: provide 'doi' or 'url'"
    assert N._read_paper({"pattern": "x"}) == "error: provide 'doi' or 'url'"


def test_paper_search_year_bounds_are_pushed_to_each_api(monkeypatch):
    """Year filtering was only on the deleted openalex_search; losing it would have been
    a silent capability regression. Each API spells the filter differently, and filtering
    after the fetch would shrink the page so `max_results` quietly meant something else."""
    seen = []

    def fake_get(url, accept="application/json"):
        seen.append(url)
        if "openalex" in url:
            return {"results": []}
        return {"message": {"items": []}}

    monkeypatch.setattr(N, "_get", fake_get)
    N._paper_search({"query": "nodulation", "from_year": 2020, "to_year": 2026})

    openalex = [u for u in seen if "openalex" in u][0]
    crossref = [u for u in seen if "crossref" in u][0]
    assert "from_publication_date%3A2020-01-01" in openalex
    assert "to_publication_date%3A2026-12-31" in openalex
    assert "from-pub-date%3A2020-01-01" in crossref
    assert "until-pub-date%3A2026-12-31" in crossref


def test_paper_search_omits_the_filter_when_no_years_given(monkeypatch):
    """An unfiltered search must not send an empty filter= and narrow itself to nothing."""
    seen = []
    monkeypatch.setattr(N, "_get", lambda url, accept="application/json": (
        seen.append(url), {"results": [], "message": {"items": []}})[1])
    N._paper_search({"query": "nodulation"})
    assert all("filter=" not in u for u in seen)


# --- hardening prototype: source status, fusion, retraction flags, pages ---------------
def test_paper_search_total_outage_is_an_error_not_no_results(monkeypatch):
    def down(url, accept="application/json"):
        raise TimeoutError("timed out")
    monkeypatch.setattr(N, "_get", down)
    out = N._paper_search({"query": "nodulation"})
    assert out.is_error and "failed at every source" in out.text
    assert "No results" not in out.text


def test_paper_search_labels_a_partial_answer(monkeypatch):
    def half(url, accept="application/json"):
        if "crossref" in url:
            raise TimeoutError("timed out")
        if "openalex" in url:
            return {"results": [{"title": "A", "doi": "https://doi.org/10.1/a",
                                 "publication_year": 2020}]}
        return {"resultList": {"result": []}}
    monkeypatch.setattr(N, "_get", half)
    out = N._paper_search({"query": "x"})
    assert out.startswith("PARTIAL RESULTS — Crossref FAILED (TimeoutError: timed out)")


def test_paper_search_fuses_sources_instead_of_concatenating(monkeypatch):
    oa = [{"title": f"OA {i}", "doi": f"https://doi.org/10.1/oa{i}", "publication_year": 2020}
          for i in range(10)]
    cr = [_crossref_item(f"10.2/cr{i}", f"CR {i}") for i in range(10)]
    _stub_get(monkeypatch, openalex=oa, crossref=cr)
    out = N._paper_search({"query": "x", "max_results": 4})
    shown = [line for line in out.splitlines() if line.startswith("    doi:")]
    assert sum("10.1/oa" in line for line in shown) == 2
    assert sum("10.2/cr" in line for line in shown) == 2


def test_paper_search_flags_retractions_from_both_sources(monkeypatch):
    oa = [{"title": "Retracted per OpenAlex", "doi": "https://doi.org/10.1/r1",
           "publication_year": 2019, "is_retracted": True}]
    item = _crossref_item("10.1/r2", "Retracted per Crossref")
    item["updated-by"] = [{"DOI": "10.1/notice", "type": "retraction",
                           "source": "retraction-watch",
                           "updated": {"date-parts": [[2023, 4, 22]]}}]
    _stub_get(monkeypatch, openalex=oa, crossref=[item])
    out = N._paper_search({"query": "x"})
    assert "RETRACTED — Retracted per OpenAlex" in out
    assert "RETRACTED — Retracted per Crossref" in out
    assert "retraction 10.1/notice, 2023-04-22, via retraction-watch" in out


def test_read_paper_continues_past_the_character_cap(monkeypatch):
    big = ["x" * 15000, "y" * 15000, "z" * 100]
    _stub_pdf(monkeypatch, big, npages=3)
    first = N._read_paper({"doi": "10.1/x"})
    assert "--- page 1 of 3 ---" in first and "--- page 2" not in first
    assert "continue with start_page=2" in first
    second = N._read_paper({"doi": "10.1/x", "start_page": 2})
    assert "--- page 2 of 3 ---" in second


def test_read_paper_reports_total_matches_beyond_the_hit_cap(monkeypatch):
    _stub_pdf(monkeypatch, "\n".join(f"n50 line {i}" for i in range(150)))
    out = N._read_paper({"doi": "10.1/x", "pattern": "n50", "context": 0})
    assert out.startswith("showing 100 of 150 match(es)")


def test_oa_lookup_failure_is_not_reported_as_no_pdf(monkeypatch):
    def down(url, accept="application/json"):
        raise TimeoutError("timed out")
    monkeypatch.setattr(N, "_get", down)
    out = N._read_paper({"doi": "10.1/x"})
    assert out.startswith("error: could not check for an open-access PDF")
    assert "No open-access PDF is listed" not in out


def test_read_paper_hard_cuts_a_single_oversized_page_and_points_on(monkeypatch):
    _stub_pdf(monkeypatch, ["w" * 30000, "next page"], npages=2)
    out = N._read_paper({"doi": "10.1/x"})
    assert out.startswith("[page 1 of 2, truncated]")
    assert "continue with start_page=2" in out


def test_openalex_by_doi_returns_the_whole_abstract_and_retraction_status(monkeypatch):
    """The verification path must not truncate: an abstract cut at 400 characters invites
    the model to complete it. Status is merged from OpenAlex and Crossref."""
    from legumista_agent import pubstatus
    words = [f"w{i}" for i in range(300)]
    work = {"title": "A retracted legume paper", "doi": "https://doi.org/10.1/r",
            "publication_year": 2019, "is_retracted": True, "type": "article",
            "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/32426053"},
            "abstract_inverted_index": {w: [i] for i, w in enumerate(words)},
            "open_access": {"oa_status": "gold"}, "referenced_works": ["W1", "W2"]}
    monkeypatch.setattr(N, "_get", lambda url, accept="application/json": work)
    monkeypatch.setattr(pubstatus, "check_doi",
                        lambda doi: {"status": "ok", "notices": [], "checked": "crossref"})
    out = N._openalex_by_doi({"doi": "10.1/R"})
    assert "w0 w1 w2" in out and "w299" in out and " …" not in out
    assert "RETRACTED — A retracted legume paper" in out
    assert "pmid:32426053" in out
    assert "retraction check: crossref+openalex" in out


def test_openalex_by_doi_separates_an_unknown_doi_from_an_outage(monkeypatch):
    import urllib.error
    from legumista_agent import pubstatus
    from legumista_agent.results import coerce

    def missing(url, accept="application/json"):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    monkeypatch.setattr(N, "_get", missing)
    monkeypatch.setattr(pubstatus, "check_doi",
                        lambda doi: {"status": "not_found", "notices": [], "checked": "doi.org"})
    out = N._openalex_by_doi({"doi": "10.1/nope"})
    assert "doi.org does not know it either — the DOI is wrong or fabricated" in out
    assert coerce(out).is_error is False             # an answer about the DOI

    monkeypatch.setattr(pubstatus, "check_doi",
                        lambda doi: {"status": "non_crossref", "notices": [], "checked": "doi.org"})
    out = N._openalex_by_doi({"doi": "10.5281/zenodo.1"})
    assert "registered with another agency" in out and "wrong" not in out

    def down(url, accept="application/json"):
        raise TimeoutError("timed out")
    monkeypatch.setattr(N, "_get", down)
    assert coerce(N._openalex_by_doi({"doi": "10.1/x"})).is_error is True


def test_europepmc_search_carries_pmids_and_flags_preprints(monkeypatch):
    """Many Europe PMC records (older papers, Agricola) have no DOI; the PMID is then the
    only identifier the model can carry forward."""
    rows = [{"title": "An old record", "pmid": "12345", "source": "MED", "pubYear": "1998"},
            {"title": "A preprint", "doi": "10.1101/2024.01.01.1", "source": "PPR",
             "pubYear": "2024"}]
    monkeypatch.setattr(N, "_get", lambda url, accept="application/json":
                        {"resultList": {"result": rows}})
    out = N._europepmc_search({"query": 'ORGANISM:"Cicer arietinum"'})
    assert "pmid:12345" in out
    assert "PREPRINT (unreviewed) — A preprint" in out
    assert "Retraction status is not checked here" in out


def test_paper_search_excludes_europepmc_preprints_unless_asked(monkeypatch):
    urls = _stub_get(monkeypatch)
    N._paper_search({"query": "lentil"})
    assert "NOT%20SRC%3APPR" in next(u for u in urls if "europepmc" in u)
    urls.clear()
    N._paper_search({"query": "lentil", "include_preprints": True})
    assert "SRC%3APPR" not in next(u for u in urls if "europepmc" in u)


def test_assembly_status_counts_every_row_and_shows_the_most_complete_first(monkeypatch):
    """300 assemblies must not be reported as '50 assemblies', and the 50 shown should be
    the informative ones, not the first 50 the CLI happened to print."""
    import json
    contigs = [{"accession": f"GCA_{i:09d}.1",
                "assembly_info": {"assembly_level": "Contig", "release_date": "2020-01-01"},
                "assembly_stats": {"contig_n50": 1000},
                "organism": {"organism_name": "Glycine max"}} for i in range(299)]
    best = {"accession": "GCF_000004515.6",
            "assembly_info": {"assembly_level": "Chromosome", "release_date": "2021-06-01"},
            "assembly_stats": {"contig_n50": 20000000},
            "organism": {"organism_name": "Glycine max"}}
    text = "\n".join(json.dumps(r) for r in contigs + [best])
    monkeypatch.setattr(N, "_cli_raw", lambda argv, stdin=None, timeout=120: (text, None))
    out = N._ncbi_assembly_status({"taxon": "Glycine max"})
    assert out.startswith("showing 50 of 300 assembly(ies) for 'Glycine max', most complete")
    first = out.splitlines()[1]
    assert "GCF_000004515.6" in first and "[Chromosome]" in first


def test_sra_runs_reports_the_search_total_beside_the_rows_shown(monkeypatch):
    envelope = (b"<ENTREZ_DIRECT>\n  <Db>sra</Db>\n  <WebEnv>X</WebEnv>\n  <QueryKey>1</QueryKey>"
                b"\n  <Count>1234</Count>\n  <Step>1</Step>\n</ENTREZ_DIRECT>\n")
    csv_text = ("Run,Platform,Model,spots,bases,LibraryLayout,ScientificName\n"
                + "".join(f"SRR{i},ILLUMINA,NovaSeq 6000,10,1000,PAIRED,Glycine max\n"
                          for i in range(20)))
    monkeypatch.setattr(N, "_cli_raw_bytes", lambda argv, timeout=60: envelope)
    monkeypatch.setattr(N, "_cli_raw",
                        lambda argv, stdin=None, timeout=120: (csv_text, None))
    out = N._sra_runs({"query": "Glycine max"})
    assert out.startswith("20 SRA run(s) for 'Glycine max' (the search matched 1,234 SRA "
                          "record(s)")


def test_openalex_by_doi_says_when_crossref_could_not_check(monkeypatch):
    """A failed Crossref check must not disappear into the merge: 'retraction check:
    openalex' alone would read as a full check."""
    from legumista_agent import pubstatus
    work = {"title": "T", "doi": "https://doi.org/10.1/t", "publication_year": 2020}
    monkeypatch.setattr(N, "_get", lambda url, accept="application/json": work)
    monkeypatch.setattr(pubstatus, "check_doi", lambda doi: {
        "status": "unknown", "notices": [], "error": "TimeoutError: timed out"})
    out = N._openalex_by_doi({"doi": "10.1/t"})
    assert ("retraction check: openalex (Crossref: STATUS UNKNOWN (retraction check "
            "failed: TimeoutError: timed out))") in out


def test_read_paper_context_is_adjacent_lines_and_zero_means_none(monkeypatch):
    """`context` defaults to one line each side; 0 must mean none, not the default."""
    _stub_pdf(monkeypatch, "alpha\nbeta N50 here\ngamma")
    around = N._read_paper({"doi": "10.1/x", "pattern": "N50"})
    assert "p.1: alpha ⏎ beta N50 here ⏎ gamma" in around
    only = N._read_paper({"doi": "10.1/x", "pattern": "N50", "context": 0})
    assert "p.1: beta N50 here" in only and "alpha" not in only
