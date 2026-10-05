#!/usr/bin/env python3
"""Run ONE samtools/bcftools dispatch in a child process, for tools_pysam.

    python -P _pysam_worker.py <request.json>       # result: one JSON object on stdout

Why a child process rather than a thread: a thread blocked inside htslib (a slow remote
host, a multi-GB stream) cannot be interrupted, and pysam's CLI dispatcher redirects the
process's stdout, so in-process calls had to be serialized behind one global lock — one
stuck call stalled every genomics call on the server. A child can be killed on a
wall-clock timeout, many can run at once, and whatever the CLI prints lands in the
child's stdout (discarded), never in the MCP server's protocol stream.

Deliberately standalone — stdlib + pysam, no legumista imports — so it starts fast and
so the parent can run it with `-P` (no script directory or cwd on sys.path): the child's
working directory is htslib's scratch directory, which a write-enabled caller can put
files in, and nothing there may ever be importable.

The result goes back over the process's ORIGINAL stdout, a pipe: a pipe is not subject
to the RLIMIT_FSIZE below, so a result file could be cut short by the very limit it is
reporting. File descriptor 1 itself is pointed at a scratch file for the dispatch, which
also recovers output from subcommands that bypass pysam's capture (samtools fasta/fastq).

Every htslib read runs here, the read-only helpers included, so all of htslib's network
traffic leaves through the egress proxy named in this process's environment
(egress_proxy.child_env) and all of it is bounded by the parent's timeout.

The request is a JSON object with an `op`:
    dispatch   module, sub, argv: pysam.<module>.<sub>(*argv)
               out_path: optional, write the captured stdout here instead of returning it
               head_bytes: how much captured stdout to send back
    fasta      path, contig, start, end, max_seq: a subsequence (fasta_fetch)
    tabix      path, contig, start, end, limit: feature lines, or contigs if no contig
    samples    path: the sample names in a VCF/BCF header
    batch      items: many small reads in one process, each file opened once — for a
               tool that needs dozens of sequences or feature rows per call:
                 {"kind": "fasta", "path", "name", "start"?, "end"?, "max_len"}
                   -> {"seq", "ref_len"}; start/end 0-based half-open, clipped to the
                      sequence; a whole record when both are absent
                 {"kind": "pick", "path", "names", "rule": "present" | "longest"}
                   -> {"name", "length"}: the first name the FASTA holds, or the longest
                 {"kind": "tabix", "path", "contig", "start", "end", "limit"} -> {"rows"}
               An item that fails carries {"error", "class"}; the others still run.
and, for every op, max_file_bytes: RLIMIT_FSIZE for this process (0: unlimited).
"""
import importlib
import json
import os
import resource
import signal
import sys
import tempfile

# Set when a write passes RLIMIT_FSIZE. With a handler installed the kernel fails the
# write with EFBIG instead of killing the process, and the CLI's own message for that
# ("failed to write the SAM header") never says why — this flag does.
_HIT_LIMIT = []


def _error(e: Exception) -> dict:
    kind = "os" if isinstance(e, OSError) else "value" if isinstance(e, ValueError) else ""
    return {"error": "open", "class": kind or type(e).__name__, "message": str(e)}


def _fasta(req: dict) -> dict:
    import pysam

    contig, start, end, max_seq = req["contig"], req["start"], req["end"], req["max_seq"]
    try:
        fa = pysam.FastaFile(req["path"])
    except Exception as e:  # noqa: BLE001 - reported to the parent
        return _error(e)
    try:
        clen = fa.lengths[fa.references.index(contig)] if contig in set(fa.references) else None
        if start is None:
            start, end = 0, min(clen if clen is not None else max_seq, max_seq)
        span = end - start
        truncated = span > max_seq
        seq = fa.fetch(reference=contig, start=start, end=start + max_seq if truncated else end)
    except Exception as e:  # noqa: BLE001
        return _error(e)
    finally:
        fa.close()
    return {"seq": seq, "start": start, "end": end, "span": span, "truncated": truncated}


