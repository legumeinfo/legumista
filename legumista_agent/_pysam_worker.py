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

The request is a JSON object:
    module, sub, argv     the dispatch: pysam.<module>.<sub>(*argv)
    out_path              optional: write the captured stdout here instead of returning it
    head_bytes            how much captured stdout to send back
    max_file_bytes        RLIMIT_FSIZE for this process (0: unlimited)
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


def _run(req: dict) -> dict:
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
        return {"error": "open", "class": "os" if isinstance(e, OSError) else "value",
                "message": str(e)}
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
