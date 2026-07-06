# Example project — *Lupinus* (Fabaceae) genomics

This is a complete, ready-to-run Legumista project: the one this tool was first built
for. It's here as a worked example of how the inputs fit together — copy it, or use it as
a reference when you `legumista init` your own topic.

**What's here (the curated inputs):**

- `legumista.yml` — identity (subject/scope/years), lexical pre-ranking terms, and the
  `llm` endpoint. Defaults to OpenRouter's free demo model so it runs on a clean clone;
  swap in a stronger hosted model or local ollama for real runs.
- `agent_state.json` — the verified anchor DOIs at the centre of the crawl.
- `ideation-goal.md` — what Phase 3 should discover.

Everything else (the corpus, reviews, ideas, downloaded PDFs) is generated and gitignored.

**Run it:**

```bash
pip install -e .                       # from the repo root, once
cd examples/lupinus
export OPENROUTER_API_KEY=sk-or-...    # or edit llm in legumista.yml (e.g. local ollama)
legumista discover                     # Phase 1 crawl + Phase 2 synthesis
legumista ideate                       # Phase 3 ideation + write-up
```

See the repo `README.md` and `PIPELINE.md` for the full guide.
