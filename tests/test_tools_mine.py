"""LIS InterMine tool tests — PathQuery construction, InterMine's in-body failure
reporting, assembly disambiguation, result capping, catalog-driven mine routing and the
response cache.

No network: `_get` is stubbed with a fake mine that records the URLs it was asked for, so
these pin OUR query construction and result handling rather than the mine's uptime."""
import json
import sys
import urllib.parse

import pytest

from legumista_agent import tools_catalog as C
from legumista_agent import tools_mine as M

# Same source-checkout override the module honours at runtime, so the catalog-backed tests
# run against a dscensor working tree without installing it (and its server-only deps).
if C.DSCENSOR_PATH and C.DSCENSOR_PATH not in sys.path:
    sys.path.insert(0, C.DSCENSOR_PATH)


@pytest.fixture(autouse=True)
def clean():
    """Every test starts with an empty response cache and NO catalog loaded.

    Prevents two cross-test leaks that would make results depend on test order: a cached
    response answering the next test's identical query without touching the fake mine,
    and the repo's real catalog.json silently steering mine routing and symbol lookup."""
    saved = (C.CATALOG_PATH, C._CANDIDATES)
    C.CATALOG_PATH, C._CANDIDATES = "", ()
    C.reset()
    M.reset_cache()
    yield
    C.CATALOG_PATH, C._CANDIDATES = saved
    C.reset()
    M.reset_cache()


@pytest.fixture
def catalog(tmp_path, clean):
    """A catalog holding one taxon WITH a mine, one WITHOUT, one curated symbol and one
    gwas collection — enough to pin the wiring without depending on store contents."""
    pytest.importorskip(
        "dscensor.catalog",
        reason="dscensor is optional: set LEGUMISTA_DSCENSOR_PATH to a source checkout")
    document = {
        "schema": 1,
        "built_at": "2026-09-08T12:00:00Z",
        "source_commit": "abc123def4567890",
        "stats": {"collections": 1},
        "taxa": {
            "Glycine/max": {
                "abbrev": "glyma",
                "resources": [{"name": "GlycineMine",
                               "URL": "https://mines.legumeinfo.org/glycinemine/begin.do"}],
            },
            # Split across dicts, exactly as the real catalog stores Aeschynomene's — the
            # URL must still be found when it has no 'name' beside it.
            "Phaseolus/vulgaris": {"abbrev": "phavu"},
            "Phaseolus": {"resources": [
                {"name": "PhaseolusMine"},
                {"URL": "https://mines.legumeinfo.org/phaseolusmine/begin.do"}]},
            "Vicia/villosa": {"abbrev": "vicvi", "resources": [
                {"name": "Genome Context Viewer",
                 "URL": "https://gcv.legumeinfo.org/gene;lis=vicvi.X"}]},
        },
        "gene_symbols": {
            "glyma": {"gmnark": {"gene": "glyma.Wm82.gnm4.ann1.Glyma.12G040000",
                                 "doi": "10.1126/science.1077937",
                                 "synopsis": "Long-distance control of nodulation."}},
        },
        "collections": [{
            "path": "Glycine/max/gwas/mixed.gwas.Bandillo_Jarquin_2015",
            "id": "mixed.gwas.Bandillo_Jarquin_2015",
            "type": "gwas", "genus": "Glycine", "species": "max",
            "base_url": "https://data.legumeinfo.org/Glycine/max/gwas/"
                        "mixed.gwas.Bandillo_Jarquin_2015",
            "index_status": "known", "files": [],
        }],
    }
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(document))
    C.CATALOG_PATH = str(path)
    C.reset()
    M.reset_cache()
    yield document
    C.reset()


