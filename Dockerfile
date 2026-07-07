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
# pysam ships manylinux wheels, so the genomics tools install without a C toolchain here.
FROM python:3.12-slim

LABEL io.modelcontextprotocol.server.name="io.github.legumeinfo/legumista"

ENV PYTHONUNBUFFERED=1

WORKDIR /src
COPY . /src
RUN pip install --no-cache-dir .

# Everything is one command: start the MCP server with `legumista mcp` (stdio by default).
# Extra args (e.g. --http, --allow-write) pass straight through.
ENTRYPOINT ["legumista", "mcp"]
