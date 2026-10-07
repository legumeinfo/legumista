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
    and an operator's LEGUMISTA_CATALOG_PATH silently steering mine routing and symbol
    lookup."""
    saved = C.CATALOG_PATH
    C.CATALOG_PATH = ""
    C.reset()
    M.reset_cache()
    yield
    C.CATALOG_PATH = saved
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
                "abbrev": "glyma", "commonName": "soybean",
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


def _members_query(mine):
    """The family-members PathQuery among the requests made (others check the family
    exists in each mine)."""
    for url in mine["urls"]:
        xml, _ = _parse(url)
        if 'view="Gene.geneFamilyAssignments.geneFamily.primaryIdentifier Gene.primary' in xml:
            return xml
    raise AssertionError("no family-members query was made")


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
    state = {"urls": [], "count": "7", "body": None, "model_urls": [],
             "live": {"glycinemine", "phaseolusmine", "cajanusmine", "lensmine",
                      "legumemine"},
             # Each mine's data model, as /service/model reports it: legumemine has no
             # breeding classes, and only it and glycinemine have GeneFunction.
             "models": {"legumemine": {"Gene", "GeneFamily", "GeneFunction",
                                       "ExpressionValue"},
                        "glycinemine": {"Gene", "GeneFamily", "GeneFunction",
                                        "ExpressionValue", "QTL", "GWASResult",
                                        "GeneticMarker"}},
             "genus_model": {"Gene", "GeneFamily", "ExpressionValue", "QTL", "GWASResult",
                             "GeneticMarker"}}

    def default_body():
        return {"wasSuccessful": True,
                "columnHeaders": ["Gene > Name", "Gene > Assembly Version",
                                  "Gene > Proteins > Identifier"],
                "results": [["Glyma.12G040000", "gnm4", "glyma.Wm82.gnm4.ann1.X.1"]]}

    def fake_get(url, accept="application/json"):
        if "/service/model" in url:
            state["model_urls"].append(url)
            name = url.rsplit("/service/", 1)[0].rsplit("/", 1)[-1]
            classes = state["models"].get(name, state["genus_model"])
            return {"model": {"classes": {c: {} for c in classes}}}
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
    assert "sortOrder=\"ExpressionValue.value desc " in xml


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
    # Every query (including the presence LOOKUP) returns zero rows here, so the gene
    # itself is unknown to the mine — which is what the reply must now say.
    assert "no gene matching 'NoSuchGene' in legumemine" in out
    assert "rejected" not in out and not out.startswith("error:")


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
def _paged(monkeypatch, mine, total, fail_at=None, count_fails=False):
    """A mine that honours size and start over `total` rows, fails the request that
    starts at `fail_at`, and with `count_fails` cannot count."""
    def fake(url, accept="application/json"):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if "/service/model" in url:
            return {"model": {"classes": {"Gene": {}, "ExpressionValue": {}}}}
        mine["urls"].append(url)
        if q.get("format") == ["count"]:
            return "unavailable" if count_fails else str(total)
        start, size = int(q.get("start", ["0"])[0]), int(q["size"][0])
        if fail_at is not None and start == fail_at:
            raise TimeoutError("timed out")
        return {"wasSuccessful": True, "columnHeaders": ["V"],
                "results": [[f"v{i:04d}"] for i in range(start, min(total, start + size))]}
    monkeypatch.setattr(M, "_get", fake)


def test_a_large_result_is_fetched_whole_in_chunks(mine, monkeypatch):
    """639 expression values for one gene once came back as the first 50. Every row is
    fetched now, in parallel chunks, and paged from the merged whole."""
    monkeypatch.setattr(M, "FETCH_CHUNK", 10)
    _paged(monkeypatch, mine, 25)
    out = M._gene_expression({"gene": "G", "max_results": 5})
    starts = sorted(int(urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
                        .get("start", ["0"])[0]) for u in mine["urls"] if "format=json" in u)
    assert starts == [0, 10, 20]
    assert "showing 5 of 25 row(s) — continue with offset=5" in out
    nxt = M._gene_expression({"gene": "G", "max_results": 5, "offset": 20})
    assert "showing 21–25 of 25 row(s)" in nxt and "v0024" in nxt
    assert len([u for u in mine["urls"] if "format=json" in u]) == 3, "paging refetched"


def test_the_fetch_limit_is_reported_never_presented_as_the_total(mine, monkeypatch):
    monkeypatch.setattr(M, "FETCH_CHUNK", 10)
    monkeypatch.setattr(M, "FETCH_MAX", 20)
    _paged(monkeypatch, mine, 25)
    out = M._gene_expression({"gene": "G"})
    assert "INCOMPLETE — legumemine: fetched the first 20 of 25 rows" in out


def test_a_failed_chunk_keeps_the_rows_before_it_and_says_so(mine, monkeypatch):
    monkeypatch.setattr(M, "FETCH_CHUNK", 10)
    _paged(monkeypatch, mine, 25, fail_at=10)
    out = M._gene_expression({"gene": "G"})
    assert "INCOMPLETE — legumemine: rows from 10 on could not be fetched" in out
    assert "10 row(s)" in out


def test_no_count_request_when_results_are_under_the_cap(mine):
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["V"], "results": [["1"]]}
    M._gene_proteins({"gene": "G", "max_results": 50})
    assert not any("format=count" in u for u in mine["urls"])


def test_a_failed_count_does_not_sink_the_query(mine):
    mine["count"] = "not-a-number"
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["V"], "results": [["1"]]}
    out = M._gene_expression({"gene": "G", "max_results": 1})
    assert "Expression values" in out and "error" not in out.lower()


def test_max_results_sets_the_page_not_the_fetch(mine):
    M._gene_proteins({"gene": "G", "max_results": 99999})
    assert f"size={M.FETCH_CHUNK}" in mine["urls"][0]


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
        "mine_gene_proteins", "mine_gene_families",
        "mine_gene_ontology", "mine_gene_expression",
        "mine_gene_symbol", "mine_gene_family_members",
        "mine_gene_search", "mine_search",
        "mine_trait_qtls", "mine_trait_gwas", "mine_marker_position"}
    for t in tools:
        assert t.read_only is True
        assert t.parameters.get("additionalProperties") is False
        # every declared required arg must actually be a declared property
        for req in t.parameters["required"]:
            assert req in t.parameters["properties"], (t.name, req)
    by_name = {t.name: t for t in tools}
    assert by_name["mine_gene_symbol"].parameters["required"] == ["symbol"]
    assert by_name["mine_gene_search"].parameters["required"] == ["query"]
    assert by_name["mine_trait_qtls"].parameters["required"] == ["trait"]
    assert by_name["mine_marker_position"].parameters["required"] == ["marker"]
    # family members takes gene OR family, so neither can be schema-required
    fam = by_name["mine_gene_family_members"].parameters
    assert fam["required"] == []
    # 'taxon' used to route the query to a genus mine, which silently lost other genera's
    # genes; the target species is now a filter, never a routing choice.
    # One taxon argument: it filters the members and adds its genus mine.
    assert "taxon" in fam["properties"] and "target_taxon" not in fam["properties"]


def test_tools_are_registered_on_the_mcp_server():
    from legumista_agent.mcp_server import _HANDLERS, build_server
    build_server()
    assert {"mine_gene_proteins", "mine_gene_families",
            "mine_gene_ontology", "mine_gene_expression"} <= set(_HANDLERS)


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
def test_family_members_resolves_gene_to_family_first(mine, monkeypatch):
    """The caller asks for a gene's homologs — needing the family id up front would be
    asking them for the answer."""
    calls = {"n": 0}

    def body(url):
        calls["n"] += 1
        if calls["n"] == 1:      # gene -> family
            return {"wasSuccessful": True,
                    "columnHeaders": ["Gene > Primary Identifier", "Gene Family > Identifier"],
                    "results": [["glyma.Wm82.gnm4.ann1.Glyma.12G040000", "Legume.fam3.10524"]]}
        return {"wasSuccessful": True,             # family -> members
                "columnHeaders": ["Identifier", "Name", "Genus"],
                "results": [["Legume.fam3.10524", "Ae04g33770", "Aeschynomene"]]}

    def fake(url, accept="application/json"):
        mine["urls"].append(url)
        if "format=count" in url:
            return mine["count"]
        return body(url)

    monkeypatch.setattr(M, "_get", fake)
    out = M._gene_family_members({"gene": "Glyma.12G040000"})
    assert "is in gene family Legume.fam3.10524" in out
    assert "Ae04g33770" in out
    xml2, _ = _parse([u for u in mine["urls"] if "format=json" in u][1])
    assert 'value="Legume.fam3.10524"' in xml2


def test_family_members_accepts_a_family_directly(mine):
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["Identifier", "Name"],
                    "results": [["Legume.fam3.10524", "Ae04g33770"]]}
    out = M._gene_family_members({"family": "Legume.fam3.10524"})
    assert "is in gene family" not in out       # no lookup step was needed
    assert "Ae04g33770" in out


def test_family_members_without_gene_or_family_is_rejected(mine):
    assert "provide 'gene'" in M._gene_family_members({})


def test_family_members_reports_an_unknown_gene_as_unknown(mine):
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["X"], "results": []}
    out = M._gene_family_members({"gene": "Glyma.999G999999"})
    # The fake mine answers every query with zero rows, so the presence check finds no
    # gene either: the reply must say the gene is unknown, not that it lacks a family.
    assert "no gene matching 'Glyma.999G999999'" in out
    # Even this early reply keeps the labelled title and the homology caveat.
    assert out.startswith("Gene family members (homologs; not an orthology call):")
    assert out.endswith(M._FAMILY_CAVEAT)


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
    assert 'sortOrder="GWASResult.pValue asc ' in xml


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
    # Queries go to the routed mines. Reading another mine's data model, to decide
    # whether to suggest querying it too, is not a query.
    assert all("/glycinemine/" in u or "/phaseolusmine/" in u
               for u in mine["urls"] if "/service/model" not in u)


def test_known_mines_reads_urls_not_names(catalog):
    """Phaseolus' resource is split across dicts in the catalog (a name-only entry and a
    URL-only entry, as Aeschynomene really is stored); keying on 'name' would lose it."""
    known = M._known_mines()
    assert "phaseolusmine" in known and "glycinemine" in known
    assert "viciamine" not in known
    # legumemine belongs to no genus, so nothing lists it — it must be added by hand or
    # every default-mine gene query would be refused.
    assert M.MINE.lower() in known


def test_mines_absent_from_the_catalog_are_still_reachable(mine, catalog):
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


def test_the_cache_key_separates_mine_and_query_but_not_page(mine):
    """A cache that ignored the mine or the query would answer one question with
    another's rows. The page size is not part of the question: the whole result is
    fetched once, and a different page of it costs nothing."""
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["Gene > Name"],
                    "results": [["G"]]}
    M._gene_families({"gene": "G"})
    M._gene_families({"gene": "G", "mine": "glycinemine"})   # different mine
    M._gene_families({"gene": "OTHER"})                      # different xml
    M._gene_families({"gene": "G", "max_results": 3})        # same question, other page
    assert len(mine["urls"]) == 3 and len(set(mine["urls"])) == 3


def test_a_repeated_family_members_call_reuses_both_of_its_round_trips(mine):
    """mine_gene_family_members is the two-request tool (gene->family, then
    family->members), so an agent circling back to it is where duplicate traffic
    accumulates.

    NOTE: it still cannot reuse mine_gene_families' response — that tool selects
    five views and this step selects one, so the PathQueries differ and the cache key
    (mine, xml, size) rightly separates them."""
    mine["body"] = {"wasSuccessful": True,
                    "columnHeaders": ["Gene > Primary Identifier", "Gene Family > Identifier"],
                    "results": [["glyma.Wm82.gnm4.ann1.Glyma.12G040000", "Legume.fam3.10524"]]}
    M._gene_family_members({"gene": "Glyma.12G040000"})
    before = len(mine["urls"])
    M._gene_family_members({"gene": "Glyma.12G040000"})
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
    assert out.index("glyma.Wm82.gnm4.ann1.Glyma.12G040000") < out.index("[mine")
    assert "10.1126/science.1077937" in out
    # The mines answer too: their curation is broader, and carries every publication.
    # The curated gene is soybean, so glycinemine, which holds symbols, answers as well.
    assert {u.split("/service/")[0].rsplit("/", 1)[-1] for u in mine["urls"]} == {
        "legumemine", "glycinemine"}


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


# --- hardening prototype: empty results, caps, target taxon ---------------------------
def test_empty_result_separates_unknown_gene_from_no_annotations(mine, monkeypatch):
    """An InterMine view is an inner join: a real gene with no GO terms and a typo both
    return zero rows. The presence LOOKUP must tell them apart."""
    def fake(url, accept="application/json"):
        mine["urls"].append(url)
        if "ontologyAnnotations" in urllib.parse.unquote(url):
            return {"wasSuccessful": True, "columnHeaders": [], "results": []}
        return {"wasSuccessful": True, "columnHeaders": ["Gene > Primary Identifier"],
                "results": [["glyma.Wm82.gnm4.ann1.Glyma.12G040000"]]}
    monkeypatch.setattr(M, "_get", fake)
    out = M._gene_ontology({"gene": "Glyma.12G040000"})
    assert "exists in legumemine" in out and "has no ontology annotations" in out
    assert "check the identifier" not in out


def test_a_failed_count_still_fetches_the_whole_result(mine, monkeypatch):
    """Without a total the chunks run one after another until one comes back short."""
    monkeypatch.setattr(M, "FETCH_CHUNK", 10)
    _paged(monkeypatch, mine, 25, count_fails=True)
    out = M._gene_ontology({"gene": "G"})
    assert "25 row(s)" in out and "INCOMPLETE" not in out


def test_a_failed_count_at_the_fetch_limit_says_the_total_is_unknown(mine, monkeypatch):
    monkeypatch.setattr(M, "FETCH_CHUNK", 10)
    monkeypatch.setattr(M, "FETCH_MAX", 20)
    _paged(monkeypatch, mine, 25, count_fails=True)
    out = M._gene_ontology({"gene": "G"})
    assert "stopped at the 20-row fetch limit" in out and "could not be counted" in out


def test_target_taxon_filters_members_and_explains_a_real_zero(mine, catalog, monkeypatch):
    calls = {"n": 0}

    def fake(url, accept="application/json"):
        mine["urls"].append(url)
        if "format=count" in url:
            return "347"
        calls["n"] += 1
        if calls["n"] == 1:
            return {"wasSuccessful": True, "columnHeaders": ["id", "fam"],
                    "results": [["glyma.Wm82.gnm4.ann1.Glyma.12G040000", "Legume.fam3.10524"]]}
        return {"wasSuccessful": True, "columnHeaders": [], "results": []}
    monkeypatch.setattr(M, "_get", fake)
    out = M._gene_family_members({"gene": "Glyma.12G040000", "taxon": "phavu"})
    members_xml = _members_query(mine)
    assert 'path="Gene.organism.genus" op="=" value="Phaseolus"' in members_xml
    assert 'value="vulgaris"' in members_xml
    assert "exists in legumemine with 347 members, none of them from Phaseolus vulgaris" in out
    assert "not orthology" in out


def test_a_common_name_routes_to_the_genus_mine(mine, catalog):
    """'soybean' used to become 'soybeanmine' (first word + 'mine'), which does not
    exist, so every breeding query for soybean by its common name failed."""
    assert M._resolve_mine({"taxon": "soybean"}) == ("glycinemine", None)
    assert M._resolve_mine({"taxon": "glyma"}) == ("glycinemine", None)


def test_family_members_does_not_call_a_failed_count_a_real_zero(mine, catalog,
                                                                 monkeypatch):
    calls = {"n": 0}

    def fake(url, accept="application/json"):
        mine["urls"].append(url)
        if "format=count" in url:
            raise TimeoutError("timed out")
        calls["n"] += 1
        if calls["n"] == 1:
            return {"wasSuccessful": True, "columnHeaders": ["id", "fam"],
                    "results": [["glyma.Wm82.gnm4.ann1.Glyma.12G040000", "Legume.fam3.10524"]]}
        return {"wasSuccessful": True, "columnHeaders": [], "results": []}
    monkeypatch.setattr(M, "_get", fake)
    out = M._gene_family_members({"gene": "Glyma.12G040000", "taxon": "phavu"})
    assert "overall size could not be checked" in out
    assert "real zero for this family" not in out and "has no members" not in out


def test_expression_names_each_studys_unit_and_warns_when_they_mix(mine):
    mine["body"] = {"wasSuccessful": True,
                    "columnHeaders": ["feature", "sample", "desc", "source", "unit", "value"],
                    "results": [
                        ["glyma.Wm82.gnm2.ann1.Glyma.12G040000", "S1", "root", "studyA", "TPM", "88.1"],
                        ["glyma.Wm82.gnm2.ann1.Glyma.12G040000", "S2", "leaf", "studyA", "TPM", "12.0"],
                        ["glyma.Wm82.gnm2.ann1.Glyma.12G040000", "S3", "nodule", "studyB", "FPKM", "40.2"]]}
    out = M._gene_expression({"gene": "Glyma.12G040000"})
    assert "NOTE: these rows mix 2 studies in different units (FPKM, TPM)" in out
    assert "studyA [TPM]: 2 row(s)" in out and "studyB [FPKM]: 1 row(s)" in out
    assert "glyma.Wm82.gnm2.ann1.Glyma.12G040000" in out     # the assembly travels


def test_expression_source_filter_constrains_the_query(mine):
    M._gene_expression({"gene": "Glyma.12G040000", "source": "studyA"})
    xml, _ = _parse(mine["urls"][0])
    assert ('path="ExpressionValue.sample.source.primaryIdentifier" op="=" '
            'value="studyA"') in xml


def test_an_unresolvable_taxon_is_a_name_problem_not_a_missing_mine(mine, catalog):
    """With a catalog loaded, a typo must not become 'soybeen has no InterMine' — that
    reads as a fact about the data. The reply is about the name, and nothing is queried."""
    out = M._trait_qtls({"trait": "seed protein", "taxon": "soybeen"})
    assert "does not match any taxon in the LIS catalog" in out
    assert "has no InterMine" not in out
    assert all("/query/results" not in u for u in mine["urls"])   # probed, never queried


def test_an_ambiguous_name_within_one_genus_routes_to_that_genus(mine, catalog, monkeypatch):
    """'wild peanut' names two Arachis species: the species is ambiguous, the mine is not."""
    ctl = C.controller()
    monkeypatch.setitem(ctl.document["taxa"], "Arachis/cardenasii",
                        {"commonName": "wild peanut", "abbrev": "aracd"})
    monkeypatch.setitem(ctl.document["taxa"], "Arachis/stenosperma",
                        {"commonName": "wild peanut", "abbrev": "araste"})
    monkeypatch.setattr(C, "_TAXON_INDEX", {})
    mine["live"].add("arachismine")
    assert M._resolve_mine({"taxon": "wild peanut"}) == ("arachismine", None)


# --- family members: narrowing and paging -------------------------------------------
def _members_body(n, start=0):
    return {"wasSuccessful": True,
            "columnHeaders": ["Identifier", "Primary Identifier", "Genus", "Species",
                              "Assembly Version"],
            "results": [["Legume.fam3.08725",
                         f"arahy.Tifrunner.gnm2.ann1.Arahy.G{start + i:05d}",
                         "Arachis", "hypogaea", "gnm2"] for i in range(n)]}


def test_family_members_narrows_the_members_by_member_assembly(mine):
    mine["body"] = _members_body(3)
    out = M._gene_family_members({"family": "Legume.fam3.08725",
                                  "member_assembly": "gnm2", "member_annotation": "ann1"})
    xml, _ = _parse(mine["urls"][0])
    assert 'path="Gene.assemblyVersion" op="=" value="gnm2"' in xml
    assert 'path="Gene.annotationVersion" op="=" value="ann1"' in xml
    assert "in assembly gnm2, annotation ann1" in out


def test_family_members_title_names_the_species_and_the_assembly(mine, catalog):
    mine["body"] = _members_body(3)
    out = M._gene_family_members({"family": "Legume.fam3.08725", "member_assembly": "gnm2",
                                  "taxon": "Phaseolus vulgaris"})
    assert "in Phaseolus vulgaris, assembly gnm2" in out


def test_family_members_refuses_assembly_with_a_family(mine):
    """With 'family' given no gene is looked up, so 'assembly' had nothing to narrow and
    was dropped without a word: the whole family read as the gnm2 list."""
    out = M._gene_family_members({"family": "Legume.fam3.08725", "assembly": "gnm2"})
    assert out.startswith("error:") and "'member_assembly'" in out
    assert not mine["urls"]


def test_assembly_picks_the_genes_copy_not_the_members(mine, catalog, monkeypatch):
    """The mine tools' shared rule is 'pass assembly to pick the gene's copy'. Applied to
    the members instead, a soybean gene's bean homologs on 'gnm4' came back as a real
    zero, since no bean genome is called gnm4."""
    calls = {"n": 0}

    def fake(url, accept="application/json"):
        mine["urls"].append(url)
        calls["n"] += 1
        if calls["n"] == 1:
            return {"wasSuccessful": True, "columnHeaders": ["id", "fam"],
                    "results": [["glyma.Wm82.gnm4.ann1.Glyma.12G040000",
                                 "Legume.fam3.10524"]]}
        return _members_body(2)
    monkeypatch.setattr(M, "_get", fake)
    out = M._gene_family_members({"gene": "Glyma.12G040000", "assembly": "gnm4",
                                  "taxon": "phavu"})
    gene_xml, _ = _parse(mine["urls"][0])
    members_xml = _members_query(mine)
    assert 'path="Gene.assemblyVersion" op="=" value="gnm4"' in gene_xml
    assert 'path="Gene.assemblyVersion"' not in members_xml
    assert 'value="Phaseolus"' in members_xml
    assert "in Phaseolus vulgaris —" in out and "real zero" not in out


def test_family_members_pages_the_whole_list_in_a_complete_sort(mine):
    mine["count"] = "348"
    mine["body"] = _members_body(348)
    out = M._gene_family_members({"family": "Legume.fam3.08725", "offset": 300,
                                  "max_results": 100})
    xml, params = _parse(mine["urls"][0])
    # The whole list is fetched once, in an order with no ties, and paged here.
    assert 'sortOrder="Gene.organism.genus asc Gene.primaryIdentifier asc ' in xml
    assert "start" not in params
    assert "showing 301–348 of 348 row(s)" in out
    assert "continue with offset" not in out


def test_a_long_member_list_is_cut_at_a_whole_row_with_the_offset_to_continue(mine):
    """348 rows overran the reply cap and were cut mid-list, with no way to fetch the
    rest: the agent never saw the genes past the cut."""
    mine["count"] = "348"
    mine["body"] = _members_body(348)
    out = M._gene_family_members({"family": "Legume.fam3.08725", "max_results": 500})
    assert "truncated to" not in out
    shown = sum(1 for line in out.splitlines() if "Arahy.G" in line)
    assert 0 < shown < 348
    assert f"showing {shown} of 348 row(s) — continue with offset={shown}" in out
    assert out.rstrip().endswith(M._FAMILY_CAVEAT)


def test_every_mine_tool_pages_a_list_too_long_for_one_reply(mine):
    mine["count"] = "500"
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["V"],
                    "results": [["x" * 200] for _ in range(500)]}
    out = M._gene_expression({"gene": "G", "max_results": 500})
    assert "truncated to" not in out
    shown = sum(1 for line in out.splitlines() if line.startswith("  x"))
    assert 0 < shown < 500 and f"continue with offset={shown}" in out


def test_an_offset_past_the_end_reports_the_total(mine):
    mine["count"] = "348"
    mine["body"] = _members_body(348)
    out = M._gene_family_members({"family": "Legume.fam3.08725", "offset": 400})
    assert "no rows at offset=400; the merged result has 348 row(s)" in out


# --- search by description ------------------------------------------------------------
def _search_body(descriptions):
    return {"wasSuccessful": True,
            "columnHeaders": ["Gene > Primary Identifier", "Gene > Organism > Genus",
                              "Gene > Organism > Species", "Gene > Description"],
            "results": [[f"arahy.Tifrunner.gnm2.ann1.Arahy.G{i:05d}", "Arachis",
                         "hypogaea", d] for i, d in enumerate(descriptions)]}


def test_gene_search_matches_descriptions_within_a_taxon(mine, catalog):
    mine["body"] = _search_body(["chalcone synthase [Glycine max]; IPR011141"])
    out = M._gene_search({"query": "chalcone synthase", "taxon": "phavu"})
    xml, _ = _parse(mine["urls"][0])
    assert ('path="Gene.description" op="CONTAINS" value="chalcone synthase"') in xml
    assert 'path="Gene.organism.genus" op="=" value="Phaseolus"' in xml
    assert 'sortOrder="Gene.primaryIdentifier asc ' in xml
    assert "Gene search by description in Phaseolus vulgaris" in out
    assert "Arahy.G00000" in out and "chalcone synthase [Glycine max]" in out
    # The caveat travels with every hit: a description is not a function.
    assert "not that its function is shown" in out and "which paralog" in out


def test_gene_search_flags_a_term_found_only_inside_a_longer_word(mine):
    """CONTAINS matches substrings: 'CHS' finds 'TrichSKD4', which is not a CHS."""
    mine["body"] = _search_body(["Gp32 n=1 Tax=Roseibium sp. TrichSKD4",
                                 "putative CHS protein"])
    out = M._gene_search({"query": "CHS"})
    assert "1 of the 2 rows contain 'CHS' only inside a longer word" in out
    assert "Arahy.G00000" in out.split("only inside a longer word")[1]


def test_gene_search_finds_families_largest_first(mine):
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["Identifier", "Size",
                                                             "Description"],
                    "results": [["Legume.fam3.08725", 2833, "chalcone synthase [Glycine max]"]]}
    out = M._gene_search({"query": "chalcone synthase", "search": "families"})
    xml, _ = _parse(mine["urls"][0])
    assert 'path="GeneFamily.description" op="CONTAINS"' in xml
    assert 'sortOrder="GeneFamily.size desc GeneFamily.primaryIdentifier asc ' in xml
    assert "Legume.fam3.08725 | 2833" in out and "lis_gene(genes={'family'" in out


def test_gene_search_refuses_what_it_cannot_answer(mine):
    assert "at least 3 characters" in M._gene_search({"query": "CH"})
    assert "'genes' or 'families'" in M._gene_search({"query": "kinase", "search": "x"})
    assert not mine["urls"]


def test_a_family_search_keeps_families_with_members_in_the_taxon(mine, catalog):
    """A taxon filters a family search to families with members there, the same one
    taxon argument every mine tool takes."""
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["Identifier", "Size",
                                                             "Description"],
                    "results": [["Legume.fam3.10524", 347, "protein kinase"]]}
    M._gene_search({"query": "kinase", "search": "families", "taxon": "phavu"})
    xml, _ = _parse(mine["urls"][0])
    assert 'path="GeneFamily.genes.organism.genus" op="=" value="Phaseolus"' in xml


def test_gene_search_explains_an_empty_result(mine):
    mine["body"] = {"wasSuccessful": True, "columnHeaders": [], "results": []}
    out = M._gene_search({"query": "zzqx"})
    assert "no genes in legumemine with a description containing 'zzqx'" in out
    assert "not abbreviations" in out


def test_gene_search_pages_the_merged_result(mine):
    mine["count"] = "100"
    mine["body"] = _search_body([f"receptor kinase {i}" for i in range(100)])
    out = M._gene_search({"query": "receptor kinase", "offset": 50, "max_results": 25})
    assert "showing 51–75 of 100 row(s) — continue with offset=75" in out


# --- a mine's own keyword search ---------------------------------------------------------
def _search_doc(n, total, start=0):
    return {"wasSuccessful": True, "totalHits": total,
            "facets": {"Category": {"Gene": total, "OntologyTerm": 10},
                       "organism.shortName": {"A. hypogaea": total - 3, "A. ipaensis": 3}},
            "results": [{"type": "Gene", "fields": {
                "primaryIdentifier": f"arahy.Tifrunner.gnm1.ann1.G{start + i:05d}",
                "name": f"G{start + i:05d}",
                "description": "chalcone synthase [Glycine max]; IPR016039",
                "organism.name": "Arachis hypogaea", "strain.identifier": "Tifrunner",
                "assemblyVersion": "gnm1", "annotationVersion": "ann1"}}
                for i in range(n)]}


def _search_by_mine(answers):
    """A keyword-search fake: answers[(mine, organism or None)] = (total, identifiers)."""
    urls = []

    def fake(url, accept=""):
        urls.append(url)
        if "/service/model" in url:
            return {"model": {"classes": {"Gene": {}}}}
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        name = url.split("/service/")[0].rsplit("/", 1)[-1]
        org = q.get("facet_organism.shortName", [None])[0]
        total, idents = answers.get((name, org), (0, []))
        start, size = int(q.get("start", ["0"])[0]), int(q.get("size", ["100"])[0])
        return {"wasSuccessful": True, "totalHits": total,
                "facets": {"Category": {"Gene": total},
                           "organism.shortName": {org or "P. vulgaris": total}},
                "results": [{"type": "Gene", "fields": {
                    "primaryIdentifier": ident, "description": "receptor kinase [Glycine max]",
                    "organism.name": "Phaseolus vulgaris"}}
                    for ident in idents[start:start + size]]}
    return fake, urls


def test_mine_search_runs_in_legumemine_and_the_genus_mine_and_merges(
        mine, catalog, monkeypatch):
    ids = [f"phavu.G19833.gnm2.ann1.Phvul.00{i}" for i in range(3)]
    fake, urls = _search_by_mine({("legumemine", "P. vulgaris"): (2, ids[:2]),
                                  ("phaseolusmine", "P. vulgaris"): (2, ids[1:])})
    monkeypatch.setattr(M, "_get", fake)
    out = M._keyword_search({"query": '"receptor kinase"', "taxon": "phavu",
                             "category": "Gene"})
    asked = [u for u in urls if "/search?" in u]
    assert {u.split("/service/")[0].rsplit("/", 1)[-1] for u in asked} == {
        "legumemine", "phaseolusmine"}
    q = urllib.parse.parse_qs(urllib.parse.urlparse(asked[0]).query)
    assert q["facet_Category"] == ["Gene"] and q["facet_organism.shortName"] == ["P. vulgaris"]
    assert ("sources — legumemine 2 row(s); phaseolusmine 2 row(s). Merged: 3 distinct "
            "row(s): 1 in both, 1 legumemine only, 1 phaseolusmine only") in out
    assert "by category — legumemine: Gene 2; phaseolusmine: Gene 2" in out
    assert "not this gene's species" in out


def test_mine_search_expands_a_genus_into_its_species_in_legumemine(
        mine, catalog, monkeypatch):
    """The search filters only by organism ('G. max'); matching the initial alone would
    take other genera's species, so the genus's own species come from the catalog."""
    fake, urls = _search_by_mine({("legumemine", "G. max"): (1, ["glyma.X"]),
                                  ("glycinemine", None): (1, ["glyma.X"])})
    monkeypatch.setattr(M, "_get", fake)
    out = M._keyword_search({"query": "NARK", "taxon": "Glycine"})
    orgs = {(u.split("/service/")[0].rsplit("/", 1)[-1],
             urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
             .get("facet_organism.shortName", [None])[0]) for u in urls if "/search?" in u}
    assert orgs == {("legumemine", "G. max"), ("glycinemine", None)}
    assert "1 in both" in out


def test_mine_search_failure_is_an_error_not_an_empty_result(mine, monkeypatch):
    monkeypatch.setattr(M, "_get", lambda url, accept="": {"wasSuccessful": False,
                                                          "error": "bad query"})
    assert M._keyword_search({"query": "x"}).startswith("error: legumemine rejected")

    def boom(url, accept=""):
        raise TimeoutError("timed out")
    monkeypatch.setattr(M, "_get", boom)
    assert M._keyword_search({"query": "y"}).startswith("error: legumemine search failed")


def test_mine_search_offset_past_the_end_reports_the_total(mine, monkeypatch):
    fake, _urls = _search_by_mine({("legumemine", None): (280, [f"x{i}" for i in range(280)])})
    monkeypatch.setattr(M, "_get", fake)
    out = M._keyword_search({"query": "z", "offset": 300})
    assert "no rows at offset=300; the merged result has 280 row(s)" in out


# --- every mine that covers the subject -------------------------------------------------
def _by_mine(rows_by_mine, cols=("Gene > Name", "Gene > Assembly Version")):
    """A fake _get answering each mine with its own rows."""
    def fake(url, accept="application/json"):
        name = url.split("/service/")[0].rsplit("/", 1)[-1]
        if "/service/model" in url:
            return {"model": {"classes": {"Gene": {}, "GeneFunction": {}}}}
        if "format=count" in url:
            return str(len(rows_by_mine.get(name, [])))
        if name in rows_by_mine.get("_fail", ()):
            raise TimeoutError("timed out")
        return {"wasSuccessful": True, "columnHeaders": list(cols),
                "results": rows_by_mine.get(name, [])}
    return fake


def test_every_mine_tool_takes_the_same_taxon_and_mine():
    for tool in M.mine_tools():
        props = tool.parameters["properties"]
        assert "taxon" in props and "mine" in props, tool.name
        assert "target_taxon" not in props, tool.name


def test_a_gene_tool_queries_legumemine_and_the_genus_mine_and_marks_each_row(
        mine, catalog, monkeypatch):
    """Agents asked about one species queried legumemine alone; the tools now ask
    every mine that covers the subject themselves."""
    asked = []
    fake = _by_mine({"legumemine": [["Glyma.12G040000", "gnm4"], ["Glyma.12G040000", "gnm2"]],
                     "glycinemine": [["Glyma.12G040000", "gnm4"], ["Glyma.12G040000", "gnm6"]]})
    monkeypatch.setattr(M, "_get", lambda url, accept="application/json":
                        asked.append(url) or fake(url, accept))
    out = M._gene_proteins({"gene": "Glyma.12G040000"})
    assert {u.split("/service/")[0].rsplit("/", 1)[-1] for u in asked
            if "/query/results" in u} == {"legumemine", "glycinemine"}
    assert "[mines: legumemine, glycinemine]" in out
    assert ("sources — legumemine 2 row(s); glycinemine 2 row(s). Merged: 3 distinct "
            "row(s): 1 in both, 1 legumemine only, 1 glycinemine only") in out
    assert "  Glyma.12G040000 | gnm4 | both" in out
    assert "  Glyma.12G040000 | gnm2 | legumemine" in out
    assert "  Glyma.12G040000 | gnm6 | glycinemine" in out


def test_rows_spelled_differently_in_the_two_mines_are_one_row(mine, catalog, monkeypatch):
    """ArachisMine drops the name's species token; once restored, the rows match."""
    doc = json.loads(open(C.CATALOG_PATH).read())
    doc["taxa"]["Arachis/hypogaea"] = {"abbrev": "arahy", "resources": [
        {"URL": "https://mines.legumeinfo.org/arachismine/begin.do"}]}
    open(C.CATALOG_PATH, "w").write(json.dumps(doc))
    C.reset()
    M.reset_cache()
    ids = ("arahy.Tifrunner.gnm2.ann1.Arahy.GHMM2H", "arahy.Tifrunner.gnm2.ann1.GHMM2H")
    monkeypatch.setattr(M, "_get", _by_mine({"legumemine": [[ids[0], "gnm2"]],
                                             "arachismine": [[ids[1], "gnm2"]]}))
    out = M._gene_proteins({"gene": ids[0]})
    assert "1 distinct row(s): 1 in both" in out and f"  {ids[0]} | gnm2 | both" in out


