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


def test_a_csi_only_table_is_read(fixtures):
    """pysam asks htslib only for a .tbi; a file published with only a .csi (pea, faba
    bean and lentil gene models, whose chromosomes are too long for TBI) must still
    open."""
    gff = fixtures / "long.gff3"
    gff.write_text("chr1\ttest\tgene\t10\t50\t.\t+\t.\tID=gene9\n")
    pysam.tabix_compress(str(gff), str(gff) + ".gz", force=True)
    pysam.tabix_index(str(gff) + ".gz", preset="gff", csi=True, force=True)
    assert not (fixtures / "long.gff3.gz.tbi").exists()
    assert "ID=gene9" in P._tabix_query({"path": "long.gff3.gz", "region": "chr1:1-100"})
    assert "1 contig(s)" in P._tabix_query({"path": "long.gff3.gz"})


def test_tabix_index_is_write_gated(fixtures):
    # bio_tools wires allow_write into the tool; the handler itself must fail closed
    tools = {t.name: t for t in P.bio_tools(allow_write=False)}
    # tabix_index always classifies as a write
    assert tools["tabix_index"].writes({}) is True


def test_write_capable_tools_are_read_only_exactly_when_writes_are_off():
    """`read_only` becomes the MCP readOnlyHint, which clients use to decide whether to
    ask before each call. Without --allow-write these tools cannot write, so prompting
    for them is noise; with it they can, and must be advertised as such."""
    names = ("samtools", "bcftools", "tabix_index")
    off = {t.name: t for t in P.bio_tools(allow_write=False)}
    on = {t.name: t for t in P.bio_tools(allow_write=True)}
    assert all(off[n].read_only is True for n in names)
    assert all(on[n].read_only is False for n in names)
    # The per-call classifier is unchanged: it is what refuses a write when writes are off.
    assert off["samtools"].writes({"args": ["sort", "-o", "x.bam", "y.bam"]}) is True


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
    assert P._bcftools_list_samples(["-l", "-r", "chr1", "variants.vcf.gz"]) is None
    assert P._bcftools_list_samples(["-l"]) is None


def test_bcftools_query_format_still_dispatches(fixtures):
    """Regression guard: the normal `query -f` path writes through `-o` and must keep
    going to the real dispatcher."""
    out = _bcf(["query", "-f", "%CHROM\t%POS\n", "variants.vcf.gz"])
    assert "chr1\t15" in out and "chr1\t25" in out


# --- attached-value options must not slip past the workspace sandbox ------------------
def test_equals_form_long_option_is_sandboxed(fixtures):
    """`--output=<path>` is a single token starting with '-', so it looks nothing like a
    path; without explicit handling it bypasses the sandbox entirely."""
    assert "outside the project workspace" in _bcf(
        ["view", "--output=/etc/evil.vcf", "variants.vcf.gz"], allow_write=True)
    guarded, err = P._guard_argv(["--output=sub.vcf"])
    assert err is None
    assert guarded[0].startswith("--output=") and guarded[0].endswith("/sub.vcf")
    assert os.path.isabs(guarded[0].split("=", 1)[1])


def test_equals_form_counts_as_a_write(fixtures):
    """Gating must see the equals form too, or it would slip through as a 'read'."""
    assert P._mk_writes(P._BCF_READ)({"args": ["view", "--output-file=x.vcf"]}) is True
    assert "writes are disabled" in _bcf(["view", "--output=x.vcf", "variants.vcf.gz"])


def test_equals_form_url_is_ssrf_checked(fixtures):
    assert "blocked URL" in _sam(["view", "--reference=http://169.254.169.254/r.fa",
                                  "reads.sorted.bam"])


def test_short_attached_path_is_refused_not_guessed(fixtures):
    """`-oout.vcf` hides the path; we refuse rather than split, because bundled boolean
    shorts (`-bS`) are indistinguishable from an attached value."""
    out = _bcf(["view", "-o/etc/evil.vcf", "variants.vcf.gz"], allow_write=True)
    assert "attached to a short option" in out and "-o /etc/evil.vcf" in out
    # a bundled boolean pair must keep working - its tail is not path-like
    assert P._short_attached_path("-bS") is False
    assert P._short_attached_path("-o/tmp/x.vcf") is True
    assert P._short_attached_path("-Oz") is False       # not a path flag at all


