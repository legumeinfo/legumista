"""LIS Data Store tool tests — discovery, access-mode reporting, and exact gene lookup.

No network: every datastore fetch is served from an in-test fake of the store (an h5ai-
style HTML index plus the four metadata layers), so the tests pin *our* parsing and
classification rather than LIS's uptime. The module-level cache is cleared per test."""
import gzip
import io

import pytest

from legumista_agent import tools_lis as L

# --- a miniature datastore ------------------------------------------------------------
COLL = "Glycine/max/annotations/Wm82.gnm4.ann1.T8TQ"
IDENT = "Wm82.gnm4.ann1.T8TQ"
PREFIX = f"glyma.{IDENT}"

BED_ROWS = [
    ("glyma.Wm82.gnm4.Gm12", 2875800, 2879231,
     "glyma.Wm82.gnm4.ann1.Glyma.12G040000.1", "-", "glyma.Wm82.gnm4.ann1.Glyma.12G040000"),
    ("glyma.Wm82.gnm4.Gm12", 5000, 6000,
     "glyma.Wm82.gnm4.ann1.Glyma.12G0400001.1", "+", "glyma.Wm82.gnm4.ann1.Glyma.12G0400001"),
]
BED_TEXT = "".join(f"{c}\t{s}\t{e}\t{m}\t0\t{st}\t{g}\n" for c, s, e, m, st, g in BED_ROWS)

CHECKSUM = "\n".join(f"d41d8cd98f00b204e9800998ecf8427e  ./{n}" for n in [
    f"README.{IDENT}.yml", f"MANIFEST.{IDENT}.yml", f"CHECKSUM.{IDENT}.md5",
    f"{PREFIX}.protein_primary.faa.gz", f"{PREFIX}.protein_primary.faa.gz.fai",
    f"{PREFIX}.protein_primary.faa.gz.gzi",
    f"{PREFIX}.cds_primary.fna.gz", f"{PREFIX}.cds_primary.fna.gz.fai",
    f"{PREFIX}.gene_models_main.gff3.gz", f"{PREFIX}.gene_models_main.gff3.gz.tbi",
    f"{PREFIX}.gene_models_main.bed.gz", f"{PREFIX}.gene_models_main.bed.gz.tbi",
    f"{PREFIX}.synonym.txt.gz",
    f"{PREFIX}.legume.fam3.VLMQ.gfa.tsv.gz",     # no index -> unreadable
    f"{PREFIX}.info_annot.txt.gz",               # no index -> unreadable
])

README = f"""---
identifier: {IDENT}
scientific_name: Glycine max
synopsis: Glycine max Williams 82 annotation
genotype:
  - Williams 82
publication_doi: 10.1111/tpj.14500
source: "http://phytozome.jgi.doe.gov"
"""

MANIFEST = f"""---
- name: {PREFIX}.protein_primary.faa.gz
  description: Protein sequences - primary only
- name: {PREFIX}.legume.fam3.VLMQ.gfa.tsv.gz
  description: gene family assignments
- name: {PREFIX}.info_annot.txt.gz
  description: MISSING
"""

TRAITS = """---
gene_symbols:
  - GmNARK
gene_model_full_id: glyma.Wm82.gnm4.ann1.Glyma.12G040000
phenotype_synopsis: Long-distance control of nodule proliferation.
references:
  - citation: Searle 2003
    doi: 10.1126/science.1077937
"""

SYNONYMS = "\n".join([
    "glyma.Wm82.gnm4.ann1.Glyma.12G040000.1\tGlyma12g04000",
    "glyma.Wm82.gnm4.ann1.Glyma.99G999999.1\tGlyma99g99999",
])

DESCRIPTION = """---
taxid: 3847
genus: Glycine
species: max
abbrev: glyma
commonName: soybean
"""

# A collection that ships no CHECKSUM — the real shape of every qtl/ and gwas/ collection.
# Its directory listing, like the live h5ai index, omits index siblings entirely.
NOCS_COLL = "Glycine/max/qtl/Demo.qtl.X_1990"
NOCS_IDENT = "Demo.qtl.X_1990"
NOCS_FILES = [f"README.{NOCS_IDENT}.yml",
              f"glyma.{NOCS_IDENT}.qtl.tsv.gz",
              f"glyma.{NOCS_IDENT}.qtlmrk.tsv.gz",
              f"glyma.{NOCS_IDENT}.markers.bed.gz"]          # this one IS indexed, hidden
