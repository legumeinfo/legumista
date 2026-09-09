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
- The dispatchers classify each call: a subcommand in the per-tool read-only allowlist
  with no output-file flag is a read (allowed by default); anything else is a **write**
  and runs only when the server was started with `legumista mcp --allow-write`. The MCP
  server has no permission gate, so that flag is the sole control and the classifier is
  enforced by the tool's own guard — writes fail closed without it.

Safety, matching tools_native.py: pysam/htslib I/O is offloaded with `asyncio.to_thread`;
local path arguments are confined to the workspace by the shared `_sandbox_path` guard
and rewritten to absolute so the process cwd is irrelevant; http(s) URL
arguments pass the native SSRF guard (other schemes refused). pysam is a dependency of
legumista, imported lazily (only when a genomics tool actually runs) so the heavy htslib
extension isn't loaded by sessions that never touch a genomics tool.

Coordinates: `region` in the helpers is samtools-style — `seqid`, `seqid:start`, or
`seqid:start-end`, 1-based inclusive — converted to pysam's 0-based half-open internally.
"""
import asyncio
import importlib
import os
import re
import threading

import config

from .tool import Tool
from .tools_native import BlockedURLError, _cap, _sandbox_path, _validate_url

MAX_RECORDS = int(os.environ.get("LEGUMISTA_PYSAM_MAX_RECORDS", "200"))
MAX_SEQ = int(os.environ.get("LEGUMISTA_PYSAM_MAX_SEQ", "100000"))

# pysam's CLI dispatchers redirect the process's stdout to capture output, which is not
# reentrant — serialize dispatcher calls (they run in `to_thread` workers, possibly
# concurrently under the MCP server).
_DISPATCH_LOCK = threading.Lock()


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


def _validate_remote(url: str):
    """Vet an http(s) URL argument (SSRF guard + optional allowlist). Returns (url, None)
    or (None, error). Non-http(s) schemes are refused — htslib does its own I/O, so we
    only permit transports we can check up front."""
    if not re.match(r"(?i)^https?://", url):
        return None, (f"error: refused URL scheme {url.split('://', 1)[0]!r} — only "
                      "http(s) URLs and local workspace files are allowed")
    allow = [p.strip() for p in os.environ.get("LEGUMISTA_PYSAM_ALLOWED_URLS", "").split(",")
             if p.strip()]
    if allow and not any(url.startswith(p) for p in allow):
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


def _guard_argv(argv: list):
    """Confine every path/URL token in a dispatcher argv. Path tokens are workspace-
    sandboxed and rewritten to absolute realpaths (so cwd is irrelevant); URL tokens pass
    the SSRF guard and are kept verbatim; everything else passes through. Returns
    (new_argv, None) or (None, error_text)."""
    out, expect_path = [], False
    for tok in argv:
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
        return None, None, None, "error: missing 'region' (e.g. 'chr1:1000-2000')"
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
# Read-only subcommands (write only to stdout, no filesystem side effects). Anything not
# listed — or any call carrying an output-file flag — is treated as a write and needs
# --allow-write. Conservative on purpose: unknown/omitted => write (fail closed).
_SAM_READ = {"view", "flagstat", "idxstats", "stats", "depth", "coverage", "bedcov",
             "quickcheck", "head", "dict", "consensus", "fasta", "fastq", "samples",
             "ampliconstats", "cram_size", "checksum"}
_BCF_READ = {"view", "query", "stats", "head", "roh", "csq"}
_OUTPUT_FLAGS = ("-o", "--output", "--output-file")


def _subcommand(args) -> str:
    argv = args.get("args") or []
    return str(argv[0]).strip().lower().replace("-", "_") if argv else ""


def _mk_writes(readset):
    """A per-call write classifier for a dispatcher over `readset`."""
    def writes(args) -> bool:
        argv = args.get("args") or []
        if not argv:
            return False
        if _subcommand(args) not in readset:
            return True
        for tok in argv[1:]:
            t = str(tok)
            attached = _ATTACHED_LONG_RE.match(t)
            if t in _OUTPUT_FLAGS or (attached and attached.group(1) in _OUTPUT_FLAGS):
                return True
        return False
    return writes


# `bcftools query -l` prints sample names straight to the process's stdout, bypassing the
# output stream that `-o` controls. To capture output, pysam's dispatcher appends
# `-o <tmpfile>` to the argv and sends the real stdout to /dev/null — so the sample list
# is discarded and the dispatcher returns "" with exit status 0: a silent empty answer.
# Serve the listing from the VariantFile header instead; same answer, no CLI text to parse.
_LIST_SAMPLES_FLAGS = ("-l", "--list-samples")


def _bcftools_list_samples(pysam, argv):
    """Answer `bcftools query -l <file>` from the header. Returns the text, or None if
    this argv is not a plain sample listing (combined with other options), in which case
    the caller runs the normal dispatcher rather than guessing at the intent."""
    rest = [t for t in argv if t not in _LIST_SAMPLES_FLAGS]
    if len(rest) != 1 or rest[0].startswith("-"):
        return None
    try:
        with pysam.VariantFile(rest[0]) as vf:
            samples = list(vf.header.samples)
    except (OSError, ValueError) as e:
        return _open_err(e, rest[0])
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


def _write_output(out, path: str, module_name: str, sub: str) -> str:
    """Persist a dispatcher's captured stdout to the file the agent asked for."""
    data = b"" if out is None else (out if isinstance(out, bytes) else str(out).encode())
    try:
        with open(path, "wb") as fh:
            fh.write(data)
    except OSError as e:
        return f"error: could not write {os.path.basename(path)}: {e}"
    name = os.path.basename(path)
    if not data:
        return f"({module_name} {sub}: produced no output; wrote empty {name})"
    lines = data.count(b"\n")
    return f"wrote {len(data):,} bytes ({lines:,} lines) to {name}"


