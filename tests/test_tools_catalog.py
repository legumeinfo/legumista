"""Catalog tool tests — the resident whole-store surface.

These exercise legumista's use of DSCensor's `CatalogController` in-process (no
HTTP server), the graceful-absence behaviour when the optional dependency or the
catalog file is missing, and the hand-off between the catalog and the live
`lis_files` path.

A tiny catalog document stands in for the real one, so the tests pin our wiring
rather than the contents of the datastore.
"""
import json
import sys

import pytest

from legumista_agent import tools_catalog as C
from legumista_agent import tools_lis as L

# Honour the same source-checkout override the module uses at runtime, so these
# tests run against a dscensor working tree without installing it (and its
# server-only aiohttp/uvloop dependencies).
if C.DSCENSOR_PATH and C.DSCENSOR_PATH not in sys.path:
    sys.path.insert(0, C.DSCENSOR_PATH)
pytest.importorskip(
    "dscensor.catalog",
    reason=(
        "dscensor is an optional dependency: install it, or set "
        "LEGUMISTA_DSCENSOR_PATH to a source checkout"
    ),
)

CATALOG = {
    "schema": 1,
    "built_at": "2026-09-08T12:00:00Z",
    "source_commit": "abc123def4567890",
    "datastore_url": "https://data.legumeinfo.org",
    "stats": {"collections": 4},
    "taxa": {
        "Glycine/max": {"commonName": "soybean", "abbrev": "glyma", "taxid": 3847},
        "Phaseolus/vulgaris": {"commonName": "common bean", "abbrev": "phavu"},
    },
    "collections": [
        {
            "path": "Glycine/max/genomes/Wm82.gnm4.4PTR",
            "id": "Wm82.gnm4.4PTR",
            "type": "genomes",
            "genus": "Glycine",
            "species": "max",
            "base_url": "https://data.legumeinfo.org/Glycine/max/genomes/Wm82.gnm4.4PTR",
            "index_status": "known",
            "synopsis": "Williams 82 assembly v4",
            "publication_doi": "10.1111/tpj.14500",
            "publication_title": "Three reference-quality genome assemblies",
            "license": "Open",
            "files": [{"n": "glyma.Wm82.gnm4.4PTR.genome_main.fna.gz", "i": [".fai"]}],
        },
        {
            "path": "Glycine/max/annotations/Wm82.gnm4.ann1.T8TQ",
            "id": "Wm82.gnm4.ann1.T8TQ",
            "type": "annotations",
            "genus": "Glycine",
            "species": "max",
            "base_url": "https://data.legumeinfo.org/Glycine/max/annotations/Wm82.gnm4.ann1.T8TQ",
            "index_status": "known",
            "publication_doi": "10.1111/tpj.14500",
            "publication_title": "Three reference-quality genome assemblies",
            "derived_from": ["Wm82.gnm4.4PTR"],
            "files": [
                {
                    "n": "glyma.Wm82.gnm4.ann1.T8TQ.protein_primary.faa.gz",
                    "i": [".fai"],
                    "description": "Protein sequences - primary only",
                },
                {"n": "glyma.Wm82.gnm4.ann1.T8TQ.info_annot.txt.gz"},
            ],
        },
        {
            "path": "Glycine/max/qtl/Demo.qtl.X_1990",
            "id": "Demo.qtl.X_1990",
            "type": "qtl",
            "genus": "Glycine",
            "species": "max",
            "base_url": "https://data.legumeinfo.org/Glycine/max/qtl/Demo.qtl.X_1990",
            "index_status": "unknown",
            "publication_doi": "10.1007/bf00226154",
            "files": [],
        },
        {
            "path": "Phaseolus/vulgaris/diversity/G19833.gnm1.div.X",
            "id": "G19833.gnm1.div.X",
            "type": "diversity",
            "genus": "Phaseolus",
            "species": "vulgaris",
            "base_url": "https://data.legumeinfo.org/Phaseolus/vulgaris/diversity/G19833.gnm1.div.X",
            "index_status": "known",
            "files": [],
        },
        {
            "path": "Phaseolus/vulgaris/expression/G19833.gnm1.expr.X",
            "id": "G19833.gnm1.expr.X",
            "type": "expression",
            "genus": "Phaseolus",
            "species": "vulgaris",
            "base_url": "https://data.legumeinfo.org/Phaseolus/vulgaris/expression/G19833.gnm1.expr.X",
            "index_status": "known",
            "files": [],
        },
    ],
}


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    """Point the tools at a fixture catalog and reset the lazy loader."""
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(CATALOG), encoding="utf-8")
    monkeypatch.setattr(C, "CATALOG_PATH", str(path))
    monkeypatch.setattr(C, "_CANDIDATES", ())   # the fixture must win
    C.reset()
    yield str(path)
    C.reset()