NOCS_README = f"""---
identifier: {NOCS_IDENT}
synopsis: Demo QTL study
publication_doi: 10.1007/bf00226154
"""
# Only discoverable by probing: absent from the listing above, present on the server.
NOCS_HIDDEN_INDEX = f"{NOCS_COLL}/glyma.{NOCS_IDENT}.markers.bed.gz.tbi"

DIRS = {
    "": ["Glycine/", "Phaseolus/", "README.md"],
    "Glycine": ["max/", "soja/"],
    "Glycine/max": ["annotations/", "genomes/", "about_this_collection/"],
    "Glycine/max/annotations": [f"{IDENT}/", "Wm82.gnm2.ann1.RVB6/"],
    "Glycine/max/qtl": [f"{NOCS_IDENT}/"],
    NOCS_COLL: NOCS_FILES,
}

VCF_COLL = "Glycine/max/diversity/Wm82.gnm2.div.X_2020"
VCF_IDENT = "Wm82.gnm2.div.X_2020"
VCF_CHECKSUM = "\n".join(f"d41d8cd98f00b204e9800998ecf8427e  ./{n}" for n in [
    f"glyma.{VCF_IDENT}.SNPdata.vcf.gz", f"glyma.{VCF_IDENT}.SNPdata.vcf.gz.tbi"])


def _html(path, entries):
    base = "/" + path.strip("/") + "/" if path.strip("/") else "/"
    links = "".join(f'<a href="{base}{e}">{e}</a>' for e in entries)
    return f'<html><a href="..">Parent</a>{links}</html>'


@pytest.fixture(autouse=True)
def fake_store(monkeypatch):
    """Serve the miniature store, and fail loudly on any URL the tests did not stub."""
    L._CACHE.clear()

    def fake_get(url, accept="application/json"):
        path = url[len(L.BASE_URL):].strip("/")
        if path in DIRS:
            return _html(path, DIRS[path])
        return _html(path, [])

    def fake_get_bytes(url, limit=None):
        name = url[len(L.BASE_URL):].strip("/")
        table = {
            f"{COLL}/CHECKSUM.{IDENT}.md5": CHECKSUM,
            f"{COLL}/README.{IDENT}.yml": README,
            f"{COLL}/MANIFEST.{IDENT}.yml": MANIFEST,
            "Glycine/max/about_this_collection/description_Glycine_max.yml": DESCRIPTION,
            f"{VCF_COLL}/CHECKSUM.{VCF_IDENT}.md5": VCF_CHECKSUM,
            f"{NOCS_COLL}/README.{NOCS_IDENT}.yml": NOCS_README,
            "Glycine/max/gene_functions/glyma.traits.yml": TRAITS,
        }
        if name.endswith(".synonym.txt.gz"):
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb") as fh:
                fh.write(SYNONYMS.encode())
            return buf.getvalue()
        if name in table:
            return table[name].encode()
        if name.endswith(".gene_models_main.bed.gz"):
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb") as fh:
                fh.write(BED_TEXT.encode())
            return buf.getvalue()
        raise FileNotFoundError(name)

    probes = []

    def fake_head(url):
        probes.append(url)
        return url == f"{L.BASE_URL}/{NOCS_HIDDEN_INDEX}"

    monkeypatch.setattr(L, "_url_exists", fake_head)
    monkeypatch.setattr(L, "_get", fake_get)
    monkeypatch.setattr(L, "_get_bytes", fake_get_bytes)
    monkeypatch.setattr(L, "_validate_url", lambda u: None)
    yield probes          # tests can assert on what was probed
    L._CACHE.clear()


def _find(**kw):
    return L._find(kw)


# --- lis_find -------------------------------------------------------------------------
def test_find_lists_genera_with_no_arguments():
    out = _find()
    assert "Glycine" in out and "Phaseolus" in out
    assert "README.md" not in out          # files are not genera


def test_find_species_reports_taxid_and_abbrev():
    out = _find(taxon="Glycine max")
    assert "soybean" in out and "taxid:3847" in out and "abbrev:glyma" in out
    assert "annotations" in out