def _parse(url):
    """Pull the PathQuery XML and params back out of a request URL."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return q.get("query", [""])[0], q


@pytest.fixture
def mine(monkeypatch):
    """A fake mine. `state['body']` is what the next JSON query returns; `state['urls']`
    records every request so tests can assert on the query that was built.

    `state['live']` is the set of mine names whose /service/version answers — the fake
    store of which mines exist. A name outside it raises, exactly as a real 404 does, so
    the existence probe can be exercised without the network."""
    state = {"urls": [], "count": "7", "body": None,
             "live": {"glycinemine", "phaseolusmine", "cajanusmine", "lensmine",
                      "legumemine"}}

    def default_body():
        return {"wasSuccessful": True,
                "columnHeaders": ["Gene > Name", "Gene > Assembly Version",
                                  "Gene > Proteins > Identifier"],
                "results": [["Glyma.12G040000", "gnm4", "glyma.Wm82.gnm4.ann1.X.1"]]}

    def fake_get(url, accept="application/json"):
        state["urls"].append(url)
        if url.endswith("/version"):
            name = url.rsplit("/service/", 1)[0].rsplit("/", 1)[-1]
            if name not in state["live"]:
                raise OSError(f"HTTP Error 404: {name}")
            return "5.0.0"
        if "format=count" in url:
            return state["count"]
        return json.loads(json.dumps(state["body"] if state["body"] is not None
                                     else default_body()))

    monkeypatch.setattr(M, "_get", fake_get)
    monkeypatch.setattr(M, "_validate_url", lambda u: None)
    return state


# --- PathQuery construction -----------------------------------------------------------
def test_pathquery_uses_lookup_for_the_gene():
    """LOOKUP is the whole reason the caller never has to choose between
    primaryIdentifier / secondaryIdentifier / name."""
    xml = M._pathquery(["Gene.name"], M._gene_constraints({"gene": "Glyma.12G040000"}))
    assert 'path="Gene" op="LOOKUP" value="Glyma.12G040000"' in xml
    assert 'view="Gene.name"' in xml


def test_pathquery_escapes_values_so_a_gene_name_cannot_inject():
    """Gene names are model-supplied; a value must not be able to close the tag."""
    payload = '"/><constraint path="Gene.name" op="=" value="x'
    xml = M._pathquery(["Gene.name"], [("Gene", "LOOKUP", payload)])
    # quoteattr may single-quote rather than emit &quot;, so assert the property that
    # matters: the payload injected no second constraint and no raw markup survived.
    assert xml.count("<constraint") == 1
    assert xml.count("</query>") == 1
    assert "/><constraint" not in xml.replace("/></query>", "")
    assert "&gt;" in xml and "&lt;" in xml


def test_assembly_and_annotation_become_extra_constraints():
    cons = M._gene_constraints({"gene": "G", "assembly": "gnm4", "annotation": "ann1"})
    assert ("Gene.assemblyVersion", "=", "gnm4") in cons
    assert ("Gene.annotationVersion", "=", "ann1") in cons
    assert M._gene_constraints({"gene": "G"}) == [("Gene", "LOOKUP", "G")]


def test_expression_is_rooted_at_expressionvalue(mine):
    """Gene has no expression collection, so the gene must be reached through
    ExpressionValue.feature - rooting at Gene would be a model error."""
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["ExpressionValue > Value"],
                    "results": [["19.44"]]}
    M._gene_expression({"gene": "Glyma.12G040000"})
    xml, params = _parse(mine["urls"][0])
    assert 'path="ExpressionValue.feature" op="LOOKUP"' in xml
    assert "sortOrder=\"ExpressionValue.value desc\"" in xml


# --- InterMine reports failure inside a 200 body ---------------------------------------
def test_failed_query_is_not_reported_as_no_data(mine):
    """The trap: HTTP 200, results: [], and the reason only in wasSuccessful/error. A
    client that trusts `results` says 'no data' when the truth is 'bad query'."""
    mine["body"] = {"wasSuccessful": False, "results": [],
                    "error": "There isn't a specified constraint value and operation for "
                             "path Gene.assemblyVersion in the request; this constraint "
                             "is required."}
    out = M._gene_proteins({"gene": "Glyma.12G040000"})
    assert "rejected the query" in out
    assert "Gene.assemblyVersion" in out
    assert "no matches" not in out          # must NOT be mistaken for an empty result


def test_service_outage_is_distinguished_from_a_bad_query(mine):
    """legumemine currently answers every query this way; the agent should be told to
    switch mines rather than to fix its query."""
    mine["body"] = {"wasSuccessful": False, "results": [],
                    "error": "Service failed. Please contact support."}
    out = M._gene_families({"gene": "Glyma.12G040000"})
    assert "query service is failing, not your query" in out
    assert "try another mine" in out


def test_valid_but_empty_is_its_own_message(mine):
    mine["body"] = {"wasSuccessful": True, "columnHeaders": [], "results": []}
    out = M._gene_ontology({"gene": "NoSuchGene"})
    assert "no matches" in out and "query was valid" in out
    assert "rejected" not in out


def test_non_dict_response_is_an_error_not_a_crash(mine):
    mine["body"] = ["unexpected"]
    assert "unexpected" in M._gene_proteins({"gene": "G"}).lower()


# --- assembly disambiguation ----------------------------------------------------------
def test_multiple_assemblies_are_flagged(mine):
    """One bare gene name matches gnm2/gnm4/gnm6 - three different loci. Mixing them
    silently would be a correctness bug in whatever the caller does next."""
    mine["body"] = {"wasSuccessful": True,
                    "columnHeaders": ["Gene > Name", "Gene > Assembly Version"],
                    "results": [["G", "gnm2"], ["G", "gnm4"], ["G", "gnm6"]]}
    out = M._gene_proteins({"gene": "G"})
    assert "matched 3 assemblies" in out and "gnm2, gnm4, gnm6" in out


def test_single_assembly_is_not_flagged(mine):
    mine["body"] = {"wasSuccessful": True,
                    "columnHeaders": ["Gene > Name", "Gene > Assembly Version"],
                    "results": [["G", "gnm4"]]}
    assert "matched" not in M._gene_proteins({"gene": "G"})


def test_expression_does_not_claim_assemblies(mine):
    """Expression rows carry no assembly column; index 1 is the sample id, so the
    assembly check must be disabled rather than reading the wrong column."""
    mine["body"] = {"wasSuccessful": True,
                    "columnHeaders": ["Feature > Name", "Sample > Identifier"],
                    "results": [["G", "SRR1"], ["G", "SRR2"]]}
    assert "assemblies" not in M._gene_expression({"gene": "G"})


# --- capping and counts ---------------------------------------------------------------
def test_capped_results_report_the_true_total(mine):
    """639 expression values for one gene - truncating without saying so would let the
    agent believe it saw everything."""
    mine["count"] = "639"
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["V"],
                    "results": [[str(i)] for i in range(2)]}
    out = M._gene_expression({"gene": "G", "max_results": 2})
    assert "of 639" in out and "raise 'max_results'" in out


def test_no_count_request_when_results_are_under_the_cap(mine):
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["V"], "results": [["1"]]}
    M._gene_proteins({"gene": "G", "max_results": 50})
    assert not any("format=count" in u for u in mine["urls"])


def test_a_failed_count_does_not_sink_the_query(mine):
    mine["count"] = "not-a-number"
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["V"], "results": [["1"]]}
    out = M._gene_expression({"gene": "G", "max_results": 1})
    assert "Expression values" in out and "error" not in out.lower()


def test_max_results_is_clamped(mine):
    M._gene_proteins({"gene": "G", "max_results": 99999})
    assert "size=500" in mine["urls"][0]


# --- mine selection -------------------------------------------------------------------
def test_default_mine_is_used_and_named(mine):
    out = M._gene_proteins({"gene": "G"})
    assert f"[mine: {M.MINE}]" in out
    assert f"/{M.MINE}/service" in mine["urls"][0]


def test_mine_override(mine):
    """The PathQueries are mine-agnostic, so switching to legumemine is one argument."""
    out = M._gene_proteins({"gene": "G", "mine": "legumemine"})
    assert "[mine: legumemine]" in out
    assert "/legumemine/service" in mine["urls"][0]


def test_missing_gene_is_rejected(mine):
    for fn in (M._gene_proteins, M._gene_families, M._gene_ontology, M._gene_expression):
        assert "missing 'gene'" in fn({"gene": "  "})


# --- registry -------------------------------------------------------------------------
def test_tools_are_read_only_and_well_formed():
    tools = M.mine_tools()
    assert {t.name for t in tools} == {
        "legumemine_gene_proteins", "legumemine_gene_families",
        "legumemine_gene_ontology", "legumemine_gene_expression",
        "legumemine_gene_symbol", "legumemine_gene_orthologs",
        "lis_trait_qtls", "lis_trait_gwas", "lis_marker_position"}
    for t in tools:
        assert t.read_only is True
        assert t.parameters.get("additionalProperties") is False
        # every declared required arg must actually be a declared property
        for req in t.parameters["required"]:
            assert req in t.parameters["properties"], (t.name, req)
    by_name = {t.name: t for t in tools}
    assert by_name["legumemine_gene_symbol"].parameters["required"] == ["symbol"]
    assert by_name["lis_trait_qtls"].parameters["required"] == ["trait"]
    assert by_name["lis_marker_position"].parameters["required"] == ["marker"]
    # orthologs takes gene OR family, so neither can be schema-required
    assert by_name["legumemine_gene_orthologs"].parameters["required"] == []


def test_tools_are_registered_on_the_mcp_server():
    from legumista_agent.mcp_server import _HANDLERS, build_server
    build_server()
    assert {"legumemine_gene_proteins", "legumemine_gene_families",
            "legumemine_gene_ontology", "legumemine_gene_expression"} <= set(_HANDLERS)


# --- taxon -> mine routing ------------------------------------------------------------
def test_taxon_routes_to_the_species_mine():
    """Every LIS mine is '<genus>mine' lower-cased, so no lookup table is needed."""
    assert M._mine_for_taxon("Glycine max") == "glycinemine"
    assert M._mine_for_taxon("Phaseolus vulgaris") == "phaseolusmine"
    assert M._mine_for_taxon("Vigna_unguiculata") == "vignamine"
    assert M._mine_for_taxon("Cicer") == "cicermine"


def test_explicit_mine_beats_taxon():
    mine, err = M._resolve_mine({"taxon": "Glycine max", "mine": "legumemine"})
    assert (mine, err) == ("legumemine", None)


def test_breeding_tools_require_a_taxon(mine):
    """QTL/GWAS/marker classes are absent from legumemine, so defaulting there would
    produce a model error rather than an honest empty result."""
    for fn, arg in ((M._trait_qtls, {"trait": "seed protein"}),
                    (M._trait_gwas, {"trait": "seed protein"}),
                    (M._marker_position, {"marker": "ss715614263"})):
        out = fn(arg)
        assert "missing 'taxon'" in out
        assert "legumemine has none of it" in out
    assert not mine["urls"], "must not query anything before a mine is known"


def test_breeding_tools_use_the_routed_mine(mine):
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["QTL > Name"],
                    "results": [["Seed protein 1-1"]]}
    out = M._trait_qtls({"trait": "seed protein", "taxon": "Phaseolus vulgaris"})
    assert "[mine: phaseolusmine]" in out
    assert "/phaseolusmine/service" in mine["urls"][0]


def test_gene_tools_do_not_require_a_taxon(mine):
    """Gene family / function data IS in legumemine, so those default there."""
    assert f"[mine: {M.MINE}]" in M._gene_proteins({"gene": "G"})


# --- symbol resolution ----------------------------------------------------------------
def test_gene_symbol_queries_genefunction(mine):
    mine["body"] = {"wasSuccessful": True,
                    "columnHeaders": ["GeneFunction > Symbol", "GeneFunction > Gene > Name"],
                    "results": [["GmNARK", "Glyma.12G040000"]]}
    out = M._gene_symbol({"symbol": "GmNARK"})
    xml, _ = _parse(mine["urls"][0])
    assert 'path="GeneFunction.symbol" op="=" value="GmNARK"' in xml
    assert "GeneFunction.publications.doi" in xml   # DOIs are the point of this tool
    assert "Glyma.12G040000" in out


def test_gene_symbol_requires_a_symbol(mine):
    assert "missing 'symbol'" in M._gene_symbol({"symbol": ""})


# --- orthologs ------------------------------------------------------------------------
def test_orthologs_resolves_gene_to_family_first(mine):
    """The caller asks 'orthologs of this gene' — needing the family id up front would be
    asking them for the answer."""
    calls = {"n": 0}

    def body(url):
        calls["n"] += 1
        if calls["n"] == 1:      # gene -> family
            return {"wasSuccessful": True, "columnHeaders": ["Gene Family > Identifier"],
                    "results": [["Legume.fam3.10524"]]}
        return {"wasSuccessful": True,             # family -> members
                "columnHeaders": ["Identifier", "Name", "Genus"],
                "results": [["Legume.fam3.10524", "Ae04g33770", "Aeschynomene"]]}

    original = M._get
    M_get_urls = mine["urls"]

    def fake(url, accept="application/json"):
        M_get_urls.append(url)
        if "format=count" in url:
            return mine["count"]
        return body(url)

    M._get = fake
    try:
        out = M._gene_orthologs({"gene": "Glyma.12G040000"})
    finally:
        M._get = original
    assert "is in gene family Legume.fam3.10524" in out
    assert "Ae04g33770" in out
    xml2, _ = _parse([u for u in M_get_urls if "format=json" in u][1])
    assert 'value="Legume.fam3.10524"' in xml2


def test_orthologs_accepts_a_family_directly(mine):
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["Identifier", "Name"],
                    "results": [["Legume.fam3.10524", "Ae04g33770"]]}
    out = M._gene_orthologs({"family": "Legume.fam3.10524"})
    assert "is in gene family" not in out       # no lookup step was needed
    assert "Ae04g33770" in out


def test_orthologs_without_gene_or_family_is_rejected(mine):
    assert "provide 'gene'" in M._gene_orthologs({})


def test_orthologs_reports_a_gene_with_no_family(mine):
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["X"], "results": []}
    out = M._gene_orthologs({"gene": "Glyma.999G999999"})
    assert "no gene family assignment" in out


# --- column disambiguation ------------------------------------------------------------
def test_duplicate_column_names_are_disambiguated():
    """QTL views select both QTL.name and QTL.linkageGroup.name; printing two columns
    called 'Name' makes the table unreadable to the caller."""
    cols = M._columns(["QTL > Trait > Name", "QTL > Name", "QTL > Linkage Group > Name",
                       "QTL > Lod"])
    assert cols[1] != cols[2]
    assert cols[1] == "QTL Name" and cols[2] == "Linkage Group Name"
    assert cols[3] == "Lod"          # unique names stay short


def test_gwas_is_sorted_most_significant_first(mine):
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["p"], "results": [["1e-11"]]}
    M._trait_gwas({"trait": "seed protein", "taxon": "Glycine max"})
    xml, _ = _parse(mine["urls"][0])
    assert 'sortOrder="GWASResult.pValue asc"' in xml


# --- catalog-driven mine routing ------------------------------------------------------
def test_species_without_a_mine_is_refused_without_a_doomed_query(mine, catalog):
    """The defect this replaced: 'Vicia villosa' guessed 'viciamine', got a raw HTTP 404
    from a full PathQuery, and the agent could not tell 'no such mine' from 'the mine is
    down' — so it retried, or concluded the data does not exist.

    The catalog does not list viciamine, so the name is probed once (/service/version)
    and then refused. What must never happen again is issuing the QUERY itself."""
    out = M._trait_qtls({"trait": "seed protein", "taxon": "Vicia villosa"})
    assert "has no InterMine" in out
    assert "not an outage" in out
    assert "lis_find(taxon='Vicia villosa', type='qtl')" in out
    assert all("/query/results" not in u for u in mine["urls"]), (
        "a species with no mine must never be queried, only probed")


def test_a_live_mine_the_catalog_omits_is_probed_rather_than_refused(mine, catalog):
    """The catalog lists 8 of the 10 live genus mines — cajanusmine and lensmine answer
    /service/version but appear nowhere in it. Refusing on the catalog alone would deny
    pigeonpea and lentil, both real crops with breeding data. A hardcoded allowance would
    drift silently every time LIS adds a mine, so an unknown name is probed instead."""
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["QTL > Name"],
                    "results": [["qSeed-1"]]}
    out = M._trait_qtls({"trait": "seed protein", "taxon": "Cajanus cajan"})
    assert "has no InterMine" not in out
    assert "cajanusmine" in out
    assert any("/query/results" in u for u in mine["urls"]), "the query must be issued"


def test_the_existence_probe_is_asked_once_per_mine(mine, catalog):
    """The probe only pays for itself if it is cached; otherwise every call to a
    catalog-unlisted mine costs an extra round trip."""
    M._trait_qtls({"trait": "seed", "taxon": "Vicia villosa"})
    M._trait_qtls({"trait": "pod", "taxon": "Vicia villosa"})
    probes = [u for u in mine["urls"] if u.endswith("/version")]
    assert len(probes) == 1, f"expected one probe, got {len(probes)}"


def test_a_species_with_a_mine_still_routes_there(mine, catalog):
    """Refusing unknown mines must not refuse the real ones: the catalog publishes
    glycinemine and phaseolusmine, and both must still be reached."""
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["QTL > Name"],
                    "results": [["Seed protein 1-1"]]}
    assert "[mine: glycinemine]" in M._trait_qtls({"trait": "protein",
                                                   "taxon": "Glycine max"})
    assert "[mine: phaseolusmine]" in M._trait_qtls({"trait": "protein",
                                                     "taxon": "Phaseolus vulgaris"})
    assert all("/glycinemine/" in u or "/phaseolusmine/" in u for u in mine["urls"])


def test_known_mines_reads_urls_not_names(catalog):
    """Phaseolus' resource is split across dicts in the catalog (a name-only entry and a
    URL-only entry, as Aeschynomene really is stored); keying on 'name' would lose it."""
    known = M._known_mines()
    assert "phaseolusmine" in known and "glycinemine" in known
    assert "viciamine" not in known
    # legumemine belongs to no genus, so nothing lists it — it must be added by hand or
    # every default-mine gene query would be refused.
    assert M.MINE.lower() in known


def test_mines_absent_from_the_catalog_are_still_reachable(catalog):
    """cajanusmine and lensmine answer /service/version but appear nowhere in the
    catalog's resources. Trusting the catalog alone would refuse pigeonpea and lentil —
    two real mines with breeding data."""
    assert M._resolve_mine({"taxon": "Cajanus cajan"}) == ("cajanusmine", None)
    assert M._resolve_mine({"taxon": "Lens culinaris"}) == ("lensmine", None)


def test_without_a_catalog_routing_falls_back_to_the_guess(mine):
    """The catalog is optional. With none loaded we cannot know which mines exist, so the
    tools must keep working on the genus guess rather than refuse everything."""
    assert M._known_mines() is None
    assert M._resolve_mine({"taxon": "Vicia villosa"}) == ("viciamine", None)
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["QTL > Name"],
                    "results": [["X"]]}
    out = M._trait_qtls({"trait": "protein", "taxon": "Vicia villosa"})
    assert "[mine: viciamine]" in out and "/viciamine/service" in mine["urls"][0]


def test_an_explicit_mine_is_never_vetoed(mine, catalog):
    """'mine' is the caller's own assertion — a brand-new mine the catalog predates must
    not be blocked by our staleness."""
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["Gene > Name"],
                    "results": [["G"]]}
    out = M._gene_proteins({"gene": "G", "mine": "brandnewmine"})
    assert "[mine: brandnewmine]" in out


# --- response cache -------------------------------------------------------------------
def test_an_identical_query_is_not_reissued(mine):
    """Measured: a 5-call agent sequence about one gene made 7 requests, one an exact
    duplicate. A second identical call must cost nothing."""
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["Gene > Name"],
                    "results": [["Glyma.12G040000"]]}
    first = M._gene_families({"gene": "Glyma.12G040000"})
    assert len(mine["urls"]) == 1
    assert M._gene_families({"gene": "Glyma.12G040000"}) == first
    assert len(mine["urls"]) == 1, "the second identical query must not hit the network"


def test_the_cache_key_separates_mine_query_and_size(mine):
    """A cache that ignored any of the three would answer one question with another's
    rows — worse than no cache at all."""
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["Gene > Name"],
                    "results": [["G"]]}
    M._gene_families({"gene": "G"})
    M._gene_families({"gene": "G", "mine": "glycinemine"})   # different mine
    M._gene_families({"gene": "OTHER"})                      # different xml
    M._gene_families({"gene": "G", "max_results": 3})        # different size
    assert len({u for u in mine["urls"]}) == 4


def test_a_repeated_ortholog_call_reuses_both_of_its_round_trips(mine):
    """legumemine_gene_orthologs is the two-request tool (gene->family, then family->
    members), so an agent circling back to it is where duplicate traffic accumulates.

    NOTE: it still cannot reuse legumemine_gene_families' response — that tool selects
    five views and this step selects one, so the PathQueries differ and the cache key
    (mine, xml, size) rightly separates them."""
    mine["body"] = {"wasSuccessful": True,
                    "columnHeaders": ["Gene Family > Identifier"],
                    "results": [["Legume.fam3.10524"]]}
    M._gene_orthologs({"gene": "Glyma.12G040000"})
    before = len(mine["urls"])
    M._gene_orthologs({"gene": "Glyma.12G040000"})
    assert len(mine["urls"]) == before


def test_errors_are_not_cached_so_a_retry_really_retries(mine):
    """Caching a transient outage would make it permanent for the process, and the
    'retry later' advice we hand back would be a lie."""
    mine["body"] = {"wasSuccessful": False, "results": [],
                    "error": "Service failed. Please contact support."}
    assert "query service is failing" in M._gene_families({"gene": "G"})
    assert len(mine["urls"]) == 1
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["Gene > Name"],
                    "results": [["G"]]}
    out = M._gene_families({"gene": "G"})
    assert len(mine["urls"]) == 2, "the retry must actually reach the mine"
    assert "Gene families" in out and "error" not in out.lower()


def test_a_failed_count_is_not_cached(mine):
    """Same rule for the pre-flight count: a failed count must not pin 'unknown total'
    onto every later call for the same query."""
    mine["count"] = "boom"
    assert M._count("glycinemine", "<query/>") is None
    mine["count"] = "639"
    assert M._count("glycinemine", "<query/>") == 639
    assert M._count("glycinemine", "<query/>") == 639
    assert sum("format=count" in u for u in mine["urls"]) == 2


def test_reset_cache_clears_it(mine):
    """The hook tests rely on; without it every test would inherit the last one's rows."""
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["Gene > Name"],
                    "results": [["G"]]}
    M._gene_families({"gene": "G"})
    M.reset_cache()
    M._gene_families({"gene": "G"})
    assert len(mine["urls"]) == 2


