#!/usr/bin/env python3
"""Native bioinformatics tools — drive samtools/bcftools/htslib through pysam.

Rather than reimplement a handful of fixed operations, this exposes the **whole**
samtools and bcftools command suites as two general dispatcher tools (`samtools`,
`bcftools`), the same argv-list convention `tools_native.ncbi_datasets`/`edirect` use for
the NCBI CLIs. Between them they cover the bulk of everyday genomics work — view, sort,
index, depth, coverage, flagstat, idxstats, stats, consensus, mpileup, call, norm, query,
annotate, merge, markdup, faidx, … — so new capability is a new argv, not new code. Two
guaranteed-read-only Python-API helpers (`fasta_fetch`, `tabix_query`) cover sequence
extraction and arbitrary GFF/BED tabix queries ergonomically, and `tabix_index` builds an
index (a write).

Read vs write, and the permission model:
- The read-only helpers are always available.
- The dispatchers classify each call against an ALLOWLIST: a read is a subcommand in
  `_SAM_READ`/`_BCF_READ` using only the options listed for it there, each of which was
  checked against the bundled CLI source to have no filesystem side effect. Anything else
  — another subcommand, an output option (`-o`, `view -U`, `fastq -1`, `--write-index`),
  an option this table does not know, a getopt abbreviation of one it does — is a
  **write**, and runs only when the server was started with `legumista mcp
  --allow-write`. The MCP server has no permission gate, so that flag is the sole control
  and the classifier is enforced by the tool's own guard — writes fail closed without it.
  A denylist of output flags cannot be made complete over two CLIs this size; an
  allowlist fails closed when either CLI grows a new option.

Safety, matching tools_native.py: local path arguments are confined to the workspace by
the shared `_sandbox_path` guard and rewritten to absolute; http(s) URL arguments pass
the native SSRF guard (other schemes refused); htslib's `##idx##` composite syntax, which
smuggles a second, unchecked path or URL inside one token, is refused outright. Each
dispatch runs in a child process (`_pysam_worker.py`) with a wall-clock timeout and a
per-file size limit, in htslib's own scratch directory rather than the workspace or the
server's cwd — so a bare filename can never reach a file the sandbox did not vet, and a
stuck call is killed rather than stalling every other one. The read-only helpers run
there too. Workers reach the network only through the egress proxy (egress_proxy.py),
which refuses any non-public destination on every connection, redirect hops included —
so any public host is readable and no private one is. pysam is a dependency of
legumista, imported lazily (only when a genomics tool actually runs) so the heavy htslib
extension isn't loaded by sessions that never touch a genomics tool.

Coordinates: `region` in the helpers is samtools-style — `seqid`, `seqid:start`, or
`seqid:start-end`, 1-based inclusive — converted to pysam's 0-based half-open internally.
"""
import asyncio
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

from . import egress_proxy
from .tool import Tool
from .tools_native import MAX_CHARS, BlockedURLError, _cap, _sandbox_path, _validate_url

MAX_RECORDS = int(os.environ.get("LEGUMISTA_PYSAM_MAX_RECORDS", "200"))
MAX_SEQ = int(os.environ.get("LEGUMISTA_PYSAM_MAX_SEQ", "100000"))

# --- dispatch workers ----------------------------------------------------------------
# Every samtools/bcftools call runs in a killable child process (see _pysam_worker.py).
#   TIMEOUT          wall-clock seconds before a dispatch is killed
#   WORKERS          dispatches that may run at once; further calls queue (up to TIMEOUT)
#   MAX_FILE_BYTES   the largest file one WRITE dispatch may produce (0: no limit)
# A read dispatch writes nothing but its captured stdout and htslib's cached copy of a
# remote index, so it gets a far smaller ceiling: only MAX_CHARS of that stdout is ever
# shown, and the ceiling is what stops `view` of a whole remote BAM filling the disk.
TIMEOUT = int(os.environ.get("LEGUMISTA_PYSAM_TIMEOUT", "300"))
WORKERS = max(1, int(os.environ.get("LEGUMISTA_PYSAM_WORKERS", "4")))
MAX_FILE_BYTES = int(os.environ.get("LEGUMISTA_PYSAM_MAX_FILE_BYTES", str(4 << 30)))
READ_FILE_BYTES = min(64 << 20, MAX_FILE_BYTES or 64 << 20)
# Seconds a downloaded remote index is trusted before htslib fetches it again.
INDEX_TTL = int(os.environ.get("LEGUMISTA_INDEX_TTL", "3600"))
_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_pysam_worker.py")
_WORKER_SLOTS = threading.BoundedSemaphore(WORKERS)
_SCRATCH = {"dir": ""}

# tabix_index runs in-process (pysam's Python API, not the CLI dispatcher) and bgzips
# its input in place, so two concurrent calls on one file must not interleave.
_INDEX_LOCK = threading.Lock()


# pysam's manylinux wheels vendor their own libcurl + OpenSSL (see pysam.libs/), built in
# a RHEL-based container, so their compiled-in CA locations are /etc/pki/tls/... . Those
# paths don't exist on Debian/Ubuntu/Alpine/Arch/macOS, so every https:// read through
# htslib dies with "Libcurl reported error 77 (Problem with the SSL CA cert)" — which is
# every remote genome/annotation/VCF this toolset is built to stream. libcurl reads
# CURL_CA_BUNDLE when it creates an easy handle, not at load time, so pointing it at a
# bundle that does exist fixes it in-process, before the first remote open.
_CA_ENV = "CURL_CA_BUNDLE"
_SYSTEM_CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",   # Debian/Ubuntu/Alpine/Arch
    "/etc/pki/tls/certs/ca-bundle.crt",     # RHEL/Fedora/CentOS
    "/etc/ssl/ca-bundle.pem",               # openSUSE
    "/etc/ssl/cert.pem",                    # macOS/BSD
)


def _ensure_ca_bundle() -> None:
    """Point htslib's bundled libcurl at a CA bundle that exists on this machine.

    An operator-set CURL_CA_BUNDLE always wins (that is how a corporate TLS-inspecting
    proxy is configured); otherwise prefer the system trust store over certifi's, so a
    site's own added roots keep working. Silently does nothing if neither is available —
    a remote read then fails with the usual htslib error rather than a new one."""
    if os.environ.get(_CA_ENV):
        return
    for path in _SYSTEM_CA_BUNDLES:
        if os.path.exists(path):
            os.environ[_CA_ENV] = path
            return
    try:
        import certifi
        os.environ[_CA_ENV] = certifi.where()
    except Exception:  # noqa: BLE001 - certifi absent or unreadable; not fatal
        pass


