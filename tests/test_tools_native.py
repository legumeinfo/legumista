"""Native-tool tests — the workspace confinement guard (`_sandbox_path` /
`_is_sensitive`), the local `grep`, the `paper_search` fan-out, and `read_paper`'s two
output shapes. No network: every HTTP/PDF entry point is stubbed."""
import os

import config
from legumista_agent import tools_native as N
from legumista_agent.tools_native import _grep, _is_sensitive, _sandbox_path


def test_workspace_root_is_not_sensitive():
    """The workspace root itself must be allowed. `os.path.relpath(root, root)` is a
    bare '.', which used to be mistaken for a hidden component and refused — regression
    for a default-path grep/read of the project refusing itself."""
    assert _is_sensitive(config.WORKSPACE) is False
    rp, err = _sandbox_path(".")
    assert err is None and rp == os.path.realpath(config.WORKSPACE)


def test_grep_default_path_searches_workspace(tmp_path, monkeypatch):
    """`grep` with no `path` defaults to '.' and must search the workspace, not refuse."""
    monkeypatch.setattr(config, "WORKSPACE", str(tmp_path))
    (tmp_path / "note.md").write_text("alpha beta gamma\n", encoding="utf-8")
    out = _grep({"pattern": "beta", "glob": "**/*.md"})
    assert "note.md" in out and "beta" in out
    assert not out.startswith("error")


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
    assert len(urls) == 2, "default call must hit OpenAlex + Crossref only"
    assert "per-page=15" in urls[0], urls[0]
    assert "rows=15" in urls[1], urls[1]


def test_paper_search_fanout_is_capped(monkeypatch):
    """The widened fetch must stay bounded — a large max_results must not turn into an
    unbounded per-source request that times the tool out."""
    urls = _stub_get(monkeypatch)
    N._paper_search({"query": "soybean", "max_results": 100})   # clamped to 20 -> 60 rows
    assert f"per-page={N._FANOUT_CAP}" in urls[0]
    assert f"rows={N._FANOUT_CAP}" in urls[1]


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
    assert "(3 unique across OpenAlex+Crossref)" in out
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
    """Replace _fetch_pdf_text (the only network/PDF path). Returns the call log."""
    calls = []

    def fake(doi, url, max_pages):
        calls.append({"doi": doi, "url": url, "max_pages": max_pages})
        return text, "https://example.org/paper.pdf", npages, npages, None

    monkeypatch.setattr(N, "_fetch_pdf_text", fake)
    return calls


_PAPER = "Intro line\nThe contig N50 was 1.2 Mb\nMethods\nassembly n50 checked\nEnd"


def test_read_paper_without_pattern_returns_the_whole_text(monkeypatch):
    """Folding the grep in must not change the plain read: still a page header plus the
    full extracted text, still capped at 30 pages by default."""
    calls = _stub_pdf(monkeypatch, _PAPER)
    out = N._read_paper({"doi": "10.1/x"})
    assert out.startswith("[12 pages; extracted 12] source: https://example.org/paper.pdf")
    assert "Intro line" in out and "End" in out
    assert calls[0]["max_pages"] == 30


def test_read_paper_with_pattern_returns_only_matching_lines(monkeypatch):
    """With a pattern, read_paper must return the grep shape — matching lines only, no
    full-text dump — and search every page, since a stat can sit past page 30."""
    calls = _stub_pdf(monkeypatch, _PAPER)
    out = N._read_paper({"doi": "10.1/x", "pattern": r"n50"})
    assert "2 match(es) for /n50/" in out
    assert "The contig N50 was 1.2 Mb" in out and "assembly n50 checked" in out
    assert "Intro line" not in out and "Methods" not in out
    assert calls[0]["max_pages"] is None, "a pattern search must not stop at page 30"


def test_read_paper_pattern_honours_ignore_case_and_max_pages(monkeypatch):
    """The two args carried over from fulltext_grep must still do something: an explicit
    max_pages must reach the fetch, and ignore_case=False must not match the wrong case."""
    calls = _stub_pdf(monkeypatch, _PAPER)
    out = N._read_paper({"doi": "10.1/x", "pattern": r"n50", "ignore_case": False,
                         "max_pages": 3})
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
                        lambda doi, url, max_pages: (None, "", 0, 0, "error: provide 'doi' or 'url'"))
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