# --- symbol precedence ----------------------------------------------------------------
def test_symbol_resolution_prefers_the_curated_catalog(mine, catalog):
    """The catalog answers offline, instantly, with a FULLY QUALIFIED gene id — no mine
    round trip and no assembly ambiguity to resolve afterwards."""
    out = M._gene_symbol({"symbol": "GmNARK"})
    assert "glyma.Wm82.gnm4.ann1.Glyma.12G040000" in out
    assert "10.1126/science.1077937" in out
    assert not mine["urls"], "a curated hit must not query the mine"


def test_a_catalog_answer_says_it_came_from_the_catalog(mine, catalog):
    """Mislabelling the source would let a reader attribute a curated claim to the mine's
    own curation, which is a different (broader) body of evidence."""
    out = M._gene_symbol({"symbol": "gmnark"})       # matching is case-insensitive
    assert "curated LIS catalog" in out
    assert "NOT a mine query" in out
    assert "[mine:" not in out


def test_a_symbol_the_catalog_lacks_falls_through_to_the_mine(mine, catalog):
    """The catalog holds 344 symbols; the mine's curation is broader, so a miss must not
    become 'no such symbol'."""
    mine["body"] = {"wasSuccessful": True,
                    "columnHeaders": ["GeneFunction > Symbol", "GeneFunction > Gene > Name"],
                    "results": [["PvSYMRK", "Phvul.001G001000"]]}
    out = M._gene_symbol({"symbol": "PvSYMRK"})
    assert "Phvul.001G001000" in out
    assert "curated LIS catalog" not in out
    xml, _ = _parse(mine["urls"][0])
    assert 'path="GeneFunction.symbol" op="=" value="PvSYMRK"' in xml


