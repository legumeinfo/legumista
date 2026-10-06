"""Gene selectors, `extract_features` and `browser_link`.

A small synthetic soybean contig with real bgzipped, indexed FASTA and GFF3 files, so
the htslib worker really runs and every expected sequence is computed from the contig
itself:

    Gm12   A  +  gene 201-500   5'UTR 201-250  CDS 251-460  3'UTR 461-500  (models .1, .2)
           B  -  gene 1001-1300 3'UTR 1001-1040 CDS 1041-1250 5'UTR 1251-1300 (GmNARK)
           C  +  gene 1400-1500 (the neighbour that stops B's upstream flank)
    Gm13   D  +  gene 401-480 on a 500 bp contig (a flank clipped at the contig end)

The gene-model BED, synonym and family files are served by a fake fetch keyed by URL;
nothing touches the network.
"""
import asyncio
import json
import os
import random
import sys
import urllib.parse

import pytest

pysam = pytest.importorskip("pysam")

import config  # noqa: E402
from legumista_agent import genes as G  # noqa: E402
from legumista_agent import tools_browser as B  # noqa: E402
from legumista_agent import tools_catalog as C  # noqa: E402
from legumista_agent import tools_extract as X  # noqa: E402
from legumista_agent import tools_lis as L  # noqa: E402
from legumista_agent.results import coerce  # noqa: E402

if C.DSCENSOR_PATH and C.DSCENSOR_PATH not in sys.path:
    sys.path.insert(0, C.DSCENSOR_PATH)
pytest.importorskip("dscensor.catalog", reason="dscensor is an optional dependency")

DS = "https://data.legumeinfo.org"
P4 = "glyma.Wm82.gnm4.ann1"
A, Bg, Cg, D = (f"{P4}.Glyma.12G010000", f"{P4}.Glyma.12G040000",
                f"{P4}.Glyma.12G050000", f"{P4}.Glyma.13G000100")
GM12, GM13 = "glyma.Wm82.gnm4.Gm12", "glyma.Wm82.gnm4.Gm13"
COMMIT = "abc123def4567890"

random.seed(7)
CONTIG12 = "".join(random.choice("ACGT") for _ in range(2000))
CONTIG13 = "".join(random.choice("ACGT") for _ in range(500))
COMP = str.maketrans("ACGT", "TGCA")


def rc(seq):
    return seq.translate(COMP)[::-1]


def sl(contig, lo, hi):
    """1-based inclusive slice."""
    return contig[lo - 1:hi]


GFF_ROWS = [
    (GM12, "gene", 201, 500, "+", f"ID={A};Note=chalcone synthase [Glycine max]%3B "
     "IPR011141 (Polyketide synthase%2C type III)"),
    (GM12, "mRNA", 201, 500, "+", f"ID={A}.1;Parent={A};longest=1"),
    (GM12, "five_prime_UTR", 201, 250, "+", f"ID={A}.1.utr5;Parent={A}.1"),
    (GM12, "CDS", 251, 460, "+", f"ID={A}.1.cds;Parent={A}.1"),
    (GM12, "three_prime_UTR", 461, 500, "+", f"ID={A}.1.utr3;Parent={A}.1"),
    (GM12, "mRNA", 251, 460, "+", f"ID={A}.2;Parent={A}"),
    (GM12, "CDS", 251, 460, "+", f"ID={A}.2.cds;Parent={A}.2"),
    (GM12, "gene", 1001, 1300, "-", f"ID={Bg};Note=Leucine-rich repeat receptor-like "
     "protein kinase"),
    (GM12, "mRNA", 1001, 1300, "-", f"ID={Bg}.1;Parent={Bg};longest=1"),
    (GM12, "three_prime_UTR", 1001, 1040, "-", f"ID={Bg}.1.utr3;Parent={Bg}.1"),
    (GM12, "CDS", 1041, 1250, "-", f"ID={Bg}.1.cds;Parent={Bg}.1"),
    (GM12, "five_prime_UTR", 1251, 1300, "-", f"ID={Bg}.1.utr5;Parent={Bg}.1"),
    (GM12, "gene", 1400, 1500, "+", f"ID={Cg}"),
    (GM12, "mRNA", 1400, 1500, "+", f"ID={Cg}.1;Parent={Cg}"),
    (GM12, "CDS", 1400, 1500, "+", f"ID={Cg}.1.cds;Parent={Cg}.1"),
    (GM13, "gene", 401, 480, "+", f"ID={D}"),
    (GM13, "mRNA", 401, 480, "+", f"ID={D}.1;Parent={D}"),
    (GM13, "CDS", 401, 480, "+", f"ID={D}.1.cds;Parent={D}.1"),
]
BED4 = "".join(f"{c}\t{s}\t{e}\t{m}\t0\t{st}\t{g}\n" for c, s, e, m, st, g in [
    (GM12, 250, 460, f"{A}.1", "+", A), (GM12, 250, 460, f"{A}.2", "+", A),
    (GM12, 1040, 1250, f"{Bg}.1", "-", Bg), (GM12, 1399, 1500, f"{Cg}.1", "+", Cg),
    (GM13, 400, 480, f"{D}.1", "+", D)])