# --- the agent's -o is honoured where pysam would clobber it --------------------------
def test_pysam_injects_output_mirrors_pysam(fixtures):
    """The rule we mirror from pysam's MAP_STDOUT_OPTIONS; getting it wrong either
    clobbers the agent's -o or double-writes a file pysam never touched."""
    assert P._pysam_injects_output("bcftools", "view", []) is True
    assert P._pysam_injects_output("bcftools", "query", []) is True
    for sub in ("head", "index", "roh", "stats"):
        assert P._pysam_injects_output("bcftools", sub, []) is False
    assert P._pysam_injects_output("samtools", "view", []) is True
    assert P._pysam_injects_output("samtools", "view", ["-c"]) is False
    assert P._pysam_injects_output("samtools", "sort", []) is False   # honours -o itself


def test_bcftools_output_flag_actually_writes_the_file(fixtures):
    out = _bcf(["view", "-H", "-o", "sub.vcf", "variants.vcf.gz"], allow_write=True)
    assert "wrote" in out and "sub.vcf" in out
    written = (fixtures / "sub.vcf").read_text()
    assert "rs1" in written and written.count("\n") == 2


def test_samtools_view_output_flag_actually_writes_the_file(fixtures):
    """samtools view is injected too — the bug is not bcftools-only."""
    out = _sam(["view", "-o", "out.sam", "reads.sorted.bam"], allow_write=True)
    assert "wrote" in out and "out.sam" in out
    assert "read0" in (fixtures / "out.sam").read_text()


def test_equals_form_output_writes_the_file(fixtures):
    out = _bcf(["view", "-H", "--output=eq.vcf", "variants.vcf.gz"], allow_write=True)
    assert "wrote" in out
    assert "rs1" in (fixtures / "eq.vcf").read_text()


def test_samtools_sort_output_is_left_to_pysam(fixtures):
    """sort is not injected, so we must NOT strip its -o - pysam writes the BAM itself."""
    assert "completed" in _sam(["sort", "-o", "s.bam", "reads.sorted.bam"], allow_write=True)
    assert (fixtures / "s.bam").exists() and (fixtures / "s.bam").stat().st_size > 0


def test_output_flag_without_a_filename_errors(fixtures):
    assert "needs a filename" in _bcf(["view", "-o"], allow_write=True)
    # the equals form with an empty value is caught upstream by the path sandbox
    assert "missing 'path'" in _bcf(["view", "--output=", "variants.vcf.gz"],
                                    allow_write=True)


def test_derived_file_can_be_indexed_and_queried(fixtures):
    """The chain the -O refusal points at: write text, tabix_index it, region-query it.
    This is what keeps binary output unnecessary rather than merely forbidden."""
    assert "wrote" in _bcf(["view", "-o", "derived.vcf", "variants.vcf.gz"],
                           allow_write=True)
    assert "indexed" in P._tabix_index({"path": "derived.vcf", "preset": "vcf"})
    out = P._tabix_query({"path": "derived.vcf.gz", "region": "chr1:20-30"})
    assert "1 record(s)" in out and "chr1\t25" in out


# --- binary/compressed output is refused rather than corrupted ------------------------
def test_binary_output_request_detection():
    assert P._binary_output_request("bcftools", ["-O", "z"]) == "bgzipped VCF"
    assert P._binary_output_request("bcftools", ["-Oz"]) == "bgzipped VCF"
    assert P._binary_output_request("bcftools", ["-Oz6"]) == "bgzipped VCF"
    assert P._binary_output_request("bcftools", ["--output-type=b"]) == "compressed BCF"
    assert P._binary_output_request("bcftools", ["-O", "v"]) == ""      # text VCF is fine
    assert P._binary_output_request("samtools", ["-b"]) == "BAM"
    assert P._binary_output_request("samtools", ["--output-fmt=cram"]) == "CRAM"
    assert P._binary_output_request("samtools", ["-h"]) == ""


