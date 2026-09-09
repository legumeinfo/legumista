"""LIS tool tests — discovery, access-mode reporting, and gene lookup.

These now run against a fixture **catalog** rather than a fake datastore website:
`lis_find`/`lis_files` contact no network at all, and `lis_gene` reads exactly two
data files (gene models and, when needed, the synonym map) whose URLs come from the
catalog. Those two reads are stubbed here, so the whole file is network-free.
"""
import json
import sys

import pytest

from legumista_agent import tools_catalog as C
from legumista_agent import tools_lis as L

# Honour the same source-checkout override the module uses at runtime.
if C.DSCENSOR_PATH and C.DSCENSOR_PATH not in sys.path:
    sys.path.insert(0, C.DSCENSOR_PATH)
pytest.importorskip(
    "dscensor.catalog",
    reason=(
        "dscensor is an optional dependency: install it, or set "
        "LEGUMISTA_DSCENSOR_PATH to a source checkout"
    ),
)

DS = "https://data.legumeinfo.org"
ANN = "Glycine/max/annotations/Wm82.gnm4.ann1.T8TQ"
PREFIX = "glyma.Wm82.gnm4.ann1.T8TQ"

# Two genes whose IDs are prefixes of one another, so exactness is actually tested.
BED_ROWS = [
    ("glyma.Wm82.gnm4.Gm12", 2875800, 2879231,
     "glyma.Wm82.gnm4.ann1.Glyma.12G040000.1", "-",
     "glyma.Wm82.gnm4.ann1.Glyma.12G040000"),
    ("glyma.Wm82.gnm4.Gm12", 5000, 6000,
     "glyma.Wm82.gnm4.ann1.Glyma.12G0400001.1", "+",
     "glyma.Wm82.gnm4.ann1.Glyma.12G0400001"),
]
BED_TEXT = "".join(
    f"{c}\t{s}\t{e}\t{m}\t0\t{st}\t{g}\n" for c, s, e, m, st, g in BED_ROWS
)
GFF3_TEXT = "\n".join([
    "##gff-version 3",
    "#refname: glyma.Wm82.gnm2",
    "\t".join(["glyma.Wm82.gnm2.Gm06", "DAGchainer", "syntenic_region", "64390",
               "1882809", "4545.0", "-", ".",
               "Name=phavu.G19833.gnm2.Chr02;"
               "matches=phavu.G19833.gnm2.Chr02:49767515..51299643;median_Ks=0.3641"]),
    "\t".join(["glyma.Wm82.gnm2.Gm06", "DAGchainer", "syntenic_region", "9000000",
               "9500000", "200.0", "+", ".",
               "Name=phavu.G19833.gnm2.Chr03;"
               "matches=phavu.G19833.gnm2.Chr03:1000..2000;median_Ks=0.41"]),
])

SYNONYM_TEXT = "\n".join([
    "glyma.Wm82.gnm4.ann1.Glyma.12G040000.1\tGlyma12g04000",
    "glyma.Wm82.gnm4.ann1.Glyma.99G999999.1\tGlyma99g99999",  # points outside the BED
])