def _pysam():
    """Import pysam lazily; return (module, None) or (None, error_text)."""
    try:
        import pysam
    except ModuleNotFoundError:
        return None, ("error: could not import 'pysam' (htslib bindings). It ships as a "
                      "dependency of legumista — reinstalling the package should restore it.")
    _ensure_ca_bundle()
    return pysam, None


# --- argument sandboxing (paths -> workspace; URLs -> SSRF-checked) ------------------
def _is_url(tok: str) -> bool:
    return bool(re.match(r"(?i)^[a-z][a-z0-9+.-]*://", tok or ""))


# htslib reads `data.bam##idx##index.bai` as "this data file, with THAT index". The
# second half is opened by htslib itself, after every check here has looked at the token
# as one string: a public URL can name a metadata-endpoint index, and a workspace path a
# local index anywhere on disk. htslib finds a sibling index without help, so the syntax
# is refused rather than split and checked.
_IDX_DELIM = "##idx##"


def _refuse_composite(tok: str):
    if _IDX_DELIM in (tok or ""):
        return (f"error: refused {tok!r} — htslib's '{_IDX_DELIM}' syntax names a second "
                "file the sandbox cannot check. Pass the data file alone; its index "
                "(.bai/.csi/.crai/.tbi) is found beside it automatically.")
    return None


def _url_allowed(url: str, prefixes: list) -> bool:
    """Match `url` against LEGUMISTA_PYSAM_ALLOWED_URLS entries by parsed scheme, host and
    port, then path prefix. A plain string prefix is not enough: the entry
    `https://data.legumeinfo.org` would also admit `https://data.legumeinfo.org.evil.com/`.
    """
    u = urllib.parse.urlparse(url)
    for prefix in prefixes:
        p = urllib.parse.urlparse(prefix)
        try:
            same_origin = (u.scheme.lower() == p.scheme.lower()
                           and (u.hostname or "") == (p.hostname or "")
                           and u.port == p.port)
        except ValueError:                      # a malformed port in either URL
            continue
        if same_origin and u.path.startswith(p.path):
            return True
    return False


def _validate_remote(url: str):
    """Vet an http(s) URL argument (SSRF guard + optional allowlist). Returns (url, None)
    or (None, error). Non-http(s) schemes are refused — htslib does its own I/O, so we
    only permit transports we can check up front."""
    err = _refuse_composite(url)
    if err:
        return None, err
    if not re.match(r"(?i)^https?://", url):
        return None, (f"error: refused URL scheme {url.split('://', 1)[0]!r} — only "
                      "http(s) URLs and local workspace files are allowed")
    allow = [p.strip() for p in os.environ.get("LEGUMISTA_PYSAM_ALLOWED_URLS", "").split(",")
             if p.strip()]
    if allow and not _url_allowed(url, allow):
        return None, "error: refused — URL is not in LEGUMISTA_PYSAM_ALLOWED_URLS allowlist"
    try:
        _validate_url(url)
    except BlockedURLError as e:
        return None, f"error: blocked URL: {e}"
    return url, None


def _resolve_source(src: str):
    """Resolve a helper tool's single file argument: a workspace path or an http(s) URL.
    Returns (target, None) or (None, error_text)."""
    src = (src or "").strip()
    if not src:
        return None, "error: missing file 'path' (a workspace file or an http(s) URL)"
    err = _refuse_composite(src)
    if err:
        return None, err
    if _is_url(src):
        return _validate_remote(src)
    return _sandbox_path(src)


# Genomics extensions + flags that take a path value — used to spot path-like tokens in a
# dispatcher argv so we can confine them to the workspace (regions, flags, numbers, and
# format words like "BAM"/"z" are left untouched).
_GENOMIC_EXT = (".bam", ".cram", ".sam", ".bai", ".csi", ".crai", ".fa", ".fasta",
                ".fai", ".gzi", ".vcf", ".bcf", ".tbi", ".bed", ".gff", ".gff3", ".gtf",
                ".gz", ".dict", ".fq", ".fastq", ".txt", ".tsv")
_PATH_FLAGS = {"-o", "--output", "--output-file", "-T", "--reference", "--fasta-ref",
               "-f", "-R", "--regions-file", "-t", "--targets-file", "-S",
               "--samples-file", "-b", "-L", "--bed"}


# getopt also accepts a value glued to its option — `--output=path` and `-opath`. Neither
# form looks like a path to `_looks_like_path` (both start with '-'), so without special
# handling they slip past the sandbox entirely.
_ATTACHED_LONG_RE = re.compile(r"^(--[A-Za-z0-9][A-Za-z0-9-]*)=(.*)$")


def _looks_like_path(tok: str) -> bool:
    return (not tok.startswith("-")
            and (os.sep in tok or tok.lower().endswith(_GENOMIC_EXT)))


def _short_attached_path(tok: str) -> bool:
    """True for a short path-option with its value glued on (`-oout.vcf`, `-T/ref.fa`).

    We refuse these rather than split them: bundled boolean shorts are indistinguishable
    (`samtools view -bS` is `-b -S`, not `-b S`), so splitting would corrupt valid argv.
    Only tokens whose tail actually looks like a path qualify, keeping `-bS` untouched."""
    if len(tok) <= 2 or not tok.startswith("-") or tok.startswith("--"):
        return False
    if tok[:2] not in _PATH_FLAGS:
        return False
    tail = tok[2:]
    return os.sep in tail or tail.lower().endswith(_GENOMIC_EXT)


def _refuse_remote_output(prev: str, tok: str):
    """A URL as the target of -o/--output/--output-file. htslib opens an output URL for
    writing with CURLOPT_UPLOAD — an HTTP PUT of the result, which in write mode can be
    built from any workspace file. Outputs belong in the workspace.

    Only these three options are known to mean "output" in every subcommand of both
    CLIs, so only they are checked here; an upload through some other output option
    (`fastq -1`, `index`'s positional output, …) is bounded instead by the egress
    proxy's per-connection send budget."""
    attached = _ATTACHED_LONG_RE.match(tok)
    value = (attached.group(2) if attached and attached.group(1) in _OUTPUT_FLAGS
             else tok if prev in _OUTPUT_FLAGS else "")
    if value and _is_url(value):
        return (f"error: refused output {value!r} — htslib would upload the result "
                "there. Write to a workspace file instead.")
    return None


