Write a rigorous literature review of the {{SUBJECT}} corpus this project has collected. Follow every rule in your system prompt, especially the evidential-integrity directives (cite only what is in the corpus below; never invent a source, DOI, or finding). The corpus digest is inlined below and is your citation base. You may use the research tools to verify or sharpen the review as you write — e.g. `read_paper`/`fulltext_grep` on a corpus DOI to confirm a claim or pull an exact statistic, or `openalex_by_doi` to check a citation — but cite ONLY papers present in the digest; tools are for verification, not for finding new sources to cite.

FRAMING/ANGLE: {{ANGLE}}

Produce the review with the required sections: Consensus Overview, Key Thematic Pillars, Friction Points, Research Gaps, and References (APA 7th, built from the authors/year/venue/DOI in the digest). Cite ONLY papers present in the digest — do not invent sources, DOIs, or findings not supported by the corpus. Weight the synthesis toward higher-similarity papers. Output ONLY the review in Markdown, with no preamble.

===== CORPUS DIGEST ({{N}} papers, ordered closest-to-core first) =====
{{DIGEST}}
===== END CORPUS DIGEST =====
