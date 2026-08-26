"""pysam bioinformatics-tool tests — build tiny indexed fixtures and exercise the general
samtools/bcftools dispatchers, the read-only helpers, write gating, and the workspace/
SSRF sandbox. Skipped when the optional `bio` extra (pysam) is absent. No network, no
model calls; the workspace is pointed at a tmp fixture dir."""
import os

import pytest

pysam = pytest.importorskip("pysam", reason="pysam is a legumista dependency — reinstall the package")

import config
from legumista_agent import tools_pysam as P


@pytest.fixture
def fixtures(tmp_path, monkeypatch):
    """A tmp workspace with an indexed FASTA, sorted+indexed BAM, tabix VCF, tabix GFF."""
    d = tmp_path
    monkeypatch.setattr(config, "WORKSPACE", str(d))

    fa = d / "genome.fa"
    fa.write_text(">chr1\n" + "ACGTACGTAC" * 20 + "\n>chr2\n" + "TTTTGGGGCC" * 10 + "\n")
    pysam.faidx(str(fa))

    header = {"HD": {"VN": "1.0"},
              "SQ": [{"LN": 200, "SN": "chr1"}, {"LN": 100, "SN": "chr2"}]}
    raw = d / "reads.bam"
    with pysam.AlignmentFile(str(raw), "wb", header=header) as out:
        for i in range(5):
            a = pysam.AlignedSegment()
            a.query_name = f"read{i}"
            a.query_sequence = "ACGTACGTAC"
            a.flag = 0
            a.reference_id = 0
            a.reference_start = 10 + i * 5
            a.mapping_quality = 60
            a.cigar = [(0, 10)]
            out.write(a)
    bam = d / "reads.sorted.bam"
    pysam.sort("-o", str(bam), str(raw))
    pysam.index(str(bam))

    vcf = d / "variants.vcf"
    vcf.write_text(
        "##fileformat=VCFv4.2\n##contig=<ID=chr1,length=200>\n"
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1\tS2\n"
        "chr1\t15\trs1\tA\tG\t50\tPASS\t.\tGT\t0/1\t1/1\n"
        "chr1\t25\t.\tC\tT\t99\tPASS\t.\tGT\t0/0\t0/1\n")
    pysam.tabix_compress(str(vcf), str(vcf) + ".gz", force=True)
    pysam.tabix_index(str(vcf) + ".gz", preset="vcf", force=True)

    gff = d / "ann.gff3"
    gff.write_text("chr1\ttest\tgene\t10\t50\t.\t+\t.\tID=gene1\n"
                   "chr1\ttest\texon\t12\t30\t.\t+\t.\tID=exon1\n")
    pysam.tabix_compress(str(gff), str(gff) + ".gz", force=True)
    pysam.tabix_index(str(gff) + ".gz", preset="gff", force=True)
    return d


def _sam(args, allow_write=False):
    return P._dispatch("samtools", P._SAM_READ, allow_write, {"args": args})


def _bcf(args, allow_write=False):
    return P._dispatch("bcftools", P._BCF_READ, allow_write, {"args": args})


# --- general dispatchers: reads -------------------------------------------------------
def test_samtools_read_subcommands(fixtures):
    assert _sam(["view", "-c", "reads.sorted.bam"]).strip() == "5"
    assert "in total" in _sam(["flagstat", "reads.sorted.bam"])
    assert "chr1" in _sam(["idxstats", "reads.sorted.bam"])


def test_bcftools_read_subcommands(fixtures):
    out = _bcf(["view", "-H", "variants.vcf.gz", "chr1:1-30"])
    assert "rs1" in out and out.count("\n") >= 1
    assert "number of records" in _bcf(["stats", "variants.vcf.gz"])


# --- write gating ---------------------------------------------------------------------
def test_write_subcommand_gated(fixtures):
    # sort is a write: refused without allow_write, runs with it
    assert "writes are disabled" in _sam(["sort", "-o", "s.bam", "reads.sorted.bam"])
    assert "completed" in _sam(["sort", "-o", "s.bam", "reads.sorted.bam"], allow_write=True)
    assert (fixtures / "s.bam").exists()


def test_output_flag_makes_a_read_subcommand_a_write(fixtures):
    # `view` reads, but `view -o file` writes -> gated
    assert "writes are disabled" in _sam(["view", "-o", "out.sam", "reads.sorted.bam"])


# --- sandbox + SSRF on dispatcher argv ------------------------------------------------
def test_argv_path_sandbox(fixtures):
    assert "outside the project workspace" in _sam(["view", "/etc/passwd"])
    assert "outside the project workspace" in _sam(
        ["sort", "-o", "../evil.bam", "reads.sorted.bam"], allow_write=True)


def test_argv_url_ssrf(fixtures):
    assert "blocked URL" in _sam(["view", "http://169.254.169.254/x.bam"])
    assert "refused URL scheme" in _sam(["view", "ftp://example.org/x.bam"])


def test_unknown_subcommand(fixtures):
    assert "unknown samtools subcommand" in _sam(["definitely-not-a-subcommand"])


# --- read-only helpers ----------------------------------------------------------------
def test_fasta_fetch(fixtures):
    out = P._fasta_fetch({"path": "genome.fa", "region": "chr1:1-20"})
    assert "ACGTACGTACACGTACGTAC" in out and "1-20 (20 bp" in out