def _guard_argv(argv: list):
    """Confine every path/URL token in a WRITE dispatcher argv. Path tokens are workspace-
    sandboxed and rewritten to absolute realpaths; URL tokens pass the SSRF guard and are
    kept verbatim; everything else passes through, and resolves (if the CLI opens it at
    all) inside the worker's scratch directory. Returns (new_argv, None) or
    (None, error_text).

    This is the heuristic guard for the operator-enabled write mode, where the argv is
    arbitrary. A read is parsed exactly against its allowlist instead (`_read_argv`)."""
    out, expect_path, prev = [], False, ""
    for tok in argv:
        err = _refuse_composite(tok) or _refuse_remote_output(prev, tok)
        if err:
            return None, err
        prev = tok
        attached = _ATTACHED_LONG_RE.match(tok)
        if attached and attached.group(1) in _PATH_FLAGS:
            flag, value = attached.group(1), attached.group(2)
            if _is_url(value):
                resolved, err = _validate_remote(value)
            else:
                resolved, err = _sandbox_path(value)
            if err:
                return None, err
            out.append(f"{flag}={resolved}")
            expect_path = False
            continue
        if _short_attached_path(tok):
            return None, (f"error: refused {tok!r} — a value attached to a short option "
                          f"hides the path from the workspace sandbox. Pass it as two "
                          f"tokens instead: {tok[:2]} {tok[2:]}")
        if _is_url(tok):
            url, err = _validate_remote(tok)
            if err:
                return None, err
            out.append(url)
        elif (expect_path and not tok.startswith("-")) or _looks_like_path(tok):
            rp, err = _sandbox_path(tok)
            if err:
                return None, err
            out.append(rp)
        else:
            out.append(tok)
        expect_path = tok in _PATH_FLAGS
    return out, None


# --- region parsing / error mapping (shared by the read helpers) ---------------------
_SPAN_RE = re.compile(r"^([\d,]+)(?:-([\d,]+))?$")


def _parse_region(region: str):
    region = (region or "").strip()
    if not region:
        return None, None, None, "error: missing 'region' (e.g. 'glyma.Wm82.gnm4.Gm12:1000-2000')"
    contig, sep, span = region.rpartition(":")
    if not sep:
        return region, None, None, None
    m = _SPAN_RE.match(span)
    if not m:
        return region, None, None, None
    lo = int(m.group(1).replace(",", ""))
    hi = int(m.group(2).replace(",", "")) if m.group(2) else lo
    start = lo - 1
    if start < 0 or hi < lo:
        return None, None, None, (f"error: bad coordinates in {region!r} "
                                  "(expected 1-based 'seqid:start-end', start<=end)")
    return contig, start, hi, None


def _open_err(e: Exception, target: str) -> str:
    kind = type(e).__name__
    msg = str(e).strip() or kind
    low = msg.lower()
    if isinstance(e, (OSError, IOError)):
        return (f"error: could not open {target!r}: {msg}. Check the path/URL and that the "
                "index is present (.fai/.bai/.csi/.tbi — build with `samtools faidx|index`, "
                "`tabix_index`, or `bcftools index`).")
    if isinstance(e, ValueError):
        if "index" in low:
            return f"error: {msg}. Build the sibling index first (samtools/bcftools/tabix_index)."
        return (f"error: {msg}. The contig may not exist in this file, or the region is out "
                "of range — list contigs with `samtools idxstats`/`bcftools view -h`.")
    return f"error: {kind}: {msg}"


# --- general dispatchers: samtools / bcftools ---------------------------------------
# The read allowlist: per subcommand, every option a read may use, taken from the
# getopt tables of the samtools/bcftools sources pysam bundles. Each entry is a short
# letter or `--long-name`, suffixed with what follows it:
#     (nothing)  no argument               =  a value, passed through
#     <          an INPUT file: workspace-sandboxed or SSRF-checked
#     %          an output format name (no `,opt=val`: those can name a reference file)
#     ?          an optional argument, attached only (getopt `::`)
# Deliberately absent, so they classify as writes: every output-file option (-o,
# --output, view -U/--unoutput/--save-counts, fasta/fastq -0/-1/-2/-s/--i1/--i2,
# stats -S/-P, csq --dump-gff, --write-index/-W); options whose argument embeds a path
# (`--tag-file STR:FILE`, `--input-fmt-option reference=FILE`, roh `-e TAG,FILE`); and
# files-of-filenames (depth -f, coverage -b, samples -F, query -v), whose contents name
# further files the sandbox never sees.
_SAM_FLAGS = "--verbosity= --threads= @="
_FASTX = ("n i N O t U f= F= G= c= T= v= d= --no-sc --no-sc-bkp --UMI --require-flags= "
          "--excl-flags= --exclude-flags= --rf= --incl-flags= --include-flags= --if= --IF= "
          "--index-format= --barcode-tag= --quality-tag= --tag= --sc-aux= --UMI-tag= "
          f"--reference< {_SAM_FLAGS}")
_BCF_REGIONS = ("r= t= R< T< --regions= --targets= --regions-overlap= --targets-overlap= "
                "--regions-file< --targets-file< --verbosity=")