P2 = "glyma.Wm82.gnm2.ann1"
BED2 = "".join(f"{c}\t{s}\t{e}\t{m}\t0\t+\t{g}\n" for c, s, e, m, g in [
    ("glyma.Wm82.gnm2.Gm12", 900, 1100, f"{P2}.Glyma.12G040000.1", f"{P2}.Glyma.12G040000"),
    ("glyma.Wm82.gnm2.Gm12", 5000, 5100, f"{P2}.Glyma.12G099999.1", f"{P2}.Glyma.12G099999")])
PV = "phavu.G19833.gnm2.ann1"
BEDPV = "".join(f"phavu.G19833.gnm2.Chr02\t{s}\t{e}\t{PV}.{n}.1\t0\t+\t{PV}.{n}\n"
                for s, e, n in [(100, 200, "Phvul.002G100400"), (300, 400, "Phvul.002G100500")])
FAM4 = f"{A}\tLegume.fam3.00001\n{Bg}\tLegume.fam3.00002\n{Cg}\tLegume.fam3.00002\n"
FAMPV = (f"{PV}.Phvul.002G100400\tLegume.fam3.00002\n"
         f"{PV}.Phvul.002G100500\tLegume.fam3.00002\n")
SYN4 = "Glyma.12G040000.1\tGlyma12g04390\n"


def _bgzip(path, text):
    with open(path, "w") as fh:
        fh.write(text)
    pysam.tabix_compress(path, path + ".gz", force=True)
    os.unlink(path)
    return path + ".gz"


def _fasta(records):
    return "".join(f">{name}\n{seq}\n" for name, seq in records)


def _ann(cid, abbrev, base, files, derived=None, genus="Glycine", species="max"):
    return {"path": f"{genus}/{species}/annotations/{cid}", "id": cid,
            "type": "annotations", "genus": genus, "species": species, "base_url": base,
            "index_status": "known", "scientific_name_abbrev": abbrev,
            "derived_from": derived or [], "files": files}