CATALOG = {
    "schema": 1,
    "built_at": "2026-09-08T12:00:00Z",
    "source_commit": "abc123def4567890",
    "datastore_url": DS,
    "stats": {"collections": 5},
    "gene_symbols": {
        "glyma": {
            "gmnark": {
                "gene": "glyma.Wm82.gnm4.ann1.Glyma.12G040000",
                "doi": "10.1126/science.1077937",
                "synopsis": "Long-distance control of nodule proliferation.",
            }
        }
    },
    "pairwise": [
        {"a": "glyma.Wm82.gnm2", "b": "phavu.G19833.gnm2", "kind": "synteny",
         "format": "gff3.gz", "collection": "Glycine/max/synteny/Wm82.gnm2.syn.HXNY",
         "file": "glyma.Wm82.gnm2.x.phavu.G19833.gnm2.HXNY.gff3.gz",
         "url": f"{DS}/Glycine/max/synteny/Wm82.gnm2.syn.HXNY/"
                "glyma.Wm82.gnm2.x.phavu.G19833.gnm2.HXNY.gff3.gz"},
        {"a": "glyma.Wm82.gnm2", "b": "glyma.Wm82.gnm2", "self": True,
         "epoch": "old_duplication", "kind": "synteny", "format": "gff3.gz",
         "collection": "Glycine/max/synteny/Wm82.gnm2.syn.HXNY",
         "file": "glyma.Wm82.gnm2.x.glyma.Wm82.gnm2.old_duplication.HXNY.gff3.gz",
         "url": f"{DS}/x/self.gff3.gz"},
        # medtr owns no synteny collection: it appears only as the B side.
        {"a": "glyma.Wm82.gnm2", "b": "medtr.A17_HM341.gnm4", "kind": "synteny",
         "format": "gff3.gz", "collection": "Glycine/max/synteny/Wm82.gnm2.syn.HXNY",
         "file": "glyma.Wm82.gnm2.x.medtr.A17_HM341.gnm4.HXNY.gff3.gz",
         "url": f"{DS}/x/medtr.gff3.gz"},
    ],
    "collections": [
        {
            "path": "Glycine/max/genomes/Wm82.gnm4.4PTR",
            "id": "Wm82.gnm4.4PTR", "type": "genomes",
            "genus": "Glycine", "species": "max",
            "base_url": f"{DS}/Glycine/max/genomes/Wm82.gnm4.4PTR",
            "index_status": "known", "taxid": 3847,
            "scientific_name_abbrev": "glyma",
            "chromosome_prefix": "Gm",
            "synopsis": "Williams 82 assembly v4",
            "publication_doi": "10.1111/tpj.14500",
            "genotype": ["Williams 82"],
            "files": [{"n": "glyma.Wm82.gnm4.4PTR.genome_main.fna.gz", "i": [".fai"]}],
        },
        {
            "path": ANN, "id": "Wm82.gnm4.ann1.T8TQ", "type": "annotations",
            "genus": "Glycine", "species": "max",
            "base_url": f"{DS}/{ANN}",
            "index_status": "known", "taxid": 3847,
            "scientific_name_abbrev": "glyma",
            "chromosome_prefix": "Gm",
            "synopsis": "Williams 82 annotation",
            "publication_doi": "10.1111/tpj.14500",
            "license": "Open",
            "derived_from": ["Wm82.gnm4.4PTR"],
            "files": [
                {"n": f"{PREFIX}.protein_primary.faa.gz", "i": [".fai"],
                 "description": "Protein sequences - primary only"},
                {"n": f"{PREFIX}.cds_primary.fna.gz", "i": [".fai"]},
                {"n": f"{PREFIX}.gene_models_main.gff3.gz", "i": [".tbi"]},
                {"n": f"{PREFIX}.gene_models_main.bed.gz", "i": [".tbi"]},
                {"n": f"{PREFIX}.info_synonyms.txt.gz"},
                {"n": f"{PREFIX}.legume.fam3.VLMQ.gfa.tsv.gz",
                 "description": "gene family assignments"},
            ],
        },
        {
            "path": "Glycine/max/annotations/Wm82.gnm2.ann1.RVB6",
            "id": "Wm82.gnm2.ann1.RVB6", "type": "annotations",
            "genus": "Glycine", "species": "max",
            "base_url": f"{DS}/Glycine/max/annotations/Wm82.gnm2.ann1.RVB6",
            "index_status": "known", "scientific_name_abbrev": "glyma",
            "files": [],
        },
        {
            "path": "Glycine/max/diversity/Wm82.gnm2.div.X_2020",
            "id": "Wm82.gnm2.div.X_2020", "type": "diversity",
            "genus": "Glycine", "species": "max",
            "base_url": f"{DS}/Glycine/max/diversity/Wm82.gnm2.div.X_2020",
            "index_status": "known",
            "files": [{"n": "glyma.Wm82.gnm2.div.X_2020.SNPdata.vcf.gz", "i": [".tbi"]}],
        },
        {
            # The real shape of nearly every qtl/gwas collection: no CHECKSUM.
            "path": "Glycine/max/qtl/Demo.qtl.X_1990",
            "id": "Demo.qtl.X_1990", "type": "qtl",
            "genus": "Glycine", "species": "max",
            "base_url": f"{DS}/Glycine/max/qtl/Demo.qtl.X_1990",
            "index_status": "inferred",
            "publication_doi": "10.1007/bf00226154",
            "files": [
                {"n": "glyma.Demo.qtl.X_1990.qtl.tsv.gz", "src": "predicted"},
                {"n": "glyma.Demo.qtl.X_1990.obo.tsv.gz", "src": "predicted"},
            ],
        },
        {
            "path": "Phaseolus/vulgaris/genomes/G19833.gnm2.fC0g",
            "id": "G19833.gnm2.fC0g", "type": "genomes",
            "genus": "Phaseolus", "species": "vulgaris",
            "base_url": f"{DS}/Phaseolus/vulgaris/genomes/G19833.gnm2.fC0g",
            "index_status": "known", "scientific_name_abbrev": "phavu",
            "files": [],
        },
    ],
}