def test_find_splits_a_taxon_string():
    assert _find(taxon="Glycine max") == _find(genus="Glycine", species="max")


def test_find_lists_collections_with_publication_doi():
    """The DOI is the whole point of surfacing READMEs — it hands the literature tools
    a citation for the dataset the agent is about to read."""
    out = _find(taxon="Glycine max", type="annotations")
    assert IDENT in out
    assert "publication_doi: 10.1111/tpj.14500" in out
    assert "openalex_by_doi" in out
    assert f"path: {COLL}" in out


def test_find_query_filters_collections():
    assert "RVB6" not in _find(taxon="Glycine max", type="annotations", query="T8TQ")
    assert "no collection" in _find(taxon="Glycine max", type="annotations", query="zzz")


def test_find_unknown_type_lists_the_real_ones():
    out = _find(taxon="Glycine max", type="nonesuch")
    assert "no collections" in out and "annotations" in out


# --- lis_files ------------------------------------------------------------------------
def test_files_separates_addressable_from_unindexed():
    """The core value: the HTML listing hides index siblings, so without this an agent
    cannot tell a streamable file from an unreadable one."""
    out = L._files({"collection": COLL})
    acc, plain = out.split("NOT INDEXED")
    assert "protein_primary.faa.gz" in acc and "fasta_fetch" in acc
    assert "gene_models_main.gff3.gz" in acc and "tabix_query" in acc
    # no .fai/.tbi published for these two
    assert "legume.fam3.VLMQ.gfa.tsv.gz" in plain
    assert "info_annot.txt.gz" in plain


def test_files_reports_publication_doi_and_descriptions():
    out = L._files({"collection": COLL})
    assert "publication_doi: 10.1111/tpj.14500" in out
    assert "Protein sequences - primary only" in out
    assert "MISSING" not in out             # placeholder descriptions are suppressed


def test_files_hides_index_siblings_from_the_listing():
    """.fai/.tbi/.gzi are evidence about other files, not data files in their own right."""
    out = L._files({"collection": COLL})
    assert ".faa.gz.fai" not in out and ".gff3.gz.tbi" not in out and ".gzi" not in out
    # 7 data files: 2 FASTA + GFF + BED + synonym + the 2 unindexed ones; the 5 index
    # siblings and the 3 metadata files are not data.
    assert "7 data file(s)" in out


def test_files_routes_indexed_vcf_to_bcftools():
    """A .tbi on a VCF means bcftools, not tabix_query - same index, different reader."""
    out = L._files({"collection": VCF_COLL})
    assert "bcftools" in out and "SNPdata.vcf.gz" in out


def test_files_accepts_a_full_url_and_rejects_foreign_hosts():
    assert "publication_doi" in L._files({"collection": f"{L.BASE_URL}/{COLL}"})
    assert "only URLs under" in L._files({"collection": "https://example.org/x/y"})


def test_files_rejects_a_non_collection_path():
    assert "not a collection path" in L._files({"collection": "Glycine"})
    assert "missing 'collection'" in L._files({"collection": ""})


# --- lis_gene -------------------------------------------------------------------------
def test_gene_resolves_locus_and_sequence_handles():
    out = L._gene({"gene": "Glyma.12G040000", "collection": COLL})
    assert "glyma.Wm82.gnm4.Gm12:2875801-2879231" in out     # BED start is 0-based
    assert "fasta_fetch(" in out and "protein_primary.faa.gz" in out
    assert "region='glyma.Wm82.gnm4.ann1.Glyma.12G040000.1'" in out
    assert "tabix_query(" in out
    assert "publication_doi: 10.1111/tpj.14500" in out


def test_gene_matching_is_exact_not_substring():
    """Glyma.12G040000 is a prefix of Glyma.12G0400001; a substring match would return
    both and silently report the wrong locus."""
    out = L._gene({"gene": "Glyma.12G040000", "collection": COLL})
    assert "1 model(s)" in out
    assert "5,001-6,000" not in out and "Glyma.12G0400001" not in out


def test_gene_accepts_prefixed_and_mrna_forms():
    for gid in ("glyma.Wm82.gnm4.ann1.Glyma.12G040000",
                "glyma.Wm82.gnm4.ann1.Glyma.12G040000.1"):
        assert "2875801-2879231" in L._gene({"gene": gid, "collection": COLL})