@pytest.fixture
def world(tmp_path, monkeypatch):
    gdir, adir = tmp_path / "genome", tmp_path / "ann"
    gdir.mkdir()
    adir.mkdir()
    genome = _bgzip(str(gdir / "glyma.Wm82.gnm4.4PTR.genome_main.fna"),
                    _fasta([(GM12, CONTIG12), (GM13, CONTIG13)]))
    pysam.faidx(genome)
    cds = {f"{A}.1": sl(CONTIG12, 251, 460), f"{A}.2": sl(CONTIG12, 251, 430),
           f"{Bg}.1": rc(sl(CONTIG12, 1041, 1250)), f"{Cg}.1": sl(CONTIG12, 1400, 1500),
           f"{D}.1": sl(CONTIG13, 401, 480)}
    primary = [k for k in cds if not k.endswith(".2")]
    pre = f"{adir}/{P4}.T8TQ"
    for suffix, recs in [("cds_primary.fna", [(k, cds[k]) for k in primary]),
                         ("cds.fna", list(cds.items())),
                         ("protein_primary.faa", [(k, "M" + "A" * 9) for k in primary]),
                         ("protein.faa", [(k, "M" + "C" * 9) for k in cds])]:
        pysam.faidx(_bgzip(f"{pre}.{suffix}", _fasta(recs)))
    gff_lines = ["##gff-version 3"] + [
        "\t".join([c, "test", t, str(s), str(e), ".", st, ".", a])
        for c, t, s, e, st, a in sorted(GFF_ROWS, key=lambda r: (r[0], r[2], r[3]))]
    pysam.tabix_index(_bgzip(f"{pre}.gene_models_main.gff3", "\n".join(gff_lines) + "\n"),
                      preset="gff", force=True)

    def files(*names):
        return [{"n": f"{P4}.T8TQ.{n}", **({"i": [".fai"]} if n.endswith((".fna.gz", ".faa.gz"))
                                             else {"i": [".tbi"]} if "gff3" in n else {})}
                for n in names]

    catalog = {
        "schema": 1, "built_at": "2026-10-01T00:00:00Z", "source_commit": COMMIT,
        "datastore_url": DS, "stats": {"collections": 6},
        "gene_symbols": {"glyma": {"gmnark": {"gene": Bg}}},
        "collections": [
            {"path": "Glycine/max/genomes/Wm82.gnm4.4PTR", "id": "Wm82.gnm4.4PTR",
             "type": "genomes", "genus": "Glycine", "species": "max",
             "base_url": str(gdir), "index_status": "known",
             "scientific_name_abbrev": "glyma",
             "files": [{"n": "glyma.Wm82.gnm4.4PTR.genome_main.fna.gz", "i": [".fai"]}]},
            _ann("Wm82.gnm4.ann1.T8TQ", "glyma", str(adir), files(
                "cds_primary.fna.gz", "cds.fna.gz", "protein_primary.faa.gz",
                "protein.faa.gz", "gene_models_main.gff3.gz", "gene_models_main.bed.gz",
                "info_synonyms.txt.gz", "legume.fam3.VLMQ.gfa.tsv.gz"),
                derived=["Wm82.gnm4.4PTR"]),
            _ann("Wm82.gnm2.ann1.RVB6", "glyma", f"{DS}/Glycine/max/annotations/Wm82.gnm2.ann1.RVB6",
                 [{"n": f"{P2}.RVB6.gene_models_main.bed.gz"},
                  {"n": f"{P2}.RVB6.gene_models_main.gff3.gz", "i": [".tbi"]}]),
            _ann("G19833.gnm2.ann1.pScz", "phavu",
                 f"{DS}/Phaseolus/vulgaris/annotations/G19833.gnm2.ann1.pScz",
                 [{"n": f"{PV}.pScz.gene_models_main.bed.gz"},
                  {"n": f"{PV}.pScz.legume.fam3.VLMQ.gfa.tsv.gz"}],
                 genus="Phaseolus", species="vulgaris"),
            _ann("ZW6.gnm1.ann1.TKZX", "pissa", f"{DS}/Pisum/sativum/annotations/ZW6.gnm1.ann1.TKZX",
                 [{"n": "pissa.ZW6.gnm1.ann1.TKZX.gene_models_main.gff3.gz", "i": [".csi"]}],
                 genus="Pisum", species="sativum"),
            {"path": "Glycine/max/genome_alignments/Wm82.gnm4.wga.ABCD",
             "id": "Wm82.gnm4.wga.ABCD", "type": "genome_alignments", "genus": "Glycine",
             "species": "max", "base_url": f"{DS}/Glycine/max/genome_alignments/Wm82.gnm4.wga.ABCD",
             "index_status": "known",
             "files": [{"n": "glyma.Wm82.gnm4.x.phavu.G19833.gnm2.ABCD.paf.gz"}]},
        ],
    }
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(catalog))
    monkeypatch.setattr(C, "CATALOG_PATH", str(path))
    C.reset()
    G.reset_caches()
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(config, "WORKSPACE", str(ws))

    fetched = []

    def fake_fetch(url, limit=None):
        fetched.append(url)
        for marker, text in [(f"{P4}.T8TQ.gene_models_main.bed.gz", BED4),
                             (f"{P2}.RVB6.gene_models_main.bed.gz", BED2),
                             (f"{PV}.pScz.gene_models_main.bed.gz", BEDPV),
                             (f"{P4}.T8TQ.info_synonyms.txt.gz", SYN4),
                             (f"{P4}.T8TQ.legume.fam3", FAM4),
                             (f"{PV}.pScz.legume.fam3", FAMPV)]:
            if url.endswith(marker) or marker in url:
                return text, None
        return None, f"unexpected fetch {url}"

    monkeypatch.setattr(L, "_fetch_gz_text", fake_fetch)
    yield {"fetched": fetched, "ws": ws}
    C.reset()
    G.reset_caches()


def run(coro):
    return coerce(asyncio.run(coro))


def extract(**kw):
    allow = kw.pop("allow_write", False)
    return run(X._extract(kw, allow))


def records(text):
    """{label: sequence} from a FASTA reply."""
    out = {}
    for block in text.split("\n>")[1:]:
        header, *seq = block.split("\n")
        out[header.split()[0]] = "".join(seq)
    return out


SEL = {"ids": [A, Bg, Cg], "collection": "Wm82.gnm4.ann1.T8TQ"}


# --- selectors ---------------------------------------------------------------------
def test_every_input_is_accounted_for(world):
    sel = G.resolve({"ids": ["Glyma.12G010000", "GmNARK", "Glyma12g04390",
                             "Glyma.12G777777", f"{P2}.Glyma.12G040000"],
                     "collection": "Wm82.gnm4.ann1.T8TQ"})
    text = sel.summary()
    assert [g.id for g in sel.genes] == [A, Bg]          # B reached twice, listed once
    assert "1 input(s) matched a gene or model ID directly" in text
    assert "GmNARK" in text and "curated symbol" in text
    assert "Glyma12g04390" in text and "superseded ID" in text
    assert "not found Glyma.12G777777" in text
    assert "refused" in text and "one annotation per selection" in text