def test_taxon_scopes_the_curated_lookup(mine, catalog):
    """One symbol can be curated in several species; 'taxon' must narrow it rather than
    be ignored — and a taxon with no curated entry falls through to the mine."""
    assert "glyma" in M._gene_symbol({"symbol": "GmNARK", "taxon": "Glycine max"})
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["GeneFunction > Symbol"],
                    "results": [["GmNARK"]]}
    out = M._gene_symbol({"symbol": "GmNARK", "taxon": "Phaseolus vulgaris"})
    assert "curated LIS catalog" not in out and mine["urls"]


# --- the GWAS -> lis_files bridge -----------------------------------------------------
def _gwas_body():
    return {"wasSuccessful": True,
            "columnHeaders": ["GWAS Result > Trait > Name", "GWAS Result > Marker Name",
                              "GWAS Result > P Value", "GWAS Result > GWAS > Identifier"],
            "results": [["seed protein", "ss715614263", "1e-11",
                         "mixed.gwas.Bandillo_Jarquin_2015"]]}


def test_gwas_output_hands_the_study_id_to_lis_files(mine, catalog):
    """The study identifiers ARE Data Store collection names, but that was only stated in
    a docstring the model never sees — so the association result and the underlying data
    stayed one undiscovered call apart."""
    mine["body"] = _gwas_body()
    out = M._trait_gwas({"trait": "seed protein", "taxon": "Glycine max"})
    assert "lis_files(collection='mixed.gwas.Bandillo_Jarquin_2015')" in out


