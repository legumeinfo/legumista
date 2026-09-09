"""legumista_agent — the tools legumista serves, and the MCP bridge that serves them.

  tool.py           the Tool interface every tool is built from (name, description,
                    JSON-schema parameters, read_only flag, async run, optional per-call
                    `writes(args)` classifier)
  mcp_server.py     the FastMCP bridge: assembles every family below and serves it

  tools_catalog.py  the resident LIS catalog (lis_survey, lis_lineage) and the
                    CatalogController the lis_* tools read through
  tools_lis.py      LIS Data Store discovery and gene lookup
  tools_mine.py     InterMine PathQueries against the LIS mines
  tools_native.py   literature (OpenAlex/Crossref/Europe PMC), NCBI CLIs, web search
  tools_local.py    web_fetch
  tools_pysam.py    samtools/bcftools/tabix over local and remote indexed files

Every tool is read-only unless the server was started with --allow-write, which only the
genomics dispatchers act on.
"""
