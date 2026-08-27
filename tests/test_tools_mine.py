"""LIS InterMine tool tests — PathQuery construction, InterMine's in-body failure
reporting, assembly disambiguation, and result capping.

No network: `_get` is stubbed with a fake mine that records the URLs it was asked for, so
these pin OUR query construction and result handling rather than the mine's uptime."""
import json
import urllib.parse

import pytest

from legumista_agent import tools_mine as M


def _parse(url):
    """Pull the PathQuery XML and params back out of a request URL."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return q.get("query", [""])[0], q


@pytest.fixture
def mine(monkeypatch):
    """A fake mine. `state['body']` is what the next JSON query returns; `state['urls']`
    records every request so tests can assert on the query that was built."""
    state = {"urls": [], "count": "7", "body": None}

    def default_body():
        return {"wasSuccessful": True,
                "columnHeaders": ["Gene > Name", "Gene > Assembly Version",
                                  "Gene > Proteins > Identifier"],
                "results": [["Glyma.12G040000", "gnm4", "glyma.Wm82.gnm4.ann1.X.1"]]}

    def fake_get(url, accept="application/json"):
        state["urls"].append(url)
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