def test_tabix_query(fixtures):
    assert "1 contig(s)" in P._tabix_query({"path": "ann.gff3.gz"})
    out = P._tabix_query({"path": "ann.gff3.gz", "region": "chr1:1-40"})
    assert "ID=gene1" in out and "ID=exon1" in out


def test_tabix_index_is_write_gated(fixtures):
    # bio_tools wires allow_write into the tool; the handler itself must fail closed
    tools = {t.name: t for t in P.bio_tools(allow_write=False)}
    # tabix_index always classifies as a write
    assert tools["tabix_index"].writes({}) is True
    assert tools["tabix_index"].read_only is False


def test_parse_region_follows_samtools_coordinate_spec():
    """samtools regions are 1-based inclusive; pysam's fetch is 0-based half-open. The
    parser converts start -> lo-1, keeps the inclusive hi as the exclusive bound (so span
    width == inclusive base count), strips commas, and rejects inverted/empty regions."""
    assert P._parse_region("chr1:1000-2000") == ("chr1", 999, 2000, None)
    assert P._parse_region("chr1:1-1") == ("chr1", 0, 1, None)        # just the first base
    assert P._parse_region("chr2:500") == ("chr2", 499, 500, None)    # single pos -> 1 bp
    assert P._parse_region("chr1:1,000-2,000") == ("chr1", 999, 2000, None)  # commas stripped
    assert P._parse_region("chr1") == ("chr1", None, None, None)      # whole contig
    assert P._parse_region("chr1:5-1")[3].startswith("error")         # start > end
    assert P._parse_region("")[3].startswith("error")                 # empty


# --- CA bundle for htslib's vendored libcurl ------------------------------------------
def test_ensure_ca_bundle_respects_operator_setting(monkeypatch):
    """An explicitly-set CURL_CA_BUNDLE is never overridden — that is how a corporate
    TLS-inspecting proxy is configured."""
    monkeypatch.setenv(P._CA_ENV, "/some/site/roots.pem")
    P._ensure_ca_bundle()
    assert os.environ[P._CA_ENV] == "/some/site/roots.pem"


def test_ensure_ca_bundle_prefers_system_store(monkeypatch, tmp_path):
    """With nothing set, the first *existing* system bundle wins (so a site's added roots
    keep working); missing candidates ahead of it are skipped."""
    present = tmp_path / "ca-certificates.crt"
    present.write_text("")
    monkeypatch.delenv(P._CA_ENV, raising=False)
    monkeypatch.setattr(P, "_SYSTEM_CA_BUNDLES", ("/nonexistent/pki.crt", str(present)))
    P._ensure_ca_bundle()
    assert os.environ[P._CA_ENV] == str(present)


def test_ensure_ca_bundle_falls_back_to_certifi(monkeypatch):
    """No system bundle (the manylinux-wheel-on-a-slim-image case) -> certifi's."""
    certifi = pytest.importorskip("certifi")
    monkeypatch.delenv(P._CA_ENV, raising=False)
    monkeypatch.setattr(P, "_SYSTEM_CA_BUNDLES", ("/nonexistent/a.crt", "/nonexistent/b.crt"))
    P._ensure_ca_bundle()
    assert os.environ[P._CA_ENV] == certifi.where()


def test_pysam_import_sets_ca_bundle(monkeypatch):
    """The fix has to land before the first remote open, so _pysam() applies it."""
    monkeypatch.delenv(P._CA_ENV, raising=False)
    mod, err = P._pysam()
    assert err is None and mod is not None
    assert os.path.exists(os.environ[P._CA_ENV])


# --- bcftools query -l (pysam's -o injection swallows it) -----------------------------
def test_bcftools_query_list_samples(fixtures):
    """pysam captures dispatcher output by appending `-o <tmpfile>` and pointing the real
    stdout at /dev/null; `query -l` printf()s to stdout, so it would come back empty."""
    out = _bcf(["query", "-l", "variants.vcf.gz"])
    assert "2 sample(s)" in out
    assert out.strip().endswith("S1\nS2")


def test_bcftools_query_list_samples_long_flag(fixtures):
    assert "S1" in _bcf(["query", "--list-samples", "variants.vcf.gz"])


def test_bcftools_query_list_samples_reports_open_errors(fixtures):
    assert "error" in _bcf(["query", "-l", "no_such_file.vcf.gz"]).lower()


def test_bcftools_query_list_samples_is_sandboxed(fixtures):
    """The interception happens after _guard_argv, so the workspace sandbox still holds."""
    assert "outside the project workspace" in _bcf(["query", "-l", "/etc/passwd"])


def test_bcftools_query_list_samples_declines_mixed_argv(fixtures):
    """Combined with other options it is not a plain listing — fall through to the
    dispatcher rather than guessing which token is the file."""
    assert P._bcftools_list_samples(pysam, ["-l", "-r", "chr1", "variants.vcf.gz"]) is None
    assert P._bcftools_list_samples(pysam, ["-l"]) is None


def test_bcftools_query_format_still_dispatches(fixtures):
    """Regression guard: the normal `query -f` path writes through `-o` and must keep
    going to the real dispatcher."""
    out = _bcf(["query", "-f", "%CHROM\t%POS\n", "variants.vcf.gz"])
    assert "chr1\t15" in out and "chr1\t25" in out
