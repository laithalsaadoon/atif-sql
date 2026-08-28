# atif-analytics

The v2 analytics pipelines over the materialized ATIF corpus:

* **LLM pipelines** — `classify`, `trajectory`, `conflicts`, `friction`,
  `perceived` — read the materialized corpus (`trajectory.json` steps +
  `edges.jsonl`), render session transcripts under the documented caps, and
  classify through `atif_models.LlmStructuredProvider` (GPT-5.6 strict
  structured outputs on bedrock-runtime; sizes per CONTRACT-V2:
  classify/trajectory/perceived=terra, conflicts=sol, friction=luna).
* **Structural pipelines** — `cluster` (UMAP 50d + HDBSCAN), `terms`
  (c-TF-IDF), `community` (mutual-kNN + Leiden CPM) — hyperparameters pinned
  by CONTRACT-V2, reading embeddings from the lance store written by
  atif-embed (skipping gracefully when the store is absent).
* **State** — the sqlite WAL `state.db` checkpoint + retry queue and the
  sharded parquet caches under `<corpus_root>/analytics/<name>/`.

Composition: atif-analytics imports **only** atif-models among the workspace
packages (import-linter independence contract); atif-cli composes it with
atif-duck's registered views via the `atif-sql analyze` command.

All pipelines default to `dry_run=True` and return plan dicts with
`estimate_cost_tokens` projections (cost guard per CONTRACT-V2).