def test_an_unreadable_synonym_file_is_not_checked_not_absent(world, monkeypatch):
    real = L._fetch_gz_text
    monkeypatch.setattr(L, "_fetch_gz_text", lambda url, limit=None: (
        (None, "HTTP 503") if "synonyms" in url else real(url, limit)))
    sel = G.resolve({"ids": ["Glyma99g99999"], "collection": "Wm82.gnm4.ann1.T8TQ"})
    assert sel.incomplete and "NOT CHECKED Glyma99g99999" in sel.summary()


def test_region_selector_infers_the_annotation_and_keeps_genome_order(world):
    sel = G.resolve({"region": f"{GM12}:1-1450"})
    assert sel.record["id"] == "Wm82.gnm4.ann1.T8TQ"
    assert [g.id for g in sel.genes] == [A, Bg, Cg]
    assert G.resolve({"region": f"{GM12}:1-1450"}).genes == sel.genes   # deterministic


def test_offset_pages_a_selection(world, monkeypatch):
    monkeypatch.setattr(G, "MAX_GENES", 2)
    first = G.resolve({"region": f"{GM12}:1-2000"})
    assert [g.id for g in first.genes] == [A, Bg] and first.total == 3
    assert "pass offset=2" in first.summary()
    assert [g.id for g in G.resolve({"region": f"{GM12}:1-2000", "offset": 2}).genes] == [Cg]


def test_family_selector_reads_the_assignment_file_once(world):
    sel = G.resolve({"family": "legume.fam3.00002", "collection": "Wm82.gnm4.ann1.T8TQ"})
    assert [g.id for g in sel.genes] == [Bg, Cg]
    G.resolve({"family": "Legume.fam3.00001", "collection": "Wm82.gnm4.ann1.T8TQ"})
    assert sum("gfa.tsv.gz" in u for u in world["fetched"]) == 1, "the cache was not used"
    assert "not a family id" in G.resolve({"family": "pfam.PF00001",
                                           "collection": "Wm82.gnm4.ann1.T8TQ"}).error


def test_translate_within_a_species_by_gene_name(world):
    sel = G.resolve({"ids": [Bg, A], "translate_to": "Wm82.gnm2.ann1.RVB6"})
    assert sel.record["id"] == "Wm82.gnm2.ann1.RVB6"
    assert [g.id for g in sel.genes] == [f"{P2}.Glyma.12G040000"]
    text = sel.summary()
    assert "same gene name" in text and "no match by name or synonym" in text


def test_translate_across_species_is_one_to_many_through_families(world):
    sel = G.resolve({"ids": [Bg], "collection": "Wm82.gnm4.ann1.T8TQ",
                     "translate_to": "G19833.gnm2.ann1.pScz"})
    assert {g.id for g in sel.genes} == {f"{PV}.Phvul.002G100400", f"{PV}.Phvul.002G100500"}
    assert "paralog" in sel.summary()


def test_selector_shape_errors(world):
    assert "exactly one of" in G.resolve({"ids": [A], "region": "x"}).error
    assert "at most" in G.resolve({"ids": ["x"] * 201, "collection": "T8TQ"}).error
    assert "selector object" in G.resolve("GmNARK").error


# --- extract_features ----------------------------------------------------------------
def test_cds_and_protein_use_the_primary_file(world):
    out = extract(genes=SEL, feature="cds")
    got = records(out.text)
    assert got == {f"{A}.1": sl(CONTIG12, 251, 460), f"{Bg}.1": rc(sl(CONTIG12, 1041, 1250)),
                   f"{Cg}.1": sl(CONTIG12, 1400, 1500)}
    assert "cds_notes" in out.text and "notes, not defects" in out.text
    assert set(records(extract(genes=SEL, feature="protein").text).values()) == {"M" + "A" * 9}


def test_isoforms_all_reads_every_model_from_the_full_file(world):
    got = records(extract(genes={"ids": [A], "collection": "Wm82.gnm4.ann1.T8TQ"},
                          feature="cds", isoforms="all").text)
    assert got == {f"{A}.1": sl(CONTIG12, 251, 460), f"{A}.2": sl(CONTIG12, 251, 430)}


def test_gene_span_comes_from_the_gff3_not_the_coding_extent(world):
    got = records(extract(genes=SEL, feature="gene").text)
    assert got[A] == sl(CONTIG12, 201, 500)
    assert got[Bg] == rc(sl(CONTIG12, 1001, 1300))


def test_minus_strand_upstream_lies_at_higher_coordinates(world):
    b = {"ids": [Bg], "collection": "Wm82.gnm4.ann1.T8TQ"}
    out = extract(genes=b, feature="upstream", flank=100)
    assert records(out.text)[Bg] == rc(sl(CONTIG12, 1301, 1400))
    assert f"loc={GM12}:1301-1400(-)" in out.text
    down = extract(genes=b, feature="downstream", flank=100)
    assert records(down.text)[Bg] == rc(sl(CONTIG12, 901, 1000))