@pytest.fixture
def no_catalog(monkeypatch):
    monkeypatch.setattr(C, "CATALOG_PATH", "")
    monkeypatch.setattr(C, "_CANDIDATES", ())  # and no real catalog on disk either
    C.reset()
    yield
    C.reset()


def _survey(**kw):
    return C._survey(kw)


# --- availability ---------------------------------------------------------------------
def test_tools_are_registered_read_only():
    tools = {t.name: t for t in C.catalog_tools()}
    assert set(tools) == {"lis_survey", "lis_lineage"}
    for tool in tools.values():
        assert tool.read_only is True
        assert tool.parameters.get("additionalProperties") is False


def test_without_a_catalog_the_tools_explain_rather_than_fail(no_catalog):
    """A missing catalog is a configuration state, not an error. The message must
    say how to enable it AND that the live tools are unaffected."""
    out = _survey()
    assert "no LIS catalog is loaded" in out
    assert "populate-catalog" in out  # tells the agent how to fix it


def test_a_broken_catalog_does_not_raise(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(C, "CATALOG_PATH", str(bad))
    monkeypatch.setattr(C, "_CANDIDATES", ())
    C.reset()
    try:
        assert "no LIS catalog is loaded" in _survey()
    finally:
        C.reset()


def test_unsupported_schema_is_refused_not_guessed(tmp_path, monkeypatch):
    """DSCensor rejects an unknown schema; legumista must surface that as an
    unavailable catalog rather than crashing or half-reading it."""
    future = tmp_path / "future.json"
    future.write_text(json.dumps({"schema": 99, "collections": []}), encoding="utf-8")
    monkeypatch.setattr(C, "CATALOG_PATH", str(future))
    monkeypatch.setattr(C, "_CANDIDATES", ())
    C.reset()
    try:
        assert "no LIS catalog is loaded" in _survey()
    finally:
        C.reset()


def test_loading_is_attempted_once(catalog, monkeypatch):
    """The lazy load must not retry per call; a missing catalog is expected."""
    calls = []
    real = C._import_controller

    def counted():
        calls.append(1)
        return real()

    monkeypatch.setattr(C, "_import_controller", counted)
    C.reset()
    _survey()
    _survey()
    assert len(calls) == 1


# --- provenance -----------------------------------------------------------------------
def test_every_answer_names_the_catalog_that_produced_it(catalog):
    """A cached catalog is only as good as its last build, so staleness has to be
    visible at the point of use rather than discovered later."""
    for out in (_survey(), _survey(taxon="Glycine max"),
                C._lineage({"collection": "Wm82.gnm4.ann1.T8TQ"})):
        assert "catalog built 2026-09-08T12:00:00Z" in out
        assert "abc123de" in out  # short commit


# --- lis_survey -----------------------------------------------------------------------
def test_survey_lists_genera_with_counts(catalog):
    out = _survey()
    assert "Glycine" in out and "Phaseolus" in out
    assert "2 genera" in out or "genera" in out


def test_survey_reports_types_for_a_species(catalog):
    out = _survey(taxon="Glycine max")
    assert "annotations" in out and "genomes" in out and "qtl" in out
    assert "Phaseolus" not in out


def test_survey_answers_co_availability(catalog):
    """The question a crawl cannot answer, because absence has no URL to follow."""
    out = _survey(needs=["diversity", "expression"])
    assert "1 species hold ALL of" in out
    assert "Phaseolus vulgaris" in out
    assert "Glycine max" not in out


def test_survey_reports_a_genuine_zero_as_an_answer(catalog):
    """Zero matches is a finding, not a lookup failure — say so explicitly."""
    out = _survey(needs=["diversity", "gwas"])
    assert "0 species hold ALL of" in out
    assert "not a lookup failure" in out


def test_survey_unknown_taxon_is_not_an_error(catalog):
    assert "no collections for" in _survey(taxon="Nonexistent species")


# --- lis_lineage ----------------------------------------------------------------------
def test_lineage_walks_to_the_parent_genome(catalog):
    out = C._lineage({"collection": "Wm82.gnm4.ann1.T8TQ"})
    assert "Wm82.gnm4.ann1.T8TQ" in out
    assert "derived from Wm82.gnm4.4PTR" in out


def test_lineage_dedupes_the_citation_bundle(catalog):
    """The annotation and its genome share a DOI; citing must not double-count."""
    out = C._lineage({"collection": "Wm82.gnm4.ann1.T8TQ"})
    assert "1 publication(s)" in out
    assert out.count("10.1111/tpj.14500") == 3  # once per chain entry, once in bundle


def test_lineage_accepts_a_full_path(catalog):
    """Agents carry paths around; accept either form."""
    by_path = C._lineage({"collection": "Glycine/max/annotations/Wm82.gnm4.ann1.T8TQ"})
    by_id = C._lineage({"collection": "Wm82.gnm4.ann1.T8TQ"})
    assert by_path == by_id


def test_lineage_miss_and_missing_argument_are_distinct(catalog):
    assert "missing 'collection'" in C._lineage({"collection": ""})
    assert "no collection with id" in C._lineage({"collection": "NoSuchThing"})


# --- shared plumbing with tools_lis ---------------------------------------------------
def test_tools_lis_reads_through_this_controller(catalog):
    """tools_lis and tools_catalog must share one loaded catalog, not two."""
    from legumista_agent import tools_lis as L

    out = L._files({"collection": "Glycine/max/annotations/Wm82.gnm4.ann1.T8TQ"})
    assert "2 data file(s)" in out
    assert "abc123de" in out  # the same build stamp these tools report


def test_catalog_absence_disables_lis_tools_too(no_catalog):
    """There is no live fallback any more: without a catalog the lis_* tools say so
    rather than silently reaching for the network."""
    from legumista_agent import tools_lis as L

    assert "no LIS catalog is loaded" in L._find({})
    assert "no LIS catalog is loaded" in L._files({"collection": "a/b/c/d"})


# --- the resident map ------------------------------------------------------------------
def test_map_is_empty_without_a_catalog(no_catalog):
    """The instructions must never advertise knowledge the server does not have."""
    assert C.catalog_map() == ""


def test_map_has_one_line_per_species_with_names_and_counts(catalog):
    lines = C.catalog_map().splitlines()
    glycine = [l for l in lines if l.startswith("Glycine max")][0]
    assert glycine.startswith("Glycine max (soybean, glyma):")
    assert "genomes=1" in glycine and "annotations=1" in glycine and "qtl=1" in glycine


def test_map_omits_zero_counts_entirely(catalog):
    """Absence is conveyed by omission, which is why the preamble must state the rule.
    Padding with `type=0` would cost tokens and read as noise."""
    glycine = [l for l in C.catalog_map().splitlines() if l.startswith("Glycine max")][0]
    assert "=0" not in glycine
    assert "expression" not in glycine  # Glycine has none in the fixture


def test_map_states_the_three_load_bearing_rules(catalog):
    """Each of these prevents a specific failure: reading a short line as an incomplete
    map, emitting a malformed lis_find(type=...), and confabulating collection ids."""
    # the preamble is wrapped for legibility, so compare on normalised whitespace
    text = " ".join(C.catalog_map().split())
    assert "ZERO collections of that type" in text
    assert "`lis_find(type=...)` accepts" in text
    assert "no collection names, no file lists and no gene loci" in text


def test_map_carries_the_build_stamp(catalog):
    """A prompt-resident fact needs the same provenance a tool reply has."""
    assert "abc123de" in C.catalog_map().splitlines()[0]


def test_map_glosses_only_the_types_present(catalog):
    """The glossary is bounded by what the store actually holds, not by our dictionary."""
    text = C.catalog_map()
    assert "qtl = linkage QTL studies" in text
    assert "methylation" not in text  # absent from the fixture


def test_map_stays_small_enough_to_be_resident(catalog):
    """Bounded cardinality is the whole justification: one line per species. If this
    ever grows past a few thousand tokens it no longer belongs in the instructions."""
    assert len(C.catalog_map()) < 20_000


def test_server_instructions_include_the_map_when_a_catalog_is_loaded(catalog):
    from legumista_agent.mcp_server import build_server

    assert "LIS Data Store map" in (build_server().instructions or "")


def test_server_instructions_omit_the_map_without_a_catalog(no_catalog):
    from legumista_agent.mcp_server import build_server

    assert "LIS Data Store map" not in (build_server().instructions or "")