_SAM_SPEC = {
    "view": "S b B c C h 1 H u M X p P n q= f= F= G= l= r= s= m= x= e= z= d= t< T< R< "
            "N< L< O% --bam --count --cram --customised-index --customized-index "
            "--excl-no-read-group --excl-no-readgroup --exclude-no-read-group "
            "--exclude-no-readgroup --fast --fetch-pairs --header-only --help --no-header "
            "--no-PG --remove-B --uncompressed --unmap --use-index --with-header "
            "--add-flags= --excl-flags= --exclude-flags= --expr= --expression= "
            "--incl-flags= --include-flags= --rf= --keep-tag= --library= --min-mapq= "
            "--min-MQ= --min-mq= --min-qlen= --read-group= --readgroup= --remove-flags= "
            "--remove-tag= --require-flags= --subsample= --subsample-seed= --tag= "
            "--sanitize= --fai-reference< --QNAME-file< --qname-file< --read-group-file< "
            "--readgroup-file< --region-file< --regions-file< --target-file< "
            f"--targets-file< --reference< --output-fmt% {_SAM_FLAGS}",
    "head": f"h= n= T< --headers= --records= --reference< {_SAM_FLAGS}",
    "flagstat": f"O% --output-fmt% {_SAM_FLAGS}",
    "idxstats": f"X {_SAM_FLAGS}",
    "stats": "? h d s X x p c= l= i= m= q= f= F= g= I= r< t< --help --remove-dups --sam "
             "--sparse --remove-overlaps --ref-stats --coverage= --read-length= "
             "--insert-size= --most-inserts= --trim-quality= --required-flag= "
             "--filtering-flag= --id= --GC-depth= --cov-threshold= --ref-stats-chunk= "
             f"--ref-seq< --target-regions< --reference< {_SAM_FLAGS}",
    "depth": "J H a X s q= Q= d= m= l= g= G= r= b< --min-MQ= --min-mq= --min-BQ= "
             "--min-bq= --excl-flags= --incl-flags= --require-flags= --reference< "
             f"{_SAM_FLAGS}",
    "coverage": "A h H m D l= q= Q= w= r= d= --rf= --ff= --incl-flags= --excl-flags= "
                "--min-read-len= --min-MQ= --min-mq= --min-BQ= --min-bq= --n-bins= "
                "--region= --depth= --min-depth= --histogram --ascii --plot-depth "
                "--no-header --help --reference< --verbosity=",
    "bedcov": "X j H c Q= g= G= d= --min-MQ= --min-mq= --max-depth= --reference< "
              "--verbosity=",
    "quickcheck": "v q u",
    "dict": "? A h H a= s= u= l< --help --no-header --alias --alternative-name "
            "--assembly= --species= --uri= --alt<",
    "consensus": "q 5 a A p d= c= H= r= f= C= l= m= X= Z= t< T< --use-qual --no-use-qual "
                 "--adj-qual --no-adj-qual --use-MQ --no-use-MQ --adj-MQ --no-adj-MQ "
                 "--ambig --het-only --mark-ins --homopoly-fix --NM-halo= --SC-cost= "
                 "--scale-MQ= --low-MQ= --high-MQ= --min-depth= --call-fract= "
                 "--het-fract= --region= --format= --cutoff= --line-len= --default-qual= "
                 "--show-del= --show-ins= --incl-flags= --rf= --excl-flags= --ff= "
                 "--min-MQ= --min-BQ= --P-het= --P-indel= --het-scale= --mode= "
                 "--homopoly-score= --homopoly-redux= --config= --ref-qual= --block-size= "
                 f"--qual-calibration< --reference< --output-fmt% {_SAM_FLAGS}",
    "fasta": _FASTX,
    "fastq": _FASTX,
    "samples": "? h i X T= f<",
    "ampliconstats": "? h s S f= F= m= d= a= l= t= c= b= D= --help --use-sample-name "
                     "--single-ref --flag-require= --flag-filter= --min-depth= "
                     "--pos-margin= --max-amplicons= --max-amplicon-length= "
                     "--tlen-adjust= --tcoord-min-count= --tcoord-bin= --depth-bin= "
                     f"--reference< {_SAM_FLAGS}",
    "cram_size": "v e --verbose --encodings --verbosity=",
    "checksum": "c P C M O a v q T m B f= F= t= b= z= N= --no-rev-comp --in-order "
                "--check-pos --check-cigar --check-mate --show-qc --verbose --all --tabs "
                "--merge --bamseqchksum --exclude-flags= --require-flags= --flag-mask= "
                f"--tags= --count= --sanitize= {_SAM_FLAGS}",
}
_BCF_SPEC = {
    "view": "G k n a A u U h H I x X p P l= O= s= f= v= V= m= M= c= C= i= e= q= Q= g= "
            "S< --header-only --no-header --with-header --trim-alt-alleles "
            "--trim-unseen-allele --no-update --drop-genotypes --private --exclude-private "
            "--uncalled --exclude-uncalled --known --novel --force-samples --phased "
            "--exclude-phased --no-version --genotype= --compression-level= --threads= "
            "--exclude= --include= --apply-filters= --min-alleles= --max-alleles= "
            "--samples= --output-type= --types= --exclude-types= --min-ac= --max-ac= "
            f"--min-af= --max-af= --samples-file< {_BCF_REGIONS}",
    "query": "h l H u N F= f= a= s= c= i= e= S< --help --list-samples --force-samples "
             "--print-header --allow-undef-tags --disable-automatic-newline= --include= "
             "--exclude= --print-filtered= --format= --annots= --samples= --collapse= "
             f"--samples-file< {_BCF_REGIONS}",
    "stats": "h 1 I v? c= e= s= d= i= f= u= S< F< E< --1st-allele-only --help "
             "--split-by-ID --verbose? --af-tag= --include= --exclude= --collapse= "
             "--depth= --apply-filters= --samples= --user-tstv= --threads= "
             f"--samples-file< --fasta-ref< --exons< {_BCF_REGIONS.replace(' --verbosity=', '')} "
             "--verbosity?",
    "head": "h= n= s= v= --headers= --records= --samples= --verbosity=",
    "roh": "h ? I i H= a= s= M= G= V= b= O= v= S< m< --include-noalt --ignore-homref "
           "--skip-indels --AF-tag= --AF-dflt= --include= --exclude= --buffer-size= "
           "--output-type= --GTs-only= --samples= --hw-to-az= --az-to-hw= "
           "--viterbi-training= --rec-rate= --threads= --AF-file< --samples-file< "
           f"--genetic-map< {_BCF_REGIONS}",
    "csq": "? h q l b i= e= O= s= p= c= C= n= B= v= f< g< S< --force --help "
           "--brief-predictions --local-csq --quiet --no-version --genetic-code= "
           "--threads= --ncsq= --trim-protein-seq= --custom-tag= --include= --exclude= "
           "--output-type= --phase= --verbose= --samples= --gff-annot< --fasta-ref< "
           f"--samples-file< {_BCF_REGIONS}",
}


def _parse_spec(text: str):
    """'q= T< --count' -> ({'q': '=', 'T': '<'}, {'count': ''})."""
    shorts, longs = {}, {}
    for entry in text.split():
        if entry.startswith("--"):
            name = entry[2:]
            kind = name[-1] if name[-1] in "=<%?" else ""
            longs[name[:-1] if kind else name] = kind
        else:
            shorts[entry[0]] = entry[1:]
    return shorts, longs


_SAM_READ = {sub: _parse_spec(text) for sub, text in _SAM_SPEC.items()}
_BCF_READ = {sub: _parse_spec(text) for sub, text in _BCF_SPEC.items()}


def _subcommand(args) -> str:
    argv = args.get("args") or []
    return str(argv[0]).strip().lower().replace("-", "_") if argv else ""


def _guard_positional(tok: str):
    """A read's positional argument: an input file, a URL, or a region."""
    if _is_url(tok):
        return _validate_remote(tok)
    if _looks_like_path(tok):
        return _sandbox_path(tok)
    return tok, None        # a region, or a bare name the worker's scratch cwd defuses