def test_binary_output_is_refused_with_a_route(fixtures):
    out = _bcf(["view", "-Oz", "-o", "sub.vcf.gz", "variants.vcf.gz"], allow_write=True)
    assert "cannot emit bgzipped VCF" in out and "tabix_index" in out
    assert not (fixtures / "sub.vcf.gz").exists()


def test_binary_output_refused_even_without_o(fixtures):
    """Without -o the bytes would land in the model's context as a str() repr instead."""
    assert "cannot emit" in _bcf(["view", "-Oz", "variants.vcf.gz"])


def test_samtools_binary_output_points_at_sort(fixtures):
    out = _sam(["view", "-b", "-o", "out.bam", "reads.sorted.bam"], allow_write=True)
    assert "cannot emit BAM" in out and "samtools sort" in out


# --- htslib's ##idx## composite syntax smuggles a second, unchecked path --------------
def test_composite_index_url_is_refused_not_split():
    """`data##idx##index`: the guard used to see one public URL while htslib fetched the
    index from the second one — here, the cloud metadata endpoint."""
    tok = "https://example.org/x.bam##idx##http://169.254.169.254/latest/meta-data/"
    _, err = P._guard_argv(["view", "-c", tok])
    assert err and "##idx##" in err
    assert P._validate_remote(tok)[1]


def test_composite_index_local_path_is_refused(fixtures):
    """The local form realpath-checks as one workspace path, but htslib opens the part
    after ##idx## as an index from anywhere on disk."""
    out = _sam(["view", "-c", "reads.sorted.bam##idx##../outside.bai", "chr1"])
    assert "##idx##" in out and out.startswith("error")
    assert "##idx##" in _sam(["sort", "-o", "s.bam", "reads.sorted.bam##idx##/x.bai"],
                             allow_write=True)
    assert "##idx##" in P._fasta_fetch({"path": "genome.fa##idx##/etc/x.fai",
                                        "region": "chr1:1-5"})


# --- reads are an allowlist: anything else is a write ---------------------------------
@pytest.mark.parametrize("args, flag", [
    (["view", "-q", "30", "-U", "rejects.sam", "reads.sorted.bam"], "-U"),
    (["view", "--unoutput", "rejects.sam", "reads.sorted.bam"], "--unoutput"),
    (["view", "--unout", "rejects.sam", "reads.sorted.bam"], "--unout"),   # getopt abbrev
    (["view", "--out=rejects.sam", "reads.sorted.bam"], "--out"),
    (["view", "--save-counts", "rejects.txt", "reads.sorted.bam"], "--save-counts"),
    (["view", "--write-index", "-c", "reads.sorted.bam"], "--write-index"),
    (["view", "-hU", "rejects.sam", "reads.sorted.bam"], "-U"),             # in a cluster
    (["fastq", "-0", "rejects.fq", "reads.sorted.bam"], "-0"),
    (["fastq", "-s", "rejects", "reads.sorted.bam"], "-s"),
    (["fasta", "--i1", "rejects.fa", "reads.sorted.bam"], "--i1"),
    (["stats", "-S", "RG", "-P", "rejects", "reads.sorted.bam"], "-S"),
    (["depth", "-f", "files.txt"], "-f"),                     # a file of filenames
    (["view", "--input-fmt-option", "reference=/etc/hosts", "reads.sorted.bam"],
     "--input-fmt-option"),
])
def test_output_options_of_read_subcommands_are_writes(fixtures, args, flag):
    """Each of these is a read subcommand writing a file through an option a denylist of
    -o/--output never knew about — with writes off, each used to create its file."""
    out = _sam(args)
    assert "writes are disabled" in out and f"'{flag}'" in out
    assert P._mk_writes(P._SAM_READ)({"args": args}) is True
    assert not any(p.name.startswith("rejects") for p in fixtures.iterdir())