def test_an_identifier_absent_from_the_catalog_is_not_claimed(mine, catalog):
    """Do not promise data we can see is not there: the catalog knows every gwas
    collection, so an unmatched identifier is reported as unmatched."""
    body = _gwas_body()
    body["results"] = [["seed protein", "m1", "1e-9", "mixed.gwas.Nobody_2099"]]
    mine["body"] = body
    out = M._trait_gwas({"trait": "seed protein", "taxon": "Glycine max"})
    assert "No gwas/ collection in the catalog is named" in out
    assert "lis_files(collection='mixed.gwas.Nobody_2099')" not in out


def test_without_a_catalog_the_bridge_is_a_suggestion_not_a_claim(mine):
    """With nothing to check against, the hand-off must be offered without asserting the
    collection exists."""
    mine["body"] = _gwas_body()
    out = M._trait_gwas({"trait": "seed protein", "taxon": "Glycine max"})
    assert "may reach the underlying data" in out and "Not verified" in out
    assert "ARE LIS Data Store" not in out


def test_the_bridge_is_absent_when_there_are_no_rows(mine, catalog):
    """An empty result must not carry a hand-off to a collection nobody named."""
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["x"], "results": []}
    out = M._trait_gwas({"trait": "nothing", "taxon": "Glycine max"})
    assert "lis_files" not in out and "no matches" in out