def _dispatch(module_name: str, readset, allow_write: bool, args) -> str:
    pysam, err = _pysam()
    if err:
        return err
    argv = args.get("args")
    if not isinstance(argv, list) or not argv:
        return (f'error: pass "args" as a non-empty list, e.g. '
                f'["view","-c","reads.bam","chr1:1-1000"] for {module_name}.')
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
    if _mk_writes(readset)(args) and not allow_write:
        return (f"error: '{module_name} {argv[0]}' is a write operation and writes are "
                "disabled. The server must be restarted with `legumista mcp "
                "--allow-write` to permit it.")
    guarded, err = _guard_argv(argv[1:])
    if err:
        return err
    if (module_name == "bcftools" and sub == "query"
            and any(t in _LIST_SAMPLES_FLAGS for t in guarded)):
        handled = _bcftools_list_samples(pysam, guarded)
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
    from pysam.utils import SamtoolsError
    with _DISPATCH_LOCK:
        try:
            out = fn(*guarded)
        except SamtoolsError as e:
            return _cap(f"[{module_name} error] {str(e).strip() or 'non-zero exit'}")
        except (OSError, ValueError) as e:
            return _open_err(e, " ".join(argv))
        except Exception as e:  # noqa: BLE001 - never let a dispatch kill the loop
            return f"error: {module_name} raised {type(e).__name__}: {e}"
    if out_path is not None:
        return _write_output(out, out_path, module_name, argv[0])
    out = "" if out is None else (out if isinstance(out, str) else str(out))
    if not out.strip():
        return (f"({module_name} {argv[0]}: completed with no stdout"
                + ("; output written" if _mk_writes(readset)(args) else "") + ")")
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
    try:
        fa = pysam.FastaFile(target)
    except Exception as e:  # noqa: BLE001
        return _open_err(e, target)
    try:
        clen = fa.lengths[fa.references.index(contig)] if contig in set(fa.references) else None
        if start is None:
            start, end = 0, min(clen if clen is not None else MAX_SEQ, MAX_SEQ)
        span = end - start
        truncated = span > MAX_SEQ
        seq = fa.fetch(reference=contig, start=start, end=start + MAX_SEQ if truncated else end)
    except Exception as e:  # noqa: BLE001
        return _open_err(e, target)
    finally:
        fa.close()
    head = (f">{contig}:{start + 1}-{end} ({len(seq):,} bp"
            + (f" of {span:,}; truncated to {MAX_SEQ:,}" if truncated else "") + ")\n")
    wrapped = "\n".join(seq[i:i + 70] for i in range(0, len(seq), 70))
    return _cap(head + (wrapped or "(empty — region outside the contig?)"))