@pytest.fixture(autouse=True)
def catalog(tmp_path, monkeypatch):
    """Load the fixture catalog and stub the two remaining data reads."""
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(CATALOG), encoding="utf-8")
    monkeypatch.setattr(C, "CATALOG_PATH", str(path))
    monkeypatch.setattr(C, "_CANDIDATES", ())   # the fixture must win
    C.reset()

    fetched = []

    def fake_fetch(url, limit=None):
        fetched.append(url)
        if url.endswith(".gene_models_main.bed.gz"):
            return BED_TEXT, None
        if url.endswith((".info_synonyms.txt.gz", ".synonym.txt.gz")):
            return SYNONYM_TEXT, None
        if url.endswith(".gff3.gz"):
            return GFF3_TEXT, None
        return None, "unexpected fetch in a test"

    monkeypatch.setattr(L, "_fetch_gz_text", fake_fetch)
    yield fetched
    C.reset()


def _find(**kw):
    return L._find(kw)


# --- lis_find: no network at all ------------------------------------------------------
def test_find_lists_genera_with_a_count(catalog):
    out = _find()
    assert "2 genera" in out
    assert "Glycine" in out and "Phaseolus" in out
    assert catalog == []  # nothing fetched


def test_find_reports_taxid_and_abbrev(catalog):
    out = _find(taxon="Glycine max")
    assert "taxid:3847" in out and "abbrev:glyma" in out
    assert "annotations" in out and "qtl" in out


def test_find_splits_a_taxon_string(catalog):
    assert _find(taxon="Glycine max") == _find(genus="Glycine", species="max")


def test_find_lists_collections_with_publication_doi(catalog):
    out = _find(taxon="Glycine max", type="annotations")
    assert "Wm82.gnm4.ann1.T8TQ" in out
    assert "publication_doi: 10.1111/tpj.14500" in out
    assert "openalex_by_doi" in out
    assert f"path: {ANN}" in out


def test_find_query_filters_collections(catalog):
    assert "RVB6" not in _find(taxon="Glycine max", type="annotations", query="T8TQ")
    assert "no collection" in _find(taxon="Glycine max", type="annotations", query="zzz")


def test_find_unknown_type_lists_the_real_ones(catalog):
    out = _find(taxon="Glycine max", type="nonesuch")
    assert "no collections" in out and "annotations" in out


def test_find_stamps_every_answer(catalog):
    """A catalog is a snapshot; staleness must be visible at the point of use."""
    for out in (_find(), _find(taxon="Glycine max"),
                _find(taxon="Glycine max", type="annotations")):
        assert "catalog built 2026-09-08T12:00:00Z" in out


# --- lis_files ------------------------------------------------------------------------
def test_files_separates_addressable_from_unindexed(catalog):
    out = L._files({"collection": ANN})
    accessible, unindexed = out.split("NOT INDEXED")
    assert "protein_primary.faa.gz" in accessible and "fasta_fetch" in accessible
    assert "gene_models_main.gff3.gz" in accessible and "tabix_query" in accessible
    assert "legume.fam3.VLMQ.gfa.tsv.gz" in unindexed
    assert catalog == []  # answered entirely from memory


def test_files_reports_doi_licence_and_descriptions(catalog):
    out = L._files({"collection": ANN})
    assert "publication_doi: 10.1111/tpj.14500" in out
    assert "license: Open" in out
    assert "Protein sequences - primary only" in out


def test_files_routes_an_indexed_vcf_to_bcftools(catalog):
    """A .tbi on a VCF means bcftools, not tabix_query — same index, different reader."""
    out = L._files({"collection": "Glycine/max/diversity/Wm82.gnm2.div.X_2020"})
    assert "bcftools" in out and "SNPdata.vcf.gz" in out