def _guard_value(kind: str, value: str):
    if kind == "<":
        # An input-file option is sandboxed even as a bare name: we know it is a file.
        return _validate_remote(value) if _is_url(value) else _sandbox_path(value)
    if kind == "%" and ("," in value or "=" in value):
        return None, (f"error: refused format {value!r} — format options (`,opt=val`) "
                      "can name a reference file; pass the bare format name.")
    return value, None


def _read_argv(readspecs: dict, sub: str, argv: list, guard: bool = True):
    """Parse a dispatcher argv as a READ of `sub`, getopt-style (short clusters, attached
    values, `--long=value`, `--`).

    Returns (argv, not_read, error):
      (guarded, None, None)  a read; input files sandboxed (when `guard`)
      (None, why, None)      not a read — `why` names the subcommand or option
      (None, None, err)      a read, but an argument failed the sandbox/SSRF guard

    Long options must be spelled in full: getopt would also accept an abbreviation
    (`--out` for `--output`), so anything that is not an exact allowlisted name is
    treated as unknown, and therefore as a write."""
    if sub not in readspecs:
        return None, "", None
    shorts, longs = readspecs[sub]
    check = _guard_value if guard else (lambda _kind, v: (v, None))
    out, i, positional_only = [], 0, False
    while i < len(argv):
        tok = argv[i]
        i += 1
        if positional_only or tok == "-" or not tok.startswith("-"):
            g, err = _guard_positional(tok) if guard else (tok, None)
            if err:
                return None, None, err
            out.append(g)
            continue
        if tok == "--":
            positional_only = True
            out.append(tok)
            continue
        if tok.startswith("--"):
            name, eq, attached = tok[2:].partition("=")
            kind = longs.get(name)
            if kind is None or (kind == "" and eq):
                return None, f"--{name}", None
            if kind in ("", "?"):            # no value, or an optional attached one
                out.append(tok)
                continue
            if not eq and i >= len(argv):
                out.append(tok)              # missing value: the CLI reports it
                continue
            value = attached if eq else argv[i]
            i += 0 if eq else 1
            g, err = check(kind, value)
            if err:
                return None, None, err
            out.extend([f"--{name}={g}"] if eq else [tok, g])
            continue
        # A cluster of short options: `-bh`, `-q30`, `-Hc`, `-T` `ref.fa`.
        for j in range(1, len(tok)):
            kind = shorts.get(tok[j])
            if kind is None:
                return None, f"-{tok[j]}", None
            if kind == "":
                continue
            prefix, rest = tok[:j + 1], tok[j + 1:]
            if kind == "?" or (not rest and i >= len(argv)):
                out.append(tok)
            elif rest:
                g, err = check(kind, rest)
                if err:
                    return None, None, err
                out.append(prefix + g)
            else:
                g, err = check(kind, argv[i])
                i += 1
                if err:
                    return None, None, err
                out.extend([prefix, g])
            break
        else:
            out.append(tok)                  # every letter was a plain flag
    return out, None, None


def _mk_writes(readspecs):
    """A per-call write classifier for a dispatcher whose reads are `readspecs`."""
    def writes(args) -> bool:
        argv = [str(a) for a in (args.get("args") or [])]
        if not argv:
            return False
        return _read_argv(readspecs, _subcommand(args), argv[1:], guard=False)[1] is not None
    return writes


_OUTPUT_FLAGS = ("-o", "--output", "--output-file")


# `bcftools query -l` prints sample names straight to the process's stdout, bypassing the
# output stream that `-o` controls. To capture output, pysam's dispatcher appends
# `-o <tmpfile>` to the argv and sends the real stdout to /dev/null — so the sample list
# is discarded and the dispatcher returns "" with exit status 0: a silent empty answer.
# Serve the listing from the VariantFile header instead; same answer, no CLI text to parse.
_LIST_SAMPLES_FLAGS = ("-l", "--list-samples")


def _bcftools_list_samples(argv):
    """Answer `bcftools query -l <file>` from the header. Returns the text, or None if
    this argv is not a plain sample listing (combined with other options), in which case
    the caller runs the normal dispatcher rather than guessing at the intent."""
    rest = [t for t in argv if t not in _LIST_SAMPLES_FLAGS]
    if len(rest) != 1 or rest[0].startswith("-"):
        return None
    res = _run_worker({"op": "samples", "path": rest[0]}, READ_FILE_BYTES)
    if res.get("error"):
        return _worker_error(res, "bcftools query -l", rest[0], tool="bcftools")
    samples = res["samples"]
    if not samples:
        return "(no samples declared in this VCF/BCF header)"
    return _cap(f"{len(samples)} sample(s):\n" + "\n".join(samples))


# --- honouring the agent's -o -------------------------------------------------------
# To capture a dispatcher's output, pysam appends `-o <tmpfile>` to the argv and points
# the real stdout at /dev/null (libcutils.pyx `_pysam_dispatch`). getopt takes the LAST
# -o, so for the subcommands below the agent's own -o is silently overridden: the file is
# never created, the content comes back as the tool result, and the exit status is 0.
# We mirror pysam's rule, strip the agent's -o before dispatch, and write the captured
# output ourselves. Subcommands pysam leaves alone (samtools sort, bcftools index, …)
# honour -o natively and must not be touched.
_BCF_NO_INJECT = ("head", "index", "roh", "stats")
_BCF_BINARY_TYPES = {"b": "compressed BCF", "u": "uncompressed BCF", "z": "bgzipped VCF"}
_SAM_BINARY_FMTS = {"bam": "BAM", "cram": "CRAM"}


def _pysam_injects_output(module_name: str, sub: str, argv: list) -> bool:
    """Does pysam's stdout capture rewrite this call's -o? Mirrors MAP_STDOUT_OPTIONS."""
    if module_name == "bcftools":
        return sub not in _BCF_NO_INJECT
    if sub in ("mpileup", "depad"):
        return True
    if sub == "view":
        return "-c" not in argv          # pysam exempts `samtools view -c` (counts only)
    return False