def test_bcftools_output_options_are_writes(fixtures):
    for args in (["view", "-W", "-o", "x.vcf.gz", "variants.vcf.gz"],
                 ["query", "-v", "list.txt", "-f", "%POS\n"],
                 ["csq", "--dump-gff", "x.gff", "variants.vcf.gz"],
                 ["roh", "-e", "GT,samples.txt", "variants.vcf.gz"]):
        assert "writes are disabled" in _bcf(args), args


def test_read_argv_parses_getopt_forms(fixtures):
    """Clusters, attached values and --long=value all still work as reads."""
    assert _sam(["view", "-c", "-q30", "reads.sorted.bam"]).strip() == "5"
    assert _sam(["view", "-cq", "30", "reads.sorted.bam"]).strip() == "5"
    assert _sam(["view", "--count", "--min-MQ=30", "reads.sorted.bam"]).strip() == "5"
    assert "rs1" in _bcf(["view", "-H", "-r", "chr1:1-20", "variants.vcf.gz"])
    assert "rs1" in _bcf(["query", "-f", "%ID\n", "--regions=chr1:1-20",
                          "variants.vcf.gz"])


def test_read_input_file_options_are_sandboxed_even_as_bare_names(fixtures):
    """An input-file option is known to take a file, so its value is sandboxed whatever
    it looks like — attached, bare, or as --long=value."""
    assert "outside the project workspace" in _sam(["view", "-c", "-T/etc/hosts",
                                                    "reads.sorted.bam"])
    assert "outside the project workspace" in _sam(["view", "-c", "--reference=/etc/hosts",
                                                    "reads.sorted.bam"])
    guarded, _, err = P._read_argv(P._SAM_READ, "view", ["-c", "-L", "targets",
                                                         "reads.sorted.bam"])
    assert err is None and guarded[2] == str(fixtures / "targets")
    assert "dotfile" in _sam(["view", "-c", "-L", ".env", "reads.sorted.bam"])


def test_read_format_options_cannot_name_a_file(fixtures):
    assert "{" in _sam(["flagstat", "-O", "json", "reads.sorted.bam"])
    assert "refused format" in _sam(["flagstat", "-O", "json,reference=/etc/hosts",
                                     "reads.sorted.bam"])


# --- the worker process ---------------------------------------------------------------
def test_bare_output_name_lands_in_scratch_not_cwd_or_workspace(fixtures, tmp_path,
                                                                monkeypatch):
    """A bare name the guard cannot recognise as a path used to resolve against the
    server's cwd — outside the workspace, dotfiles included. The worker's cwd is htslib's
    scratch directory, so such a name can only ever land there."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    out = _sam(["view", "-q", "30", "-U", "bare", "reads.sorted.bam"], allow_write=True)
    assert "read3" in out
    assert not (elsewhere / "bare").exists() and not (fixtures / "bare").exists()
    assert os.path.exists(os.path.join(P._scratch_dir(), "local", "bare"))


def test_fastq_output_is_captured_not_leaked(fixtures, capfd):
    """samtools fastq writes to fd 1 directly. In-process that went to the server's own
    stdout — the stdio transport's JSON-RPC stream — and the tool returned nothing."""
    out = _sam(["fastq", "reads.sorted.bam"])
    assert out.startswith("@read0") and "ACGTACGTAC" in out
    assert "ACGTACGTAC" not in capfd.readouterr().out


def test_a_stuck_dispatch_is_killed_and_does_not_block_others(fixtures, monkeypatch):
    """A FIFO nobody writes stands in for a remote host that never answers."""
    import threading
    monkeypatch.setattr(P, "TIMEOUT", 2)
    os.mkfifo(fixtures / "stuck.sam")
    result = {}
    stuck = threading.Thread(target=lambda: result.setdefault(
        "out", _sam(["view", "-c", "stuck.sam"])))
    stuck.start()
    assert _sam(["view", "-c", "reads.sorted.bam"]).strip() == "5"   # not serialized
    stuck.join(10)
    assert "ran past the 2s limit" in result["out"]