def test_stop_at_neighbor_truncates_the_flank(world):
    out = extract(genes={"ids": [Bg], "collection": "Wm82.gnm4.ann1.T8TQ"},
                  feature="upstream", flank=200, stop_at_neighbor=True)
    assert records(out.text)[Bg] == rc(sl(CONTIG12, 1301, 1399))
    assert f"stopped_at_neighbor={Cg}" in out.text


def test_start_codon_anchor_and_utrs(world):
    a = {"ids": [A], "collection": "Wm82.gnm4.ann1.T8TQ"}
    assert records(extract(genes=a, feature="upstream", flank=50,
                           anchor="start_codon").text)[f"{A}.1"] == sl(CONTIG12, 201, 250)
    b = {"ids": [Bg], "collection": "Wm82.gnm4.ann1.T8TQ"}
    assert records(extract(genes=b, feature="utr5").text)[f"{Bg}.1"] == \
        rc(sl(CONTIG12, 1251, 1300))
    assert records(extract(genes=b, feature="utr3").text)[f"{Bg}.1"] == \
        rc(sl(CONTIG12, 1001, 1040))



def test_a_csi_only_gff_is_read(world):
    """Four pea, two faba bean and one lentil annotation index their GFF3 with CSI only;
    pysam alone would look for a .tbi and fail every genomic feature."""
    doc = json.loads(open(C.CATALOG_PATH).read())
    ann = next(c for c in doc["collections"] if c["id"] == "Wm82.gnm4.ann1.T8TQ")
    gff = os.path.join(ann["base_url"], f"{P4}.T8TQ.gene_models_main.gff3.gz")
    os.unlink(gff + ".tbi")
    pysam.tabix_index(gff, preset="gff", csi=True, force=True)
    for entry in ann["files"]:
        if entry["n"].endswith("gene_models_main.gff3.gz"):
            entry["i"] = [".csi"]
    open(C.CATALOG_PATH, "w").write(json.dumps(doc))
    C.reset()
    out = extract(genes={"ids": [Bg], "collection": "Wm82.gnm4.ann1.T8TQ"}, feature="gene")
    assert records(out.text)[Bg] == rc(sl(CONTIG12, 1001, 1300))


def test_lis_gene_reads_the_gene_span_from_the_real_gff3(world):
    """lis_gene end to end through the worker: B's coding extent is 1,041-1,250, but the
    gene, UTRs included, spans 1,001-1,300 on the minus strand."""
    out = L._gene({"gene": Bg})
    assert f"gene span:  {GM12}:1,001-1,300 (-)" in out
    assert f"mRNA {Bg}.1: 1,001-1,300" in out
    assert f"coding extent:  {GM12}:1,041-1,250 (-)" in out
    assert f"(gene span): {GM12}:1001-1300" in out

def test_a_gene_without_a_utr_is_a_problem_not_an_empty_record(world):
    out = extract(genes={"ids": [Cg], "collection": "Wm82.gnm4.ann1.T8TQ"}, feature="utr5")
    assert records(out.text) == {}
    assert "no annotated five_prime_UTR" in out.text


def test_a_flank_is_clipped_at_the_contig_end(world):
    out = extract(genes={"ids": [D], "collection": "Wm82.gnm4.ann1.T8TQ"},
                  feature="downstream", flank=100)
    assert records(out.text)[D] == sl(CONTIG13, 481, 500)
    assert "clipped_at_contig_end" in out.text


def test_dry_run_sizes_without_fetching_sequence(world, monkeypatch):
    calls = []
    real = X._run_worker
    monkeypatch.setattr(X, "_run_worker", lambda req, lim=0: calls.append(req["items"])
                        or real(req, lim))
    out = extract(genes=SEL, feature="gene", dry_run=True)
    assert "dry run: 3 record(s), 701 bp; nothing fetched" in out.text
    assert len(calls) == 1 and not any(i.get("end") for i in calls[0] if i["kind"] == "fasta")


def test_one_worker_reads_a_whole_selection(world, monkeypatch):
    calls = []
    real = X._run_worker
    monkeypatch.setattr(X, "_run_worker", lambda req, lim=0: calls.append(1) or real(req, lim))
    extract(genes=SEL, feature="upstream", flank=100)
    assert len(calls) == 2, "planning + fetching: one worker each, whatever the gene count"


def test_large_output_without_write_access_says_what_it_left_out(world, monkeypatch):
    monkeypatch.setattr(X, "INLINE_CHARS", 1200)
    out = extract(genes=SEL, feature="gene")
    assert "showing" in out.text and "without --allow-write" in out.text
    assert len(records(out.text)) < 3
    assert not list(world["ws"].iterdir())