def test_files_states_how_the_file_list_was_obtained(catalog):
    """A list built from the filename convention is a good guess, not a fact. Saying
    which is the difference between an agent trusting a path and verifying it."""
    out = L._files({"collection": "Glycine/max/qtl/Demo.qtl.X_1990"})
    assert "PREDICTED from the datastore's documented filename convention" in out
    assert "may not exist" in out
    assert "publication_doi: 10.1007/bf00226154" in out  # metadata survives

    checksummed = L._files({"collection": ANN})
    assert "documented filename convention" not in checksummed  # authoritative


def test_files_reports_a_verified_list_as_confirmed(catalog, monkeypatch):
    """With --verify, autocontent has already confirmed the files exist; the tool must
    pass that distinction through rather than flattening it back to a guess."""
    record = [c for c in CATALOG["collections"] if c["type"] == "qtl"][0]
    verified = dict(record, index_status="verified",
                    files=[{"n": "glyma.Demo.qtl.X_1990.qtl.tsv.gz", "src": "verified"}])
    monkeypatch.setattr(
        C.controller(), "get_collection", lambda path: verified
    )
    out = L._files({"collection": "Glycine/max/qtl/Demo.qtl.X_1990"})
    assert "CONFIRMED to exist" in out
    assert "may not exist" not in out


def test_files_with_nothing_resolvable_says_so(catalog, monkeypatch):
    """Neither a CHECKSUM nor a documented convention: report the gap as itself."""
    record = [c for c in CATALOG["collections"] if c["type"] == "qtl"][0]
    monkeypatch.setattr(
        C.controller(), "get_collection",
        lambda path: dict(record, index_status="unknown", files=[]),
    )
    out = L._files({"collection": "Glycine/max/qtl/Demo.qtl.X_1990"})
    assert "FILE LIST UNAVAILABLE" in out
    assert "not an empty collection" in out


def test_files_accepts_path_url_and_bare_id(catalog):
    by_path = L._files({"collection": ANN})
    by_url = L._files({"collection": f"{DS}/{ANN}"})
    by_id = L._files({"collection": "Wm82.gnm4.ann1.T8TQ"})
    assert by_path == by_url == by_id


def test_files_rejects_a_non_collection_path(catalog):
    assert "not a collection path" in L._files({"collection": "Glycine/max"})
    assert "missing 'collection'" in L._files({"collection": ""})


def test_files_unknown_collection_is_an_honest_miss(catalog):
    out = L._files({"collection": "Glycine/max/genomes/Not.In.Catalog"})
    assert "no collection at" in out and "lis_find" in out


# --- lis_gene -------------------------------------------------------------------------
def test_gene_resolves_locus_and_sequence_handles(catalog):
    out = L._gene({"gene": "Glyma.12G040000", "collection": ANN})
    assert "glyma.Wm82.gnm4.Gm12:2875801-2879231" in out  # BED start is 0-based
    assert "fasta_fetch(" in out and "protein_primary.faa.gz" in out
    assert "region='glyma.Wm82.gnm4.ann1.Glyma.12G040000.1'" in out
    assert "tabix_query(" in out
    assert "publication_doi: 10.1111/tpj.14500" in out


def test_gene_reads_only_the_gene_models_file(catalog):
    """Everything else — the collection, its URLs, the symbol map — is in memory."""
    L._gene({"gene": "Glyma.12G040000", "collection": ANN})
    assert catalog == [f"{DS}/{ANN}/{PREFIX}.gene_models_main.bed.gz"]


def test_gene_matching_is_exact_not_substring(catalog):
    """Glyma.12G040000 is a prefix of Glyma.12G0400001; a substring match would
    return both and silently report the wrong locus."""
    out = L._gene({"gene": "Glyma.12G040000", "collection": ANN})
    assert "1 model(s)" in out
    assert "5,001-6,000" not in out


def test_gene_accepts_prefixed_and_mrna_forms(catalog):
    for gid in ("glyma.Wm82.gnm4.ann1.Glyma.12G040000",
                "glyma.Wm82.gnm4.ann1.Glyma.12G040000.1"):
        assert "2875801-2879231" in L._gene({"gene": gid, "collection": ANN})