def test_gene_derives_the_collection_from_a_qualified_id():
    """A qualified ID carries the annotation stem but NOT the 4-char key, so the key is
    recovered by listing the annotations directory."""
    out = L._gene({"gene": "glyma.Wm82.gnm4.ann1.Glyma.12G040000"})
    assert IDENT in out and "2875801-2879231" in out


def test_gene_reports_locus_provenance():
    """Bounds are the BED mRNA extent and can sit inside the GFF gene feature - the
    output must not imply an exact gene extent."""
    assert "mRNA extent from gene_models_main.bed" in L._gene(
        {"gene": "Glyma.12G040000", "collection": COLL})


def test_gene_without_collection_or_qualified_id_asks_for_one():
    assert "pass 'collection'" in L._gene({"gene": "Glyma.12G040000"})


def test_gene_requires_a_gene():
    assert "missing 'gene'" in L._gene({"gene": ""})


# --- registry -------------------------------------------------------------------------
def test_lis_tools_are_read_only_and_well_formed():
    tools = L.lis_tools()
    assert {t.name for t in tools} == {"lis_find", "lis_files", "lis_gene"}
    for t in tools:
        assert t.read_only is True
        assert t.parameters["type"] == "object"
        assert t.parameters.get("additionalProperties") is False
        assert t.description.strip()


def test_lis_tools_are_registered_on_the_mcp_server():
    from legumista_agent.mcp_server import _HANDLERS, build_server
    build_server()
    assert {"lis_find", "lis_files", "lis_gene"} <= set(_HANDLERS)


# --- fallback when a collection publishes no CHECKSUM ---------------------------------
def test_files_falls_back_to_the_directory_listing():
    """Whole collection TYPES ship without a CHECKSUM (every qtl/ and gwas/ collection
    checked). Before the fallback, lis_files failed outright on all of them — and blamed
    the path, which was correct."""
    out = L._files({"collection": NOCS_COLL})
    assert "error" not in out.split("\n")[0]
    assert f"glyma.{NOCS_IDENT}.qtl.tsv.gz" in out
    assert "publication_doi: 10.1007/bf00226154" in out     # README still readable


def test_files_probing_recovers_an_index_the_listing_omits():
    """The whole reason to probe: h5ai filters .fai/.tbi out of the HTML index AND its
    JSON API, so a listing-only fallback would mislabel a streamable file as unreadable."""
    out = L._files({"collection": NOCS_COLL})
    accessible, unindexed = out.split("NOT INDEXED")
    assert f"glyma.{NOCS_IDENT}.markers.bed.gz" in accessible   # found only by probe
    assert "tabix_query" in accessible
    assert f"glyma.{NOCS_IDENT}.qtl.tsv.gz" in unindexed        # genuinely has no index


def test_files_names_the_evidence_for_its_index_claims():
    """A probed 'not indexed' is weaker evidence than a manifest one; say which."""
    probed = L._files({"collection": NOCS_COLL})
    assert "publishes no CHECKSUM" in probed
    assert "no .fai/.tbi sibling found when probed" in probed
    manifested = L._files({"collection": COLL})
    assert "publishes no CHECKSUM" not in manifested
    assert "no .fai/.tbi published" in manifested


def test_files_does_not_probe_when_a_checksum_exists(fake_store):
    """Probing is N requests per collection; it must never fire on the common path."""
    L._files({"collection": COLL})
    assert fake_store == []
    L._files({"collection": NOCS_COLL})
    assert fake_store, "fallback path must probe"
    assert all(u.endswith(tuple(L._ACCESS_BY_INDEX)) for u in fake_store)


def test_files_probes_only_data_files(fake_store):
    """README/MANIFEST/CHANGES/CHECKSUM cannot have index siblings — don't ask."""
    L._files({"collection": NOCS_COLL})
    assert not any("README." in u for u in fake_store)


def test_probe_set_is_narrowed_by_file_type(fake_store):
    """The listing tells us which index siblings are even possible, so a .tsv.gz/.bed.gz
    is asked about .tbi/.csi and never .fai/.bai/.crai. Three tabbed files -> six probes,
    not fifteen."""
    L._files({"collection": NOCS_COLL})
    assert len(fake_store) == 6
    assert all(u.endswith((".tbi", ".csi")) for u in fake_store)


