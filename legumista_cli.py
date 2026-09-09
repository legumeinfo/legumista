#!/usr/bin/env python3
"""`legumista` — command-line entry point for the legumista MCP server.

One command, `legumista mcp`, which serves the legume-genomics toolset over the Model
Context Protocol. There is no project, config file, or working directory to set up: the
server is self-contained and the collection catalog ships with the package.

    legumista mcp                      # stdio — how an MCP client spawns a server
    legumista mcp -t http --port 8000  # a long-running HTTP endpoint
    legumista mcp --allow-write        # also expose the write-capable genomics ops
"""

import os
import sys

import typer

app = typer.Typer(
    add_completion=False,
    help="Serve legume-genomics, literature and NCBI tools over MCP.",
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _out(msg: str) -> None:
    print(msg, flush=True)


@app.callback()
def _root() -> None:
    """Typer collapses a single-command app into a bare `legumista [OPTIONS]`. This
    no-op callback keeps `mcp` addressable as a subcommand, which is what every existing
    client config and the container ENTRYPOINT invoke."""


@app.command()
def mcp(
    transport: str = typer.Option("stdio", "--transport", "-t",
        help="Transport: 'stdio' (default; how MCP clients spawn a server) or 'http'."),
    host: str = typer.Option("127.0.0.1", help="Bind host (http transport only)."),
    port: int = typer.Option(8000, help="Bind port (http transport only)."),
    root: str = typer.Option(None, "--root", "-C", metavar="DIR",
        help="Directory local file arguments are confined to. Default: the launch "
             "directory. Only matters for the genomics tools' file paths."),
    allow_write: bool = typer.Option(
        False, "--allow-write",
        help="Expose the genomics tools' write operations (samtools sort/index, bcftools "
             "call, tabix_index). Off by default — read operations only."),
):
    """Start a spec-compliant FastMCP server exposing legumista's tools: the LIS Data
    Store catalog (lis_find/lis_files/lis_gene/lis_synteny, lis_survey/lis_lineage),
    InterMine queries against the LIS mines, scholarly search and read_paper, NCBI
    datasets + EDirect, web search/fetch, and the pysam genomics suite (samtools,
    bcftools, tabix, fasta_fetch)."""
    if transport not in ("stdio", "http"):
        _err(f"[!] unknown transport {transport!r} — use 'stdio' or 'http'.")
        raise typer.Exit(1)
    # config reads LEGUMISTA_HOME at import time, so set it before the import below.
    if root:
        os.environ["LEGUMISTA_HOME"] = os.path.abspath(root)

    from legumista_agent.mcp_server import serve

    # stdio speaks the protocol on stdout, so status must go to stderr to avoid
    # corrupting the JSON-RPC stream; http is a plain server, so stdout is fine.
    log = _err if transport == "stdio" else _out
    log(f"[*] legumista MCP server (transport={transport}"
        + (f", http://{host}:{port}/mcp" if transport == "http" else "")
        + (", writes ENABLED" if allow_write else "") + ") …")
    try:
        serve(transport=transport, host=host, port=port, allow_write=allow_write)
    except KeyboardInterrupt:
        log("[*] stopped.")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
