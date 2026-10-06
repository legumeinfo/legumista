# files — htslib reads of indexed files: samtools, bcftools, fasta_fetch, tabix_query, tabix_index

- A file argument is a workspace path or an http(s) URL whose sibling index exists
  (`.fai`, `.bai`, `.csi`, `.tbi`). "Missing index" means the file is not queryable;
  `lis_files` says which LIS files are.
- Regions are 1-based, inclusive: `seqid`, `seqid:start` or `seqid:start-end`
  (`glyma.Wm82.gnm4.Gm12:1000-2000`).
- If a tool reports pysam absent, the tools are unavailable on this server; do not retry.

## Tools
- `samtools` and `bcftools` take an argv list, `{args: [subcommand, ...]}`, and return
  stdout. Header first to learn contig and sample names: `["view","-H",<bam>]`,
  `["view","-h",<vcf>]`. Then, for example, `["view","-c",<bam>,<region>]`,
  `["coverage","-r",<region>,<bam>]`, or
  `["query","-f","%CHROM\t%POS\t%REF\t%ALT\n","-r",<region>,<vcf>]`.
- `fasta_fetch`: `{path, region}`; a region may be a whole sequence name, such as a
  protein's model name.
- `tabix_query`: `{path, region?, max_records?}`; without a region it lists contigs.
- `tabix_index` writes an index.

## Reads and writes
Read subcommands and options work by default. Anything that writes a file is refused
unless the server allows writes: `sort`, `index`, `markdup`, `call`, `norm`,
`tabix_index`, or an output option (`-o`, `view -U`, `fastq -1`, …), or any option outside
a read subcommand's allowlist. On "writes are disabled", report that the step needs write
access rather than retrying with other flags. Paths are confined to the workspace, and
`##idx##` paths are refused: pass the data file alone and its index is found beside it.
