"""verify_ids — extraction and per-kind verdicts, fully offline (every lookup stubbed)."""
from types import SimpleNamespace

from legumista_agent import pubstatus, tools_catalog, tools_mine
from legumista_agent import tools_verify as V

DRAFT = """Soybean NARK (glyma.Wm82.gnm4.ann1.Glyma.12G040000) was described by
Searle et al. (doi:10.1126/science.1077937). See Wm82.gnm4.ann1.T8TQ and GCF_000004515.6.
A retracted claim: https://doi.org/10.1177/1758835920922055."""


def test_extraction_finds_each_kind_once_and_strips_punctuation():
    ids = V.extract_ids(DRAFT)
    assert ids["dois"] == ["10.1126/science.1077937", "10.1177/1758835920922055"]
    assert ids["genes"] == ["glyma.Wm82.gnm4.ann1.Glyma.12G040000"]
    assert ids["collections"] == ["Wm82.gnm4.ann1.T8TQ"]     # not the gene's prefix
    assert ids["assemblies"] == ["GCF_000004515.6"]


def _stub(monkeypatch):
    statuses = {
        "10.1126/science.1077937": {"status": "ok", "notices": [], "checked": "crossref",
                                    "title": "Long-distance signaling in nodulation directed by a CLAVATA1-like receptor kinase"},
        "10.1177/1758835920922055": pubstatus.from_crossref({"updated-by": [
            {"DOI": "10.1177/17588359231172420", "type": "retraction",
             "source": "retraction-watch", "updated": {"date-parts": [[2023, 4, 22]]}}]}),
        "10.9999/made-up": {"status": "not_found", "notices": [], "checked": "doi.org"},
        "10.5281/zenodo.1234567": {"status": "non_crossref", "notices": [],
                                   "checked": "doi.org"},
    }
    monkeypatch.setattr(pubstatus, "check_doi", lambda d: dict(statuses.get(d, {"status": "unknown", "error": "timeout"})))
    monkeypatch.setattr(tools_mine, "_gene_presence",
                        lambda mine, a: (["glyma.Wm82.gnm4.ann1.Glyma.12G040000"], None))
    ctl = SimpleNamespace(collections=[{"id": "Wm82.gnm4.ann1.T8TQ",
                                        "path": "Glycine/max/annotations/Wm82.gnm4.ann1.T8TQ"}])
    monkeypatch.setattr(tools_catalog, "controller", lambda: ctl)
    monkeypatch.setattr(V, "_cli_raw", lambda argv, **k: (None, "__missing__datasets"))


def test_verdicts_per_kind(monkeypatch):
    _stub(monkeypatch)
    out = V._verify({"text": DRAFT, "dois": ["10.9999/made-up", "10.5281/zenodo.1234567"]})
    assert "DOI 10.1126/science.1077937 — FOUND" in out
    assert "DOI 10.1177/1758835920922055 — FOUND, RETRACTED (retraction 10.1177/17588359231172420" in out
    assert "DOI 10.9999/made-up — NOT FOUND: not registered at doi.org" in out
    # A DataCite DOI (datasets, software) is absent from Crossref but real: never NOT FOUND.
    assert "DOI 10.5281/zenodo.1234567 — FOUND at doi.org, but not a Crossref DOI" in out
    assert "gene glyma.Wm82.gnm4.ann1.Glyma.12G040000 — FOUND" in out
    assert "collection Wm82.gnm4.ann1.T8TQ — FOUND in the LIS catalog" in out
    assert "assembly GCF_000004515.6 — UNCHECKED (the NCBI datasets CLI is not installed)" in out
    assert "1 flagged by a retraction/concern notice" in out
    assert out.startswith("verify_ids — 7 identifier(s): 5 FOUND, 1 NOT FOUND, 1 UNCHECKED")


def test_a_real_doi_with_the_wrong_title_is_a_mismatch(monkeypatch):
    _stub(monkeypatch)
    out = V._verify({"citations": [{"doi": "10.1126/science.1077937",
                                    "title": "Drought tolerance QTL in chickpea"}]})
    assert "MISMATCH" in out


def test_nothing_to_check_is_an_input_error(monkeypatch):
    _stub(monkeypatch)
    out = V._verify({"text": "no identifiers here"})
    assert out.is_error


def test_collection_ids_of_other_shapes_are_found_only_as_catalog_ids():
    """A loose pattern would cut `Tifrunner.gnm2.ann2.expr.Tifrunner.Clevenger_2016` short
    and report the fragment NOT FOUND. Real IDs of every shape are found by lookup, even
    inside a file name; nothing is invented from a partial match."""
    known = {"mixed.gwas.Bandillo_Jarquin_2015",
             "Tifrunner.gnm2.ann2.expr.Tifrunner.Clevenger_2016", "Wm82.gnm4.ann1.T8TQ"}
    text = ("See mixed.gwas.Bandillo_Jarquin_2015 and "
            "Tifrunner.gnm2.ann2.expr.Tifrunner.Clevenger_2016. The BED is "
            "glyma.Wm82.gnm4.ann1.T8TQ.gene_models_main.bed.gz.")
    found = V.extract_ids(text, known)["collections"]
    assert set(found) == known
    assert V.extract_ids(text)["collections"] == ["Wm82.gnm4.ann1.T8TQ"]   # no catalog