def _binary_output_request(module_name: str, argv: list) -> str:
    """Name the binary/compressed output format this argv asks for, or "" if it is text.

    Captured output reaches us as text, so a binary format would have to round-trip
    through str() to reach the file (or the model's context) — silent corruption. These
    are refused with a route that produces the same artifact safely."""
    if module_name == "bcftools":
        for i, tok in enumerate(argv):
            value = ""
            if tok in ("-O", "--output-type"):
                value = argv[i + 1] if i + 1 < len(argv) else ""
            elif tok.startswith("--output-type="):
                value = tok.split("=", 1)[1]
            elif tok.startswith("-O") and len(tok) > 2:
                value = tok[2:]          # -Oz, and -Oz6 with a compression level
            label = _BCF_BINARY_TYPES.get(value.strip().lower()[:1])
            if label:
                return label
        return ""
    for i, tok in enumerate(argv):
        if tok == "-b":
            return "BAM"
        if tok == "-C":
            return "CRAM"
        value = ""
        if tok in ("-O", "--output-fmt"):
            value = argv[i + 1] if i + 1 < len(argv) else ""
        elif tok.startswith("--output-fmt="):
            value = tok.split("=", 1)[1]
        label = _SAM_BINARY_FMTS.get(value.strip().lower().split(",")[0])
        if label:
            return label
    return ""


def _binary_output_error(module_name: str, sub: str, fmt: str) -> str:
    route = ("Write the text form (drop -O, keep `-o out.vcf`), then call tabix_index "
             "with preset=vcf — it bgzip-compresses in place and builds the .tbi, "
             "leaving an indexed file you can region-query."
             if module_name == "bcftools" else
             "Write SAM text (drop -b/-C), or use `samtools sort -o out.bam`, which "
             "writes BAM natively and is not affected.")
    return (f"error: `{module_name} {sub}` cannot emit {fmt} through this tool — its "
            f"output is captured as text, which would corrupt the bytes. {route}")


def _extract_output_path(argv: list):
    """Pull the agent's -o/--output/--output-file (and its value) out of argv.
    Returns (argv_without_it, path_or_None, error_or_None)."""
    out, path, skip = [], None, False
    for i, tok in enumerate(argv):
        if skip:
            skip = False
            continue
        if tok in _OUTPUT_FLAGS:
            if i + 1 >= len(argv) or argv[i + 1].startswith("-"):
                return None, None, f"error: {tok} needs a filename."
            path, skip = argv[i + 1], True
            continue
        attached = _ATTACHED_LONG_RE.match(tok)
        if attached and attached.group(1) in _OUTPUT_FLAGS:
            if not attached.group(2):
                return None, None, f"error: {attached.group(1)}= needs a filename."
            path = attached.group(2)
            continue
        out.append(tok)
    return out, path, None


def _scratch_dir() -> str:
    """The root of the workers' cwds: htslib's own scratch space, never the workspace.

    htslib caches a remote file's index in its cwd, and resolves any bare filename the
    sandbox could not recognise as a path against it. Pointing that at the workspace
    would let a read leave files there; at the server's cwd, let a bare name reach
    whatever directory the server was launched from. A directory beside the catalog
    cache (which the sandbox refuses to address) gives both a harmless home and keeps
    downloaded indexes across calls."""
    if _SCRATCH["dir"]:
        return _SCRATCH["dir"]
    from .catalog_source import cache_dir
    path = cache_dir() / "htslib"
    try:
        path.mkdir(parents=True, exist_ok=True)
        # Workers run in subdirectories (see _worker_cwd). A plain file at the root is an
        # index cached by an older release under its bare filename: never consulted
        # again, and not to be trusted, so clear it.
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                os.unlink(entry.path)
        _SCRATCH["dir"] = str(path)
    except OSError:
        _SCRATCH["dir"] = tempfile.mkdtemp(prefix="legumista-htslib-")
    return _SCRATCH["dir"]


_URL_RE = re.compile(r"(?i)https?://\S+")


def _worker_cwd(req: dict) -> str:
    """The directory one worker runs in: keyed by every remote URL the call opens.

    htslib caches a remote file's index in its cwd under the index's FILENAME alone, and
    reuses any local file of that name without asking which host it came from
    (hts.c, idx_test_and_fetch). In one shared directory, a caller who first reads
    `https://evil.example/<name>.vcf.gz` decides the index every later caller gets for
    the real `https://data.legumeinfo.org/.../<name>.vcf.gz`: a well-formed index with
    wrong offsets, so region queries silently return wrong or missing records. Keying
    the directory by the full URLs means an index is only ever reused for the URL it
    was downloaded beside. A call naming several remote files is keyed by the whole set.

    htslib also never revalidates a cached index, so a file republished upstream would
    keep its old index forever. Anything cached longer than INDEX_TTL is removed first,
    and htslib downloads it afresh."""
    tokens = req.get("argv") or [req.get("path") or ""]
    urls = sorted({m.group(0) for tok in tokens for m in [_URL_RE.search(tok)] if m})
    if not urls:
        path = os.path.join(_scratch_dir(), "local")
    else:
        key = hashlib.sha256("\n".join(urls).encode()).hexdigest()[:32]
        path = os.path.join(_scratch_dir(), "remote", key)
    os.makedirs(path, exist_ok=True)
    cutoff = time.time() - INDEX_TTL
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.is_file(follow_symlinks=False) and entry.stat().st_mtime < cutoff:
                try:
                    os.unlink(entry.path)
                except FileNotFoundError:      # another worker expired it first
                    continue
    return path


def _run_worker(req: dict, max_file_bytes: int = 0) -> dict:
    """Run one htslib operation in a child process (see _pysam_worker.py for the ops);
    return the worker's result dict, or one with `error` set to busy/timeout/crash.
    Blocks the calling (to_thread) thread.

    The child's environment routes all of htslib's HTTP through the egress proxy, which
    refuses non-public destinations on every connection — redirect hops included."""
    _ensure_ca_bundle()      # the child's libcurl reads it; set it for every caller
    if not _WORKER_SLOTS.acquire(timeout=TIMEOUT):
        return {"error": "busy"}
    try:
        with tempfile.TemporaryDirectory(prefix="legumista-pysam-") as tmp:
            req_path = os.path.join(tmp, "req.json")
            with open(req_path, "w", encoding="utf-8") as fh:
                json.dump({**req, "max_file_bytes": max_file_bytes}, fh)
            try:
                # -P: the script's directory and the cwd stay off sys.path, so nothing a
                # write left in the scratch directory can shadow an import.
                proc = subprocess.run(
                    [sys.executable, "-P", _WORKER, req_path],
                    cwd=_worker_cwd(req), env=egress_proxy.child_env(),
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT,
                    check=False)
            except subprocess.TimeoutExpired:
                return {"error": "timeout"}
        try:
            return json.loads(proc.stdout.decode("utf-8", "replace"))
        except ValueError:
            tail = proc.stderr.decode("utf-8", "replace").strip()[-500:]
            return {"error": "crash", "message": f"exit status {proc.returncode}"
                                                 + (f": {tail}" if tail else "")}
    finally:
        _WORKER_SLOTS.release()