def test_large_output_with_write_access_lands_in_server_named_files(world, monkeypatch):
    monkeypatch.setattr(X, "INLINE_CHARS", 1200)
    out = extract(genes=SEL, feature="gene", allow_write=True)
    names = sorted(p.name for p in world["ws"].iterdir())
    assert len(names) == 2 and names[0].startswith("extract_gene_")
    assert names[0].endswith(".bed") and names[1].endswith(".fa")
    bed = (world["ws"] / names[0]).read_text().splitlines()
    assert f"{GM12}\t200\t500\t{A}\t0\t+" in bed            # 0-based start
    assert names[1] in out.text


def test_the_total_size_is_capped(world, monkeypatch):
    monkeypatch.setattr(X, "MAX_TOTAL_BP", 500)
    out = extract(genes=SEL, feature="gene")
    assert out.is_error and "over the 500 bp limit" in out.text


def test_bad_arguments_fail_clearly(world):
    assert "feature" in extract(genes=SEL, feature="exon").text
    assert "between 1 and" in extract(genes=SEL, feature="upstream", flank=50_000).text


# --- browser_link --------------------------------------------------------------------
def _params(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def _links(text):
    return [line.strip() for line in text.splitlines() if line.strip().startswith("https://")]


def test_a_gene_link_highlights_it_on_the_gene_track(world):
    out = run(asyncio.to_thread(B._browser_link, {"genes": {"ids": ["GmNARK"],
                                                            "collection": "Wm82.gnm4.ann1.T8TQ"}}))
    (url,) = _links(out.text)
    params = _params(url)
    assert params["assembly"] == "glyma.Wm82.gnm4"
    assert params["loc"] == f"{GM12}:41-2250"
    assert params["highlight"] == f"{GM12}:1041-1250"
    assert params["tracks"] == f"{P4}.T8TQ.gene_models_main.gff3"
    assert "PREDICTED" in out.text and "no JBrowse placements yet" in out.text


def test_inline_features_are_zero_based_half_open(world):
    out = run(asyncio.to_thread(B._browser_link, {
        "genes": {"ids": [Bg], "collection": "Wm82.gnm4.ann1.T8TQ"}, "mark": "features"}))
    session = json.loads(_params(_links(out.text)[0])["sessionTracks"])
    (feature,) = session[0]["adapter"]["features"]
    assert (feature["start"], feature["end"], feature["strand"]) == (1040, 1250, -1)


def test_a_csi_only_gff_becomes_a_session_track_with_its_csi(world):
    out = run(asyncio.to_thread(B._browser_link, {"region": "pissa.ZW6.gnm1.chr1:1-1000"}))
    session = json.loads(_params(_links(out.text)[0])["sessionTracks"])
    adapter = session[0]["adapter"]
    assert _params(_links(out.text)[0])["tracks"] == session[0]["trackId"]   # shown
    assert adapter["index"]["indexType"] == "CSI"
    assert adapter["index"]["location"]["uri"].startswith(B.DATA_HOST)
    assert adapter["index"]["location"]["uri"].endswith(".gff3.gz.csi")


def test_a_url_is_never_accepted_as_a_track(world):
    out = run(asyncio.to_thread(B._browser_link, {
        "region": f"{GM12}:1-100", "tracks": ["https://evil.example/x.gff3.gz"]}))
    assert out.is_error and "never accepted" in out.text


def test_dotplot_uses_the_catalogs_alignment(world):
    out = run(asyncio.to_thread(B._browser_link, {
        "region": f"{GM12}:1-100", "view": "dotplot", "compare": "phavu.G19833.gnm2"}))
    spec = json.loads(_params(_links(out.text)[0])["session"][len("spec-"):])
    view = spec["views"][0]
    assert view["type"] == "DotplotView"
    assert view["tracks"] == ["glyma.Wm82.gnm4.x.phavu.G19833.gnm2.ABCD.paf"]
    missing = run(asyncio.to_thread(B._browser_link, {
        "region": f"{GM12}:1-100", "view": "dotplot", "compare": "medtr.A17.gnm5"}))
    assert missing.is_error and "no synteny track" in missing.text


def test_a_long_url_marks_fewer_genes_and_says_so(world, monkeypatch):
    monkeypatch.setattr(B, "MAX_URL_CHARS", 420)
    out = run(asyncio.to_thread(B._browser_link, {
        "genes": {"region": f"{GM12}:1-2000"}, "mark": "features"}))
    assert "the URL limit cut the rest" in out.text



# --- browser_link with the catalog's JBrowse placements ------------------------------
AG = "https://all-genera.lis.ncgr.org/tools/jbrowse2/"
CICER = "https://cicer.legumeinfo.org/tools/jbrowse2/"
GENE_TRACK = f"{P4}.T8TQ.gene_models_main.gff3"


@pytest.fixture
def placed(world):
    """The fixture catalog as populate-catalog writes it once it records placements
    (JBROWSE_IDS_IN_CATALOG.md): two instances up, one unavailable at build time."""
    doc = json.loads(open(C.CATALOG_PATH).read())
    doc["jbrowse_instances"] = {
        "all-genera": {"url": AG, "status": "ok", "fetched_at": "2026-10-05T12:00:00Z"},
        "cicer": {"url": CICER, "status": "ok", "fetched_at": "2026-10-05T12:00:00Z"},
        "peanutbase": {"url": "https://www.peanutbase.org/tools/jbrowse2/",
                       "status": "unavailable", "detail": "HTTP 503"}}
    by_id = {c["id"]: c for c in doc["collections"]}
    by_id["Wm82.gnm4.4PTR"]["jbrowse"] = [
        {"instance": "all-genera", "assemblies": ["glyma.Wm82.gnm4"]},
        {"instance": "cicer", "assemblies": ["glyma.Wm82.gnm4"]}]
    by_id["Wm82.gnm4.ann1.T8TQ"]["jbrowse"] = [
        {"instance": "all-genera", "assemblies": ["glyma.Wm82.gnm4"],
         "tracks": [{"id": GENE_TRACK, "type": "FeatureTrack",
                     "file": f"{P4}.T8TQ.gene_models_main.gff3.gz", "index": "TBI"}]},
        {"instance": "cicer", "assemblies": ["glyma.Wm82.gnm4"],
         "tracks": [{"id": "glyma_custom_genes", "type": "FeatureTrack",
                     "file": f"{P4}.T8TQ.gene_models_main.gff3.gz", "index": "TBI"}]}]
    doc["collections"].append(
        {"path": "Pisum/sativum/genomes/ZW6.gnm1.ABCD", "id": "ZW6.gnm1.ABCD",
         "type": "genomes", "genus": "Pisum", "species": "sativum",
         "base_url": f"{DS}/Pisum/sativum/genomes/ZW6.gnm1.ABCD", "index_status": "known",
         "scientific_name_abbrev": "pissa", "files": [],
         "jbrowse": [{"instance": "all-genera", "assemblies": ["pissa.ZW6.gnm1"]}]})
    by_id["ZW6.gnm1.ann1.TKZX"]["jbrowse"] = [
        {"instance": "all-genera", "assemblies": ["pissa.ZW6.gnm1"],
         "tracks": [{"id": "pissa.ZW6.gnm1.ann1.TKZX.gene_models_main.gff3",
                     "type": "FeatureTrack", "index": "TBI",
                     "file": "pissa.ZW6.gnm1.ann1.TKZX.gene_models_main.gff3.gz"}]}]
    by_id["Wm82.gnm4.wga.ABCD"]["jbrowse"] = [
        {"instance": "all-genera", "assemblies": ["glyma.Wm82.gnm4", "phavu.G19833.gnm2"],
         "tracks": [{"id": "deployed.synteny.track", "type": "SyntenyTrack",
                     "file": "glyma.Wm82.gnm4.x.phavu.G19833.gnm2.ABCD.paf.gz"}]}]
    doc["collections"].append(
        {"path": "Phaseolus/vulgaris/genomes/G19833.gnm2.fC0g", "id": "G19833.gnm2.fC0g",
         "type": "genomes", "genus": "Phaseolus", "species": "vulgaris",
         "base_url": f"{DS}/Phaseolus/vulgaris/genomes/G19833.gnm2.fC0g",
         "index_status": "known", "scientific_name_abbrev": "phavu", "files": []})
    open(C.CATALOG_PATH, "w").write(json.dumps(doc))
    C.reset()
    return world


def link(**args):
    return run(asyncio.to_thread(B._browser_link, args))


def test_placed_names_come_from_the_deployed_config(placed):
    out = link(genes={"ids": ["GmNARK"], "collection": "Wm82.gnm4.ann1.T8TQ"})
    (url,) = _links(out.text)
    assert url.startswith(AG)
    assert _params(url)["tracks"] == GENE_TRACK
    assert "from all-genera's deployed config" in out.text and "PREDICTED" not in out.text


def test_a_track_only_a_genus_portal_serves_picks_that_portal(placed):
    out = link(region=f"{GM12}:1-2000", tracks=["glyma_custom_genes"])
    (url,) = _links(out.text)
    assert url.startswith(CICER) and _params(url)["tracks"] == "glyma_custom_genes"


def test_an_assembly_no_instance_serves_is_a_finding_with_what_was_checked(placed):
    out = link(region="phavu.G19833.gnm2.Chr02:1-1000")
    assert not out.is_error and not _links(out.text)
    assert "no LIS JBrowse instance serves phavu.G19833.gnm2 (checked: all-genera, cicer)" \
        in out.text
    assert "NOT CHECKED: peanutbase" in out.text


def test_an_explicit_instance_that_does_not_serve_it_says_who_does(placed):
    out = link(region="pissa.ZW6.gnm1.chr1:1-1000", instance="cicer")
    assert "cicer does not serve pissa.ZW6.gnm1; all-genera does" in out.text
    assert "NOT CHECKED" in link(region=f"{GM12}:1-100", instance="peanutbase").text


def test_a_config_pointing_at_a_missing_tbi_gets_the_csi_session_track(placed):
    out = link(region="pissa.ZW6.gnm1.chr1:1-1000")
    params = _params(_links(out.text)[0])
    session = json.loads(params["sessionTracks"])
    assert session[0]["adapter"]["index"]["indexType"] == "CSI"
    assert params["tracks"] == session[0]["trackId"]
    assert "points ZW6.gnm1.ann1.TKZX's gene models at a .tbi" in out.text


def test_a_track_id_the_catalog_does_not_place_is_flagged_not_refused(placed):
    out = link(region=f"{GM12}:1-2000", tracks=[GENE_TRACK, "someone_elses_track"])
    assert _links(out.text) and "not in the catalog's placements for all-genera: " \
        "someone_elses_track" in out.text


def test_the_dotplot_uses_the_placed_synteny_track(placed):
    out = link(region=f"{GM12}:1-100", view="dotplot", compare="G19833.gnm2.fC0g")
    spec = json.loads(_params(_links(out.text)[0])["session"][len("spec-"):])
    assert spec["views"][0]["tracks"] == ["deployed.synteny.track"]
    assert _links(out.text)[0].startswith(AG)


# --- lis_gene over a selector -----------------------------------------------------------
def test_lis_gene_lists_a_family_with_spans_and_descriptions(world):
    """A family's members in one annotation, complete, with what each gene is described
    as — so a description, not an ID's letters, is what an agent reads."""
    out = L._gene({"genes": {"family": "Legume.fam3.00002",
                             "collection": "Wm82.gnm4.ann1.T8TQ"}})
    assert "selection: 2 gene(s) in Wm82.gnm4.ann1.T8TQ" in out
    assert f"  {Bg} | {GM12}:1,001-1,300 (-) | Leucine-rich repeat receptor-like " \
        "protein kinase" in out
    assert f"  {Cg} | {GM12}:1,400-1,500 (+) | (the gene row has no Note)" in out
    assert "cannot tell them apart" in out


def test_lis_gene_list_shows_only_the_product_name(world):
    out = L._gene({"genes": {"ids": [A], "collection": "Wm82.gnm4.ann1.T8TQ"}})
    assert f"  {A} | {GM12}:201-500 (+) | chalcone synthase [Glycine max]\n" in out
    assert "IPR011141" not in out


def test_lis_gene_on_one_gene_gives_the_whole_note(world):
    out = L._gene({"gene": A})
    assert ("description: chalcone synthase [Glycine max]; IPR011141 (Polyketide "
            "synthase, type III)") in out
    assert "not a demonstrated function" in out


def test_lis_gene_list_pages_when_its_rows_overrun_the_reply(world, monkeypatch):
    """Rows past the reply cap are dropped whole, and the selection's own paging line
    names the offset that continues it."""
    sel = {"family": "Legume.fam3.00002", "collection": "Wm82.gnm4.ann1.T8TQ"}
    full = L._gene({"genes": sel})
    monkeypatch.setattr(L, "MAX_CHARS", len(full) - 60)
    out = L._gene({"genes": sel})
    assert "genes 1–1 of 2" in out and "pass offset=1 for the next page" in out
    assert Bg in out and f"  {Cg} |" not in out
    nxt = L._gene({"genes": {**sel, "offset": 1}})
    assert "genes 2–2 of 2" in nxt and f"  {Cg} |" in nxt


def test_lis_gene_list_marks_an_unread_gff3_not_checked(world, monkeypatch):
    monkeypatch.setattr(L, "_read_gff", lambda *a: ([], "timeout"))
    out = L._gene({"genes": {"ids": [Bg], "collection": "Wm82.gnm4.ann1.T8TQ"}})
    assert f"  {Bg} | {GM12}:1,041-1,250 (-) [coding extent] | NOT CHECKED" in out
    assert "gene spans and descriptions NOT CHECKED — timeout" in out


def test_lis_gene_takes_one_gene_or_a_selector_not_both(world):
    out = L._gene({"gene": A, "genes": {"ids": [A]}})
    assert out.startswith("error:") and "not both" in out
    assert "'genes', a selector" in L._gene({})
