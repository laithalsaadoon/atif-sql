# atif-analytics

The v2 analytics pipelines over the materialized ATIF corpus:

* **LLM pipelines** — `classify`, `conflicts`, `friction`, `perceived` —
  read the materialized corpus (`trajectory.json` steps + `edges.jsonl`,
  each stored compressed or plain),
  render session transcripts under the documented caps, and classify through
  `atif_models.domain.ports.LlmStructuredProvider` (GPT-6 or Claude 5.5
  structured outputs on bedrock-runtime; sizes classify=small, conflicts=large,
  friction=small, perceived=medium). `classify` labels `work_category` and `goal` only.
  `classify` and `conflicts` skip non-interactive sessions (`turn_audit`,
  `one_shot_job`), and `friction` and `perceived` read human turns only.
  The transcripts carry user and agent turns and tool calls with their
  arguments; each tool result is a size stub unless
  `ATIF_SQL_SESSION_TEXT_TOOL_RESULT_BODIES=true`.
* **Narrate** — `narrate` writes a plain-language account of every session
  (`analyze --narrate`; off by default). `atif_analytics.domain.narration`
  splits a session into streams (the main chain, and one per subagent by
  `agent_id`) and cuts each into windows at human turns, compactions,
  subagent launches and returns, and a size cap; each window gets one
  `session_narration` row (intent, actions, outcome, first and last step
  uuid, model id, prompt digest), each subagent one `subagent_stories` row
  with a digest shown to the window where its work ends (a queued task
  notification in `session_events` counts as the return), and each session
  one `session_stories` row. A call carries the previous window's narration and
  its last raw steps, and sees tool arguments at 1,200 characters and the
  opening of each tool result (its own bounds; the other pipelines are
  unchanged); a result the harness saved to a file is marked and read from
  the session's source archive. Each call is told what closed its window. Every session kind is eligible, the size is small
  (`ATIF_SQL_LLM_MODEL_NARRATE` and `ATIF_SQL_LLM_EFFORT_NARRATE` name a
  model and an effort for this stage alone), the output
  cap is the model's own limit, and a session that resumes continues each
  stream from its last narrated step.
* **Review** — `review` stores where the agent fell short in a session
  (`analyze --review`; off by default). It reads the session's story and
  window narrations, a triage call names the windows worth opening, and a
  review call reads those windows' steps against a nine-heading checklist
  (`atif_analytics.domain.review`). Each finding is one `session_findings`
  row: `expected`, `actual`, the checklist heading, and quotes with the
  uuids of the steps they're in. The pipeline checks every quote against the
  step it names and drops a finding whose quote isn't there, so a narration
  decides what gets read and never what gets stored. There's no severity or
  confidence column. Each reviewed session gets a `session_reviews` row with
  what was opened, stored and dropped. `ATIF_SQL_REVIEW_ROUTE=steps` reads
  every step with no narration, `ATIF_SQL_REVIEW_GOAL_FILE` adds the
  operator's goal (`examples/review-goal.example.md`), and the model
  defaults to another vendor's than narrate's
  (`ATIF_SQL_LLM_MODEL_REVIEW`, `ATIF_SQL_LLM_EFFORT_REVIEW`).
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
packages (import-linter forbidden contract); atif-cli composes it via the
`atif-sql analyze` command, handing it a session source over atif-duck's lake
(`atif_cli.lake_sessions`).

All pipelines default to `dry_run=True` and return plan dicts with
`estimate_cost_tokens` projections (cost guard per CONTRACT-V2).