def _worker_error(res: dict, what: str, target: str, *, tool: str = "", out_path=None,
                  limit: int = READ_FILE_BYTES, is_write: bool = False) -> str:
    """Turn a worker's error result into the tool's error text. `what` names the call
    (`samtools view`, `fasta_fetch`); `tool` is the CLI, for errors the CLI reported."""
    kind, msg = res["error"], (res.get("message") or "").strip()
    if kind == "busy":
        return (f"error: all {WORKERS} genomics workers stayed busy for {TIMEOUT}s; "
                "try again shortly.")
    if kind == "timeout":
        return (f"error: `{what}` ran past the {TIMEOUT}s limit and was stopped. Narrow "
                "the region or use a smaller input.")
    if kind == "limit":
        return (f"error: `{what}` stopped at this server's {limit:,}-byte limit on a "
                "file one call may write"
                + ("" if is_write else " (a read only needs what fits in the result)")
                + ". Narrow the region or filter more tightly.")
    if kind == "tool":
        return _cap(f"[{tool} error] {msg or 'non-zero exit'}")
    if kind == "open":
        cls = res.get("class") or "Error"
        exc = (OSError(msg) if cls == "os" else ValueError(msg) if cls == "value"
               else type(cls, (Exception,), {})(msg))
        return _open_err(exc, target)
    if kind == "write":
        return f"error: could not write {os.path.basename(out_path or '')}: {msg}"
    if kind == "other":
        return f"error: {tool or what} raised {res.get('class')}: {msg}"
    return f"error: the {tool or what} worker failed ({msg})"


def _dispatch(module_name: str, readset, allow_write: bool, args) -> str:
    pysam, err = _pysam()
    if err:
        return err
    argv = args.get("args")
    if not isinstance(argv, list) or not argv:
        return (f'error: pass "args" as a non-empty list, e.g. '
                f'["view","-c","reads.bam","glyma.Wm82.gnm4.Gm12:1-1000"] for {module_name}.')
    argv = [str(a) for a in argv]
    sub = argv[0].strip().lower().replace("-", "_")
    # Resolve the subcommand first (no side effects) so an unknown one reports clearly
    # regardless of read/write mode.
    try:
        mod = importlib.import_module(f"pysam.{module_name}")
    except Exception as e:  # noqa: BLE001
        return f"error: could not load pysam.{module_name}: {type(e).__name__}: {e}"
    fn = getattr(mod, sub, None)
    if not callable(fn):
        common = ("view, sort, index, depth, coverage, stats" if module_name == "samtools"
                  else "view, call, query, norm, stats, index")
        return (f"error: unknown {module_name} subcommand {argv[0]!r}. "
                f"Standard ones include: {common}.")
    for tok in argv[1:]:
        err = _refuse_composite(tok)
        if err:
            return err
    guarded, not_read, err = _read_argv(readset, sub, argv[1:])
    if err:
        return err
    is_write = not_read is not None
    if is_write and not allow_write:
        what = (f"'{module_name} {argv[0]}' is a write operation" if not not_read else
                f"'{not_read}' is not a read-only option of `{module_name} {argv[0]}`, "
                "so this call counts as a write")
        return (f"error: {what} and writes are disabled. The server must be restarted "
                "with `legumista mcp --allow-write` to permit it.")
    if is_write:
        guarded, err = _guard_argv(argv[1:])
        if err:
            return err
    if (module_name == "bcftools" and sub == "query"
            and any(t in _LIST_SAMPLES_FLAGS for t in guarded)):
        handled = _bcftools_list_samples(guarded)
        if handled is not None:
            return handled
    out_path = None
    if _pysam_injects_output(module_name, sub, guarded):
        fmt = _binary_output_request(module_name, guarded)
        if fmt:
            return _binary_output_error(module_name, sub, fmt)
        guarded, out_path, oerr = _extract_output_path(guarded)
        if oerr:
            return oerr
    limit = MAX_FILE_BYTES if is_write else READ_FILE_BYTES
    res = _run_worker({"op": "dispatch", "module": module_name, "sub": sub,
                       "argv": guarded, "out_path": out_path,
                       "head_bytes": MAX_CHARS * 4}, limit)
    if res.get("error"):
        return _worker_error(res, f"{module_name} {argv[0]}", " ".join(argv),
                             tool=module_name, out_path=out_path, limit=limit,
                             is_write=is_write)
    if out_path is not None:
        name = os.path.basename(out_path)
        if not res.get("written"):
            return f"({module_name} {argv[0]}: produced no output; wrote empty {name})"
        return f"wrote {res['written']:,} bytes ({res['lines']:,} lines) to {name}"
    out = res.get("stdout") or ""
    if not out.strip():
        return (f"({module_name} {argv[0]}: completed with no stdout"
                + ("; output written" if is_write else "") + ")")
    return _cap(out)


# --- read-only Python-API helpers ----------------------------------------------------
def _fasta_fetch(args) -> str:
    pysam, err = _pysam()
    if err:
        return err
    target, err = _resolve_source(args.get("path"))
    if err:
        return err
    contig, start, end, rerr = _parse_region(args.get("region"))
    if rerr:
        return rerr
    res = _run_worker({"op": "fasta", "path": target, "contig": contig, "start": start,
                       "end": end, "max_seq": MAX_SEQ}, READ_FILE_BYTES)
    if res.get("error"):
        return _worker_error(res, "fasta_fetch", target)
    seq, start, end, span = res["seq"], res["start"], res["end"], res["span"]
    head = (f">{contig}:{start + 1}-{end} ({len(seq):,} bp"
            + (f" of {span:,}; truncated to {MAX_SEQ:,}" if res["truncated"] else "") + ")\n")
    wrapped = "\n".join(seq[i:i + 70] for i in range(0, len(seq), 70))
    return _cap(head + (wrapped or "(empty — region outside the contig?)"))