def _tabix_query(args) -> str:
    pysam, err = _pysam()
    if err:
        return err
    target, err = _resolve_source(args.get("path"))
    if err:
        return err
    try:
        tbx = pysam.TabixFile(target)
    except Exception as e:  # noqa: BLE001
        return _open_err(e, target)
    try:
        region = (args.get("region") or "").strip()
        if not region:
            contigs = list(tbx.contigs)
            shown = ", ".join(contigs[:MAX_RECORDS]) + (
                f" … (+{len(contigs) - MAX_RECORDS})" if len(contigs) > MAX_RECORDS else "")
            return _cap(f"tabix {os.path.basename(target)}: {len(contigs)} contig(s): "
                        f"{shown or '(none)'}\n(pass a 'region' to fetch feature lines)")
        contig, start, end, rerr = _parse_region(region)
        if rerr:
            return rerr
        limit = min(int(args.get("max_records") or MAX_RECORDS), MAX_RECORDS)
        rows, n, more = [], 0, False
        try:
            for line in tbx.fetch(contig, start, end):
                if n >= limit:
                    more = True
                    break
                rows.append("  " + line[:300])
                n += 1
        except Exception as e:  # noqa: BLE001
            return _open_err(e, target)
        span = contig if start is None else f"{contig}:{start + 1}-{end}"
        if not rows:
            return f"No records in {span} of {os.path.basename(target)}."
        head = f"{n} record(s) in {span}" + ("  [capped]" if more else "") + ":\n"
        return _cap(head + "\n".join(rows))
    finally:
        tbx.close()


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
        with _DISPATCH_LOCK:                 # tabix_index compresses in place if needed
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
    gate, and the sole gate on the MCP server, which has no permission layer)."""
    args_arr = {"args": {"type": "array", "items": {"type": "string"},
                         "description": "argv for the CLI: the subcommand followed by its "
                                        "flags/arguments, e.g. [\"view\",\"-c\",\"reads.bam\","
                                        "\"chr1:1-1000\"]. File paths are confined to the "
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
            "--allow-write. Regions are 1-based (e.g. 'chr1:1000-2000').",
            {"type": "object", "properties": {**args_arr}, "required": ["args"]},
            lambda a: _dispatch("samtools", _SAM_READ, allow_write, a),
            read_only=False, writes=_mk_writes(_SAM_READ)),
        _mk("bcftools",
            "Run any bcftools subcommand on VCF/BCF (view, query, stats, call, norm, "
            "annotate, merge, index, consensus, …). Args: {args: [subcommand, ...]}. "
            "Read subcommands (view/query/stats/…) work by default; writing ones "
            "(call/norm/index/…) need --allow-write. Regions are 1-based.",
            {"type": "object", "properties": {**args_arr}, "required": ["args"]},
            lambda a: _dispatch("bcftools", _BCF_READ, allow_write, a),
            read_only=False, writes=_mk_writes(_BCF_READ)),
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
                                       "enum": ["gff", "bed", "vcf", "sam", "psltbl"]}},
             "required": ["path", "preset"]}, _tabix_index,
            read_only=False, writes=lambda a: True),
    ]