def test_a_failed_mine_leaves_partial_results_from_the_other(mine, catalog, monkeypatch):
    monkeypatch.setattr(M, "_get", _by_mine({"legumemine": [["Glyma.12G040000", "gnm4"]],
                                             "_fail": ("glycinemine",)}))
    out = M._gene_proteins({"gene": "Glyma.12G040000"})
    assert "PARTIAL RESULTS — glycinemine FAILED" in out
    assert "The rows below come only from legumemine" in out
    assert "Glyma.12G040000 | gnm4" in out


def test_an_explicit_mine_is_queried_alone(mine, catalog):
    M._gene_proteins({"gene": "Glyma.12G040000", "mine": "legumemine"})
    assert all("/legumemine/" in u for u in mine["urls"])


def test_taxon_filters_rows_and_adds_its_genus_mine(mine, catalog):
    M._gene_proteins({"gene": "Phvul.001G000100", "taxon": "phavu"})
    queried = {u.split("/service/")[0].rsplit("/", 1)[-1] for u in mine["urls"]}
    assert queried == {"legumemine", "phaseolusmine"}
    xml, _ = _parse(mine["urls"][0])
    assert 'path="Gene.organism.genus" op="=" value="Phaseolus"' in xml
    assert 'path="Gene.organism.species" op="=" value="vulgaris"' in xml