def test_plausible_index_suffixes_by_type():
    f = L._plausible_index_suffixes
    assert f("x.fna.gz") == (".fai",)              # not the generic .gz group
    assert f("x.genome_main.fa.gz") == (".fai",)
    assert f("x.bam") == (".bai", ".csi")
    assert f("x.cram") == (".crai",)
    assert f("x.vcf.gz") == (".tbi", ".csi")       # before the generic .gz group
    assert f("x.gene_models_main.bed.gz") == (".tbi", ".csi")
    assert f("x.qtl.tsv.gz") == (".tbi", ".csi")


def test_unknown_file_type_still_probes_everything():
    """A wrong guess would mislabel streamable data as unreadable, so an unrecognized
    extension stays conservative rather than skipping the file."""
    assert L._plausible_index_suffixes("x.somethingnew") == L._PROBE_SUFFIXES


def test_files_probe_count_is_capped(monkeypatch, fake_store):
    monkeypatch.setattr(L, "MAX_PROBES", 4)
    L._files({"collection": NOCS_COLL})
    assert len(fake_store) == 4


def test_files_empty_listing_and_no_checksum_is_an_honest_error():
    """The old message blamed the path for what was a missing manifest."""
    out = L._files({"collection": "Glycine/max/qtl/DoesNotExist.qtl.Y_2000"})
    assert "could not list" in out and "publishes no" in out


def test_gene_uses_the_same_fallback():
    """A CHECKSUM-less annotation must still resolve rather than dead-end."""
    assert "no gene_models_main.bed.gz" in L._gene(
        {"gene": "Glyma.12G040000", "collection": NOCS_COLL})


# --- alias resolution: curated symbols and superseded IDs -----------------------------
def test_gene_resolves_a_curated_symbol():
    """GmNARK -> Glyma.12G040000 via gene_functions/<abbrev>.traits.yml, which also
    carries the gene's own DOI."""
    out = L._gene({"gene": "GmNARK", "collection": COLL})
    assert "glyma.Wm82.gnm4.ann1.Glyma.12G040000 in" in out
    assert "curated symbol 'GmNARK' in glyma.traits.yml" in out
    assert "10.1126/science.1077937" in out
    assert "2875801-2879231" in out


def test_symbol_match_is_case_insensitive():
    assert "2875801-2879231" in L._gene({"gene": "gmnark", "collection": COLL})


def test_gene_resolves_a_superseded_id():
    out = L._gene({"gene": "Glyma12g04000", "collection": COLL})
    assert "superseded ID 'Glyma12g04000'" in out
    assert "2875801-2879231" in out


def test_exact_id_is_never_shadowed_by_an_alias():
    """An exact hit must win outright — no provenance line, no alias lookup."""
    out = L._gene({"gene": "Glyma.12G040000", "collection": COLL})
    assert "2875801-2879231" in out
    assert "resolved from:" not in out


def test_alias_pointing_outside_the_collection_is_not_a_false_hit():
    """The synonym file maps Glyma99g99999 to a gene absent from this BED; resolving the
    alias must not manufacture a locus."""
    out = L._gene({"gene": "Glyma99g99999", "collection": COLL})
    assert "no match" in out


def test_miss_reports_which_sources_were_consulted():
    out = L._gene({"gene": "NotAGene9", "collection": COLL})
    assert "Consulted:" in out
    assert "curated symbols" in out and "synonym file" in out


def test_miss_explains_that_cross_assembly_names_are_not_synonyms():
    """The A5 case: MtrunA17_Chr1g* against a gnm4 annotation is a different assembly,
    not a superseded name, and no store file expresses that mapping."""
    out = L._gene({"gene": "MtrunA17_Chr1g0147651", "collection": COLL})
    assert "different assembly" in out and "lis_find" in out


def test_symbol_resolution_degrades_where_no_traits_file_exists(monkeypatch):
    """Vigna/Cicer/Arachis publish no traits.yml — that must be a clean miss."""
    monkeypatch.setattr(L, "_traits_symbols", lambda g, s, a: {})
    out = L._gene({"gene": "GmNARK", "collection": COLL})
    assert "no match" in out and "curated symbols" not in out