def test_file_size_limit_stops_a_runaway_write(fixtures, monkeypatch):
    monkeypatch.setattr(P, "READ_FILE_BYTES", 200)
    out = _sam(["view", "-h", "reads.sorted.bam"])
    assert "200-byte limit" in out and "a read only needs" in out
    monkeypatch.setattr(P, "MAX_FILE_BYTES", 200)
    out = _bcf(["view", "-o", "big.vcf", "variants.vcf.gz"], allow_write=True)
    assert "200-byte limit" in out


def test_allowed_urls_match_by_host_not_string_prefix():
    allow = ["https://data.legumeinfo.org"]
    assert P._url_allowed("https://data.legumeinfo.org/Glycine/x.bam", allow)
    assert not P._url_allowed("https://data.legumeinfo.org.evil.example/x.bam", allow)
    assert not P._url_allowed("https://data.legumeinfo.org@evil.example/x.bam", allow)
    assert not P._url_allowed("http://data.legumeinfo.org/x.bam", allow)
    assert P._url_allowed("https://h.example/a/b.bam", ["https://h.example/a/"])
    assert not P._url_allowed("https://h.example/c/b.bam", ["https://h.example/a/"])


# --- remote indexes are cached per URL, not per filename -------------------------------
LIS = "https://data.legumeinfo.org/Glycine/max/diversity/x/glyma.SNPs.vcf.gz"
EVIL = "https://evil.example/glyma.SNPs.vcf.gz"


def test_same_filename_on_two_hosts_never_shares_an_index():
    """htslib reuses a cached index by filename alone. In one shared directory, whoever
    read `evil.example/<name>` first would decide the index for the real LIS file."""
    lis = P._worker_cwd({"argv": ["view", "-H", LIS, "chr1:1-10"]})
    evil = P._worker_cwd({"argv": ["view", "-H", EVIL, "chr1:1-10"]})
    assert lis != evil
    open(os.path.join(evil, "glyma.SNPs.vcf.gz.tbi"), "w").close()
    assert not os.path.exists(os.path.join(lis, "glyma.SNPs.vcf.gz.tbi"))


def test_one_url_keeps_its_cache_across_calls_and_tools():
    """The cache still works: every call on the same URL lands in the same directory,
    whether it is a dispatch, a helper, or a URL attached to an option."""
    a = P._worker_cwd({"argv": ["view", "-H", LIS]})
    assert P._worker_cwd({"op": "tabix", "path": LIS}) == a
    ref = "https://data.legumeinfo.org/ref.fa"
    both = P._worker_cwd({"argv": ["mpileup", f"--fasta-ref={ref}", LIS]})
    assert both == P._worker_cwd({"argv": ["mpileup", "-f", ref, LIS]}) != a
    assert P._worker_cwd({"argv": ["view", "local.vcf.gz"]}).endswith(os.sep + "local")


def test_a_cached_index_expires(monkeypatch):
    """htslib never revalidates; a file republished upstream must not keep its old
    index forever."""
    cwd = P._worker_cwd({"path": LIS})
    stale, fresh = os.path.join(cwd, "old.tbi"), os.path.join(cwd, "new.tbi")
    for name in (stale, fresh):
        open(name, "w").close()
    os.utime(stale, (0, 0))
    P._worker_cwd({"path": LIS})
    assert not os.path.exists(stale) and os.path.exists(fresh)


# --- outputs never go to a URL ---------------------------------------------------------
@pytest.mark.parametrize("args", [
    ["sort", "-o", "https://93.184.216.34/x.bam", "reads.sorted.bam"],
    ["sort", "--output=https://93.184.216.34/x.bam", "reads.sorted.bam"],
    ["view", "-o", "http://93.184.216.34/x.sam", "reads.sorted.bam"],
])
def test_an_output_url_is_refused_in_write_mode(fixtures, args):
    """htslib opens an output URL with CURLOPT_UPLOAD: a PUT of data built from
    workspace files, to a host the caller chose."""
    out = _sam(args, allow_write=True)
    assert "refused output" in out and "upload" in out


def test_a_url_input_beside_a_workspace_output_still_works(fixtures):
    guarded, err = P._guard_argv(["-o", "sub.vcf", "https://93.184.216.34/v.vcf.gz"])
    assert err is None and guarded[-1] == "https://93.184.216.34/v.vcf.gz"