def test_a_tool_whose_data_is_only_in_genus_mines_never_asks_legumemine(mine, catalog):
    """legumemine's model has no QTL class: asking it would fail, not answer."""
    mine["body"] = {"wasSuccessful": True, "columnHeaders": ["QTL > Name"],
                    "results": [["Seed protein 1-1"]]}
    out = M._trait_qtls({"trait": "seed protein", "taxon": "soybean"})
    assert "[mine: glycinemine]" in out
    assert not any("/legumemine/" in u for u in mine["urls"])


def test_a_gene_id_spelled_differently_is_retried(mine, monkeypatch):
    """ArachisMine spells legumemine's ...ann1.Arahy.GHMM2H as ...ann1.GHMM2H."""
    asked = []

    def fake(url, accept="application/json"):
        xml, _ = _parse(url)
        asked.append(xml)
        hit = 'value="arahy.Tifrunner.gnm2.ann1.GHMM2H"' in xml
        return {"wasSuccessful": True, "columnHeaders": ["Gene > Name", "Assembly"],
                "results": [["GHMM2H", "gnm2"]] if hit else []}
    monkeypatch.setattr(M, "_get", fake)
    out = M._gene_proteins({"gene": "arahy.Tifrunner.gnm2.ann1.Arahy.GHMM2H",
                            "mine": "arachismine"})
    assert ("arachismine spells arahy.Tifrunner.gnm2.ann1.Arahy.GHMM2H as "
            "arahy.Tifrunner.gnm2.ann1.GHMM2H; its rows are for that ID.") in out
    assert "GHMM2H | gnm2" in out
    assert M._other_spellings("arahy.Tifrunner.gnm2.ann1.GHMM2H") == [
        "arahy.Tifrunner.gnm2.ann1.Arahy.GHMM2H"]
    assert M._other_spellings("GHMM2H") == []


