# atif-analytics

The v2 analytics pipelines over the materialized ATIF corpus:

* **LLM pipelines** — `classify`, `conflicts`, `friction`, `perceived` —
  read the materialized corpus (`trajectory.json` steps + `edges.jsonl`),
  render session transcripts under the documented caps, and classify through
  `atif_models.LlmStructuredProvider` (GPT-5.6 strict structured outputs on
  bedrock-runtime; sizes classify=luna, conflicts=sol, friction=luna,
  perceived=terra). `classify` labels `work_category` and `goal` only.
  `classify` and `conflicts` skip non-interactive sessions (`turn_audit`,
  `one_shot_job`), and `friction` and `perceived` read human turns only.
* **Authorship** — `atif_analytics.domain.authorship` decides who wrote a
  user step (`human`, `stop_hook`, `task_notification`, `harness`,
  `audit_prompt`). It's a twin of `atif_duck.domain.authorship`, pinned by
  `packages/atif-duck/tests/test_authorship_twin_pin.py`, because this
  package can't import atif-duck. The transcript renderer shows a
  machine-written user step under its author label (`[stop_hook ...]`,
  `[harness ...]`), clipped to 300 characters.
* **State** — the sqlite WAL `state.db` checkpoint + retry queue and the
  sharded parquet caches under `<corpus_root>/analytics/<name>/`.

Composition: atif-analytics imports **only** atif-models among the workspace
packages (import-linter independence contract); atif-cli composes it with
atif-duck's registered views via the `atif-sql analyze` command.

All pipelines default to `dry_run=True` and return plan dicts with
`estimate_cost_tokens` projections (cost guard per CONTRACT-V2).
