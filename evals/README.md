# Evaluations

Measures what the hardening work is for: how often a model using legumista states
something false. Every answer is scored +1 (correct), 0 (declined) or −1 (wrong), so a
model that guesses cannot outscore one that says "I could not verify this".

```bash
pip install -e ".[evals]"
ANTHROPIC_API_KEY=... python -m evals.run --model <model-id> \
    --catalog /path/to/catalog.json --repeats 3 --out runs/$(date +%F).jsonl
python -m evals.score --run runs/<date>.jsonl --baseline runs/<previous>.jsonl
```

- `cases/*.yaml` — the questions. `catalog.yaml` is answerable from the pinned catalog
  alone; `literature.yaml` needs the literature APIs; `traps.yaml` has false premises;
  `outages.yaml` makes hosts unreachable in-process (`inject.block_hosts`).
- `catalog.lock.json` — the catalog build the expectations were read from. `run.py`
  refuses any other file unless `--allow-catalog-drift` is passed.
- `run.py` — drives the model against the in-process server and records every tool call,
  its `isError` flag, and token use. A judge call labels each answer's stance (asserts or
  abstains) and, for rubric cases, its verdict. Not run in CI.
- `score.py` — grades a run and prints score, accuracy, precision, wrong rate, trap and
  outage handling, identifiers no tool returned, tool errors, and cost.

Adding a case: write down where the expected value came from (`evidence`), prefer exact
strings over a rubric, and give every trap and outage a rubric that names the wrong
answer explicitly. `tests/test_evals.py` checks the files are well formed.