def test_gene_derives_the_collection_from_a_qualified_id(catalog):
    """The qualified ID carries the annotation stem but not the 4-char key; the key
    now comes from the catalog rather than a directory listing."""
    out = L._gene({"gene": "glyma.Wm82.gnm4.ann1.Glyma.12G040000"})
    assert "Wm82.gnm4.ann1.T8TQ" in out and "2875801-2879231" in out


def test_gene_resolves_a_curated_symbol_from_the_catalog(catalog):
    """No traits.yml fetch: the symbol map now travels in the catalog itself."""
    out = L._gene({"gene": "GmNARK", "collection": ANN})
    assert "curated symbol 'GmNARK'" in out
    assert "carried in the catalog" in out
    assert "10.1126/science.1077937" in out
    assert "2875801-2879231" in out
    assert catalog == [f"{DS}/{ANN}/{PREFIX}.gene_models_main.bed.gz"]


def test_symbol_match_is_case_insensitive(catalog):
    assert "2875801-2879231" in L._gene({"gene": "gmnark", "collection": ANN})


def test_gene_resolves_a_superseded_id(catalog):
    out = L._gene({"gene": "Glyma12g04000", "collection": ANN})
    assert "superseded ID 'Glyma12g04000'" in out
    assert "2875801-2879231" in out


def test_exact_id_is_never_shadowed_by_an_alias(catalog):
    """An exact hit must win outright — no alias lookup, no synonym fetch."""
    out = L._gene({"gene": "Glyma.12G040000", "collection": ANN})
    assert "resolved from:" not in out
    assert not any("synonym" in url for url in catalog)


def test_alias_pointing_outside_the_collection_is_not_a_false_hit(catalog):
    """The synonym file maps Glyma99g99999 to a gene absent from this BED;
    resolving the alias must not manufacture a locus."""
    assert "no match" in L._gene({"gene": "Glyma99g99999", "collection": ANN})


def test_miss_reports_which_sources_were_consulted(catalog):
    out = L._gene({"gene": "NotAGene9", "collection": ANN})
    assert "curated symbols" in out and "synonym file" in out


def test_miss_explains_that_cross_assembly_names_are_not_synonyms(catalog):
    out = L._gene({"gene": "MtrunA17_Chr1g0147651", "collection": ANN})
    assert "different assembly" in out and "lis_find" in out


def test_gene_requires_a_gene(catalog):
    assert "missing 'gene'" in L._gene({"gene": ""})


def test_gene_without_collection_or_qualified_id_asks_for_one(catalog):
    assert "pass 'collection'" in L._gene({"gene": "Glyma.12G040000"})


def test_gene_needs_a_gene_models_file(catalog):
    """A collection with no BED cannot answer, and says so rather than erroring out."""
    out = L._gene({"gene": "Glyma.12G040000",
                   "collection": "Glycine/max/annotations/Wm82.gnm2.ann1.RVB6"})
    assert "no gene_models_main.bed.gz" in out


# --- registry -------------------------------------------------------------------------
def test_lis_tools_are_read_only_and_well_formed():
    tools = L.lis_tools()
    assert {t.name for t in tools} == {
        "lis_find", "lis_files", "lis_gene", "lis_synteny"
    }
    for tool in tools:
        assert tool.read_only is True
        assert tool.parameters["type"] == "object"
        assert tool.parameters.get("additionalProperties") is False
        assert tool.description.strip()


def test_lis_tools_are_registered_on_the_mcp_server():
    from legumista_agent.mcp_server import _HANDLERS, build_server
    build_server()
    assert {"lis_find", "lis_files", "lis_gene", "lis_synteny"} <= set(_HANDLERS)


def test_tools_report_clearly_when_no_catalog_is_configured(monkeypatch):
    monkeypatch.setattr(C, "CATALOG_PATH", "")
    monkeypatch.setattr(C, "_CANDIDATES", ())
    C.reset()
    try:
        for out in (L._find({}), L._files({"collection": ANN}),
                    L._gene({"gene": "Glyma.12G040000", "collection": ANN})):
            assert "no LIS catalog is loaded" in out
            assert "populate-catalog" in out
    finally:
        C.reset()


