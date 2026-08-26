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

DESCRIPTION = """---
taxid: 3847
genus: Glycine
species: max
abbrev: glyma
commonName: soybean
"""

DIRS = {
    "": ["Glycine/", "Phaseolus/", "README.md"],
    "Glycine": ["max/", "soja/"],
    "Glycine/max": ["annotations/", "genomes/", "about_this_collection/"],
    "Glycine/max/annotations": [f"{IDENT}/", "Wm82.gnm2.ann1.RVB6/"],
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
        }
        if name in table:
            return table[name].encode()
        if name.endswith(".gene_models_main.bed.gz"):
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb") as fh:
                fh.write(BED_TEXT.encode())
            return buf.getvalue()
        raise FileNotFoundError(name)

    monkeypatch.setattr(L, "_get", fake_get)
    monkeypatch.setattr(L, "_get_bytes", fake_get_bytes)
    monkeypatch.setattr(L, "_validate_url", lambda u: None)
    yield
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
    # 6 data files: 2 FASTA + GFF + BED + the 2 unindexed ones; the 5 index siblings
    # and the 3 metadata files are not data.
    assert "6 data file(s)" in out


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


def test_gene_miss_says_it_is_exact_only():
    out = L._gene({"gene": "GmNARK", "collection": COLL})
    assert "no exact match" in out and "symbols" in out


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