def _tabix_query(args) -> str:
    pysam, err = _pysam()
    if err:
        return err
    target, err = _resolve_source(args.get("path"))
    if err:
        return err
    region = (args.get("region") or "").strip()
    if not region:
        res = _run_worker({"op": "tabix", "path": target, "contig": None}, READ_FILE_BYTES)
        if res.get("error"):
            return _worker_error(res, "tabix_query", target)
        contigs = res["contigs"]
        shown = ", ".join(contigs[:MAX_RECORDS]) + (
            f" … (+{len(contigs) - MAX_RECORDS})" if len(contigs) > MAX_RECORDS else "")
        return _cap(f"tabix {os.path.basename(target)}: {len(contigs)} contig(s): "
                    f"{shown or '(none)'}\n(pass a 'region' to fetch feature lines)")
    contig, start, end, rerr = _parse_region(region)
    if rerr:
        return rerr
    limit = min(int(args.get("max_records") or MAX_RECORDS), MAX_RECORDS)
    res = _run_worker({"op": "tabix", "path": target, "contig": contig, "start": start,
                       "end": end, "limit": limit}, READ_FILE_BYTES)
    if res.get("error"):
        return _worker_error(res, "tabix_query", target)
    rows = ["  " + line for line in res["rows"]]
    span = contig if start is None else f"{contig}:{start + 1}-{end}"
    if not rows:
        return f"No records in {span} of {os.path.basename(target)}."
    head = f"{len(rows)} record(s) in {span}" + ("  [capped]" if res["more"] else "") + ":\n"
    return _cap(head + "\n".join(rows))


# --- write helper: build a bgzip+tabix index -----------------------------------------
def _tabix_index(args) -> str:
    pysam, err = _pysam()
    if err:
        return err
    raw = (args.get("path") or "").strip()
    if _is_url(raw):
        return "error: tabix_index writes an index, so it needs a local workspace file, not a URL."
    target, err = _sandbox_path(raw)
    if err:
        return err
    preset = (args.get("preset") or "").strip().lower()
    if preset not in ("gff", "bed", "vcf", "sam", "psltbl"):
        return ("error: 'preset' must be one of gff | bed | vcf | sam | psltbl "
                "(the tabix column layout for this file type).")
    try:
        with _INDEX_LOCK:
            out_path = pysam.tabix_index(target, preset=preset, force=True, keep_original=True)
    except Exception as e:  # noqa: BLE001
        return _open_err(e, target)
    idx = out_path + (".tbi")
    return (f"indexed {os.path.basename(target)} (preset={preset}) -> "
            f"{os.path.basename(out_path)} (+ {os.path.basename(idx)})")


def _mk(name, description, params, sync_fn, *, read_only=True, writes=None):
    async def run(a, _f=sync_fn):
        return await asyncio.to_thread(_f, a)
    return Tool(name=name, description=description, parameters=params,
                read_only=read_only, run=run, writes=writes)


def bio_tools(allow_write: bool = False) -> list:
    """The pysam/htslib toolset. `allow_write` gates the dispatchers' and `tabix_index`'s
    write operations at the tool boundary (belt-and-suspenders alongside the permission
    gate, and the sole gate on the MCP server, which has no permission layer).

    It also decides how those three tools are advertised. Without it they cannot write
    anything — every write is refused — so they are read-only and say so: clients use
    `readOnlyHint` to decide whether to ask the user before each call, and a prompt for
    a tool that cannot write is noise. With it they can, and are advertised as such."""
    args_arr = {"args": {"type": "array", "items": {"type": "string"},
                         "description": "argv for the CLI: the subcommand followed by its "
                                        "flags/arguments, e.g. [\"view\",\"-c\",\"reads.bam\","
                                        "\"glyma.Wm82.gnm4.Gm12:1-1000\"]. File paths are confined to the "
                                        "workspace; regions are samtools-style (1-based)."}}
    path = {"path": {"type": "string",
                     "description": "An indexed file: a workspace path or http(s) URL, with "
                                    "its sibling index (.fai/.bai/.csi/.tbi) present."}}
    region = {"region": {"type": "string",
                         "description": "samtools-style region, 1-based inclusive: 'seqid', "
                                        "'seqid:start', or 'seqid:start-end'."}}
    return [
        _mk("samtools",
            "Run any samtools subcommand on SAM/BAM/CRAM/FASTA (view, sort, index, depth, "
            "coverage, flagstat, idxstats, stats, faidx, markdup, consensus, …). Args: "
            "{args: [subcommand, ...]}. Read subcommands (view/flagstat/idxstats/stats/"
            "depth/coverage/…) work by default; writing ones (sort/index/markdup/…) need "
            "--allow-write. Regions are 1-based (e.g. 'glyma.Wm82.gnm4.Gm12:1000-2000').",
            {"type": "object", "properties": {**args_arr}, "required": ["args"]},
            lambda a: _dispatch("samtools", _SAM_READ, allow_write, a),
            read_only=not allow_write, writes=_mk_writes(_SAM_READ)),
        _mk("bcftools",
            "Run any bcftools subcommand on VCF/BCF (view, query, stats, call, norm, "
            "annotate, merge, index, consensus, …). Args: {args: [subcommand, ...]}. "
            "Read subcommands (view/query/stats/…) work by default; writing ones "
            "(call/norm/index/…) need --allow-write. Regions are 1-based.",
            {"type": "object", "properties": {**args_arr}, "required": ["args"]},
            lambda a: _dispatch("bcftools", _BCF_READ, allow_write, a),
            read_only=not allow_write, writes=_mk_writes(_BCF_READ)),
        _mk("fasta_fetch",
            "Extract a subsequence from an indexed FASTA (.fai) — a guaranteed read-only "
            f"convenience over `samtools faidx`. Args: {{path, region}}. Capped at {MAX_SEQ:,} "
            "bp per call.",
            {"type": "object", "properties": {**path, **region},
             "required": ["path", "region"]}, _fasta_fetch),
        _mk("tabix_query",
            "Query a bgzip+tabix feature table (GFF/GTF/BED or any tabbed genomic file): "
            "no 'region' lists indexed contigs, a region returns the feature lines. "
            "Read-only. Args: {path, region?, max_records?}.",
            {"type": "object",
             "properties": {**path, **region,
                            "max_records": {"type": "integer",
                                            "description": f"Row cap (1–{MAX_RECORDS})."}},
             "required": ["path"]}, _tabix_query),
        _mk("tabix_index",
            "Build a bgzip+tabix index for a GFF/BED/VCF/SAM file (bgzip-compresses in "
            "place if needed), so it becomes region-queryable. A WRITE — needs "
            "--allow-write. Args: {path, preset: gff|bed|vcf|sam|psltbl}.",
            {"type": "object",
             "properties": {**path,
                            "preset": {"type": "string",
                                       "enum": ["gff", "bed", "vcf", "sam", "psltbl"],
                                       "description": "Column layout of the file: gff, "
                                                      "bed, vcf, sam or psltbl."}},
             "required": ["path", "preset"]}, _tabix_index,
            read_only=not allow_write, writes=lambda a: True),
    ]