# --- lis_synteny -----------------------------------------------------------------------
def test_synteny_lists_partners_without_touching_the_network(catalog):
    """Orientation comes from the catalog's pairwise graph, derived at build time."""
    out = L._synteny({"genome": "glyma.Wm82.gnm2"})
    assert "3 partner(s)" in out
    assert "phavu.G19833.gnm2" in out and "medtr.A17_HM341.gnm4" in out
    assert catalog == []


def test_synteny_finds_pairs_stored_under_the_other_genome(catalog):
    """The directionality case. A pairwise file lives under whichever genome is the
    reference, so a genome that owns no collection appears only as the B side. Reading
    just its own collection would report no synteny at all."""
    out = L._synteny({"genome": "medtr.A17_HM341.gnm4"})
    assert "1 partner(s)" in out
    assert "glyma.Wm82.gnm2" in out
    assert "stored under the partner's collection" in out


def test_synteny_surfaces_self_comparisons_with_their_epoch(catalog):
    """Which whole-genome duplication a block came from is the informative part; a
    generic 'paralog mode' would discard it."""
    out = L._synteny({"genome": "glyma.Wm82.gnm2"})
    assert "self-comparison, old_duplication" in out


def test_synteny_parses_blocks_with_score_and_ks(catalog):
    out = L._synteny({"genome": "glyma.Wm82.gnm2", "partner": "phavu.G19833.gnm2",
                      "region": "glyma.Wm82.gnm2.Gm06:64390-1882809"})
    assert "glyma.Wm82.gnm2.Gm06:64390-1882809" in out
    assert "phavu.G19833.gnm2.Chr02:49767515-51299643" in out
    assert "score=4545.0" in out and "median_Ks=0.3641" in out


def test_synteny_region_filter_excludes_non_overlapping_blocks(catalog):
    out = L._synteny({"genome": "glyma.Wm82.gnm2", "partner": "phavu.G19833.gnm2",
                      "region": "glyma.Wm82.gnm2.Gm06:64390-1882809"})
    assert "1 of 1 block(s)" in out
    assert "Chr03" not in out          # the 9Mb block is outside the region


def test_synteny_empty_region_is_a_result_not_an_error(catalog):
    """Three outcomes must stay distinct: wrong assembly, no overlap, fetch failure."""
    out = L._synteny({"genome": "glyma.Wm82.gnm2", "partner": "phavu.G19833.gnm2",
                      "region": "glyma.Wm82.gnm2.Gm06:1-100"})
    assert "no blocks overlap" in out
    assert "not an error" in out


def test_synteny_routes_when_the_assembly_has_none(catalog):
    """Synteny is published for one, usually old, assembly per species. The common
    request arrives on a newer one; naming the alternative turns a dead end into a
    next step."""
    out = L._synteny({"gene": "glyma.Wm82.gnm4.ann1.Glyma.12G040000"})
    assert "no synteny or alignment data for 'glyma.Wm82.gnm4'" in out
    assert "glyma.Wm82.gnm2" in out
    assert "lis_find(" in out


def test_synteny_takes_the_assembly_from_a_gene_id(catalog):
    by_gene = L._synteny({"gene": "glyma.Wm82.gnm2.ann1.Glyma.01G000100"})
    by_genome = L._synteny({"genome": "glyma.Wm82.gnm2"})
    assert by_gene == by_genome


def test_synteny_caps_blocks_and_reports_the_total(catalog):
    out = L._synteny({"genome": "glyma.Wm82.gnm2", "partner": "phavu.G19833.gnm2",
                      "max_blocks": 1})
    assert "1 of 2 block(s)" in out and "[capped]" in out


def test_synteny_unknown_partner_lists_the_real_ones(catalog):
    out = L._synteny({"genome": "glyma.Wm82.gnm2", "partner": "nosuch.X.gnm1"})
    assert "no synteny file pairs" in out and "phavu.G19833.gnm2" in out


def test_synteny_needs_a_genome_or_gene(catalog):
    assert "pass 'genome'" in L._synteny({})


def test_synteny_region_parse_errors_are_reported(catalog):
    out = L._synteny({"genome": "glyma.Wm82.gnm2", "partner": "phavu.G19833.gnm2",
                      "region": "glyma.Wm82.gnm2.Gm06:500-100"})
    assert "start > end" in out
