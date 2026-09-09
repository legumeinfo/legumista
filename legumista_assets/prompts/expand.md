You are writing a detailed, technically rigorous research proposal for ONE idea, grounded strictly in the corpus below. The corpus is inlined below and is your citation base. You may use the research tools to deepen the proposal — e.g. `read_paper`/`read_paper (with `pattern`)` on a corpus DOI for exact methods/parameters, or `ncbi_assembly_status`/`sra_runs` to confirm a dataset or genome the plan depends on actually exists — but cite ONLY papers present in the corpus digest; tools are for verification, not for adding new citations.

The idea must be {{DOMAIN_CONSTRAINT}}. Write a proposal a domain expert would take seriously: concrete methods, named tools and parameters, specific datasets, and claims corroborated by SPECIFIC findings from the corpus, each cited inline by first-author + year and DOI (e.g. "[Garg et al. 2022, 10.1111/tpj.xxxxx]"). Cite ONLY papers present in the corpus digest; never invent a DOI, number, or finding. Where the corpus lacks support, say so explicitly rather than guessing.

Write GitHub-flavoured Markdown with exactly these sections (use ## headings):
## Summary
## Background & motivation   (cite specific corpus findings that make this timely)
## Specific aim & hypothesis
## Data & inputs             (name the exact datasets/resources/accessions from the corpus)
## Approach                  (step-by-step methodology: tools/methods, params, order)
## Novelty vs. prior work    (contrast with specific corpus papers, cited)
## Feasibility               (data/resource availability, scale, what's needed)
## Risks & mitigations
## Expected outcomes & validation  (how results would be evaluated/benchmarked)
## References                (APA-style, ONLY corpus DOIs actually cited above)

Output ONLY the markdown proposal — no preamble and no code fences around the whole document.

===== THE IDEA (structured record from the ideation loop) =====
{{IDEA_JSON}}

===== SYNTHESIS OF THE CORPUS =====
{{SYNTH}}

===== CORPUS DIGEST (evidence base — cite from here only) =====
{{CORPUS}}
===== END CORPUS DIGEST =====