def test_a_slow_fetch_replies_still_fetching_and_the_next_call_collects_it(
        mine, monkeypatch):
    """A mine computing a large new result can outlast a client's patience. The reply
    says so and the fetch carries on; the same call later gets the result."""
    import threading
    release = threading.Event()
    calls = []

    def slow(url, accept="application/json"):
        if "/service/model" in url:
            return {"model": {"classes": {"Gene": {}}}}
        calls.append(url)
        release.wait(5)
        return {"wasSuccessful": True, "columnHeaders": ["Gene > Name"],
                "results": [["G1"], ["G2"]]}
    monkeypatch.setattr(M, "_get", slow)
    monkeypatch.setattr(M, "TIME_BUDGET", 0.05)
    first = M._gene_proteins({"gene": "G"})
    assert "STILL FETCHING" in first and "make the same call again" in first
    release.set()
    monkeypatch.setattr(M, "TIME_BUDGET", 5)
    second = M._gene_proteins({"gene": "G"})
    assert "G1" in second and "G2" in second
    assert len(calls) == 1, "the second call must collect the first fetch, not refetch"


def test_keyword_hits_are_one_object_whatever_each_mine_shows_beside_them():
    """legumemine names a Bailey II gene where arachismine leaves the name blank; the
    two hits are the same gene."""
    a, b = M._Fetched("legumemine"), M._Fetched("arachismine")
    a.rows = [["Gene", "arahy.BaileyII.gnm1.ann1.G1", "G1", "Arachis hypogaea", "", "d"]]
    b.rows = [["Gene", "arahy.BaileyII.gnm1.ann1.G1", "", "Arachis hypogaea", "", "d"]]
    items = M._merge([a, b], set(), ["type", "identifier"], None, key_cols=(0, 1))
    assert len(items) == 1 and items[0][1] == {"legumemine", "arachismine"}
    # A PathQuery row is the answer itself: a difference in any column is a finding.
    assert len(M._merge([a, b], set(), ["type", "identifier"], None)) == 2


def test_a_mine_without_the_family_is_left_out_and_the_reply_says_why(
        mine, catalog, monkeypatch):
    """phaseolusmine holds legfed_v1_0 families only; asking it for a legume.fam3
    family's members would report a certain zero as if it were a finding."""
    def fake(url, accept="application/json"):
        name = url.split("/service/")[0].rsplit("/", 1)[-1]
        if "/service/model" in url:
            return {"model": {"classes": {"Gene": {}}}}
        mine["urls"].append(url)
        if "format=count" in url:
            return "0" if name == "phaseolusmine" else "9"
        return _members_body(2)
    monkeypatch.setattr(M, "_get", fake)
    out = M._gene_family_members({"family": "Legume.fam3.10524", "taxon": "phavu"})
    assert "phaseolusmine was not queried: it has no gene family Legume.fam3.10524." in out
    assert not any("/phaseolusmine/" in u and "format=json" in u for u in mine["urls"])
