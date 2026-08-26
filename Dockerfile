# Container distribution of the legumista MCP server (stdio by default).
#
# The primary distribution channel is PyPI + `uvx` (see README "Wiring it into an MCP
# client"); this image is the secondary, container channel. The OCI label lets the official
# MCP Registry verify an `oci` package entry pointing at a pushed image.
#
#   docker build -t legumista .
#   docker run --rm -i legumista                       # stdio; -i keeps stdin open
#   docker run --rm -p 8000:8000 legumista --http --host 0.0.0.0
#
# Unlike a bare `pip install`, this image also carries the EXTERNAL CLIs that four of the
# served tools shell out to, so the whole advertised toolset works out of the box:
#
#   ncbi_datasets, ncbi_assembly_status  ->  NCBI `datasets`
#   edirect, sra_runs                    ->  NCBI EDirect (esearch/efetch/esummary/xtract)
#
# samtools/bcftools need no system package: pysam's manylinux wheels bundle htslib and
# both command suites, so the genomics tools install without a C toolchain.
FROM python:3.12-slim

LABEL io.modelcontextprotocol.server.name="io.github.legumeinfo/legumista"

ENV PYTHONUNBUFFERED=1

# Optional: an NCBI API key raises the Entrez rate limit from 3 to 10 requests/second.
# EDirect picks it up from the environment. Without one, broad `sra_runs`/`edirect`
# queries are throttled upstream and can exceed the tools' own 120 s timeout — that is
# NCBI, not this image. Pass at run time:  docker run -e NCBI_API_KEY=... legumista
#   https://ncbiinsights.ncbi.nlm.nih.gov/2017/11/02/new-api-keys-for-the-e-utilities/

# ca-certificates: htslib's vendored libcurl needs a CA bundle to read https:// genomics
#   files (the base image ships one, but an explicit install keeps that a stated
#   requirement rather than an inherited accident — see tools_pysam._ensure_ca_bundle).
# curl: fetches the NCBI CLIs below. bash/perl are already in the base image and are
#   what the EDirect scripts run on.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# --- NCBI Datasets CLI (static single-file binaries) ---------------------------------
# TARGETARCH is supplied by BuildKit; default to amd64 for a plain `docker build`.
ARG TARGETARCH=amd64
RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) ncbi_arch=linux-amd64 ;; \
      arm64) ncbi_arch=linux-arm64 ;; \
      *) echo "unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    base=https://ftp.ncbi.nlm.nih.gov/pub/datasets/command-line/v2/"$ncbi_arch"; \
    for tool in datasets dataformat; do \
      curl -fsSL -o /usr/local/bin/"$tool" "$base/$tool"; \
      chmod +x /usr/local/bin/"$tool"; \
    done; \
    datasets --version

# --- NCBI EDirect --------------------------------------------------------------------
# The tarball holds the driver scripts (esearch/efetch/esummary/nquire, all bash) but NOT
# the compiled helpers: `xtract` is a dispatcher that execs `xtract.<platform>`, which is
# published separately per architecture. Fetch those too, or every esearch pipeline fails
# at the first xtract with "Unable to locate xtract executable".
ENV PATH=/opt/edirect:$PATH
RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) edirect_platform=Linux ;; \
      arm64) edirect_platform=ARM64 ;; \
      *) echo "unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    mkdir -p /opt; \
    curl -fsSL https://ftp.ncbi.nlm.nih.gov/entrez/entrezdirect/edirect.tar.gz \
      | tar -xz -C /opt; \
    for tool in xtract transmute rchive; do \
      curl -fsSL -o /tmp/"$tool".gz \
        "https://ftp.ncbi.nlm.nih.gov/entrez/entrezdirect/$tool.$edirect_platform.gz"; \
      gunzip -c /tmp/"$tool".gz > /opt/edirect/"$tool"."$edirect_platform"; \
      chmod +x /opt/edirect/"$tool"."$edirect_platform"; \
      rm -f /tmp/"$tool".gz; \
    done; \
    esearch -help >/dev/null; \
    echo '<a><b>ok</b></a>' | xtract -pattern a -element b

WORKDIR /src
COPY . /src
RUN pip install --no-cache-dir .

# htslib caches a remote file's index into the process working directory, so give it a
# writable scratch dir rather than letting it litter /src (or fail on a read-only mount).
WORKDIR /work

# Fail the build if the served toolset is not actually complete: every tool the MCP
# server advertises must be present, and the four CLI-backed ones must really be callable.
RUN set -eux; \
    python -c "\
from legumista_agent.mcp_server import build_server, _HANDLERS; \
build_server(); \
missing = {'lis_find','lis_files','lis_gene','samtools','bcftools','fasta_fetch',\
'tabix_query','ncbi_datasets','edirect','sra_runs','read_paper'} - set(_HANDLERS); \
assert not missing, missing; \
print(len(_HANDLERS), 'tools served')"; \
    python -c "\
import shutil; \
missing = [b for b in ('datasets','esearch','efetch','esummary','xtract') if not shutil.which(b)]; \
assert not missing, missing; \
print('external CLIs OK')"; \
    python -c "\
import pysam, importlib; \
importlib.import_module('pysam.bcftools'); \
print('htslib', pysam.__samtools_version__)"

# Everything is one command: start the MCP server with `legumista mcp` (stdio by default).
# Extra args (e.g. --http, --allow-write) pass straight through.
ENTRYPOINT ["legumista", "mcp"]