def _tabix(req: dict) -> dict:
    import pysam

    try:
        tbx = pysam.TabixFile(req["path"])
    except Exception as e:  # noqa: BLE001
        return _error(e)
    try:
        if req.get("contig") is None:
            return {"contigs": list(tbx.contigs)}
        rows, more = [], False
        try:
            for line in tbx.fetch(req["contig"], req["start"], req["end"]):
                if len(rows) >= req["limit"]:
                    more = True
                    break
                rows.append(line[:300])
        except Exception as e:  # noqa: BLE001
            return _error(e)
        return {"rows": rows, "more": more}
    finally:
        tbx.close()


def _samples(req: dict) -> dict:
    import pysam

    try:
        with pysam.VariantFile(req["path"]) as vf:
            return {"samples": list(vf.header.samples)}
    except (OSError, ValueError) as e:
        return _error(e)


def _batch(req: dict) -> dict:
    import pysam

    handles, out = {}, []

    def open_file(kind, path):
        key = (kind, path)
        if key not in handles:
            handles[key] = (pysam.FastaFile(path) if kind != "tabix"
                            else pysam.TabixFile(path))
        return handles[key]

    for item in req.get("items", []):
        kind = item.get("kind")
        try:
            handle = open_file(kind, item["path"])
            if kind == "fasta":
                name = item["name"]
                ref_len = handle.get_reference_length(name)
                start = max(0, int(item.get("start") or 0))
                end = ref_len if item.get("end") is None else min(int(item["end"]), ref_len)
                end = min(end, start + int(item.get("max_len") or end - start))
                seq = handle.fetch(reference=name, start=start, end=end) if end > start else ""
                out.append({"seq": seq, "ref_len": ref_len, "start": start, "end": end})
            elif kind == "pick":
                lengths = dict(zip(handle.references, handle.lengths))
                present = [n for n in item["names"] if n in lengths]
                if item.get("rule") == "longest" and present:
                    present.sort(key=lambda n: (-lengths[n], n))
                name = present[0] if present else None
                out.append({"name": name, "length": lengths.get(name) if name else None})
            elif kind == "tabix":
                rows = []
                if item["contig"] in set(handle.contigs):
                    for row in handle.fetch(item["contig"], int(item["start"]),
                                            int(item["end"])):
                        rows.append(row)
                        if len(rows) >= int(item.get("limit") or 5000):
                            break
                out.append({"rows": rows})
            else:
                out.append({"error": f"unknown batch item kind {kind!r}", "class": "value"})
        except Exception as e:  # noqa: BLE001 - one failed item must not sink the rest
            out.append(_error(e))
    for handle in handles.values():
        handle.close()
    return {"items": out}


def _run(req: dict) -> dict:
    op = req.get("op", "dispatch")
    if op != "dispatch":
        return {"fasta": _fasta, "tabix": _tabix, "samples": _samples,
                "batch": _batch}[op](req)
    from pysam.utils import SamtoolsError

    fn = getattr(importlib.import_module(f"pysam.{req['module']}"), req["sub"])
    head = int(req.get("head_bytes") or 0)
    fd1 = tempfile.TemporaryFile()
    os.dup2(fd1.fileno(), 1)
    try:
        out = fn(*req["argv"])
    except SamtoolsError as e:
        if _HIT_LIMIT:
            return {"error": "limit"}
        return {"error": "tool", "message": str(e)[: max(head, 4096)]}
    except (OSError, ValueError) as e:
        return _error(e)
    except Exception as e:  # noqa: BLE001 - report it; the parent formats the error
        return {"error": "other", "class": type(e).__name__, "message": str(e)}
    data = b"" if out is None else (out if isinstance(out, bytes) else str(out).encode())
    if not data:
        fd1.seek(0)
        data = fd1.read()
    if req.get("out_path"):
        try:
            with open(req["out_path"], "wb") as fh:
                fh.write(data)
        except OSError as e:
            return {"error": "limit"} if _HIT_LIMIT else {"error": "write", "message": str(e)}
        return {"written": len(data), "lines": data.count(b"\n")}
    return {"stdout": data[:head].decode("utf-8", "replace"), "bytes": len(data)}


def main(req_path: str) -> int:
    with open(req_path, encoding="utf-8") as fh:
        req = json.load(fh)
    result_fd = os.dup(1)
    limit = int(req.get("max_file_bytes") or 0)
    if limit > 0:
        signal.signal(signal.SIGXFSZ, lambda *_: _HIT_LIMIT.append(1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
    result = _run(req)
    with os.fdopen(result_fd, "w", encoding="utf-8") as fh:
        json.dump(result, fh)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
