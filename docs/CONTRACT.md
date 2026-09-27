# atif-sql inter-package contract (v1)

Binding for all packages. Orchestrator-owned; change only via this file.

This file binds the corpus layout, the enrichment pass, the
transcript-derived view/macro surface, and the CLI shapes below. What sits on
top of them — the analytics pipelines, VSS, the model registry, the cron
lanes — is bound by `docs/CONTRACT-V2.md`.

## Scope: the transcript-derived surface
The views
sessions, messages(→steps), tool_calls, tool_results, todo_events,
todo_state_current, subagent views, task_creations/task_updates/tasks_state_current,
skill_invocations + macros ago, model_used, cost_estimate, tool_rank,
todo_velocity, subagent_fanout, skill_rank, skill_source_mix; CLI query/schema/status.
VSS/semantic_search and the analytics pipelines are shipped commands, bound
by CONTRACT-V2 rather than by this file — their absence from the list above
divides labour between the two contracts and does not deny they exist. Lean
proofs are out of scope for the workspace.

## Materialized corpus layout (atif-corpus writes, atif-duck reads)
<corpus_root>/                     # default: ~/.atif-sql/corpus/<corpus-slug>/
  sessions/<session_id>/
    trajectory.json                # compact JSON (separators=(',',':')), ATIF-v1.7
    loss_report.json               # atif_converter LossReport.to_json()
    edges.jsonl                    # one line per RAW record: {uuid, parent_uuid,
                                   #  message_id, type, ts, is_sidechain,
                                   #  is_compact_summary, source_file, tool_use_ids: [..]}
    session_events.jsonl           # one line per KEPT non-message record: {seq, ts,
                                   #  event_type, subtype, uuid, parent_uuid,
                                   #  tool_use_id, is_sidechain, source_file, payload,
                                   #  payload_bytes, payload_truncated}; may be empty
    meta.json                      # {session_id, source_mtime_ns, source_files: [...],
                                   #  harbor_version, converter_version, converter_schema,
                                   #  materialized_at,
                                   #  agent, source_present, source_removed_at?,
                                   #  source_archive?, columnar_schema}
    source/<path>.zst              # raw source archive: one zstd file per source file,
                                   #  <path> relative to the main transcript's parent
    session.parquet                # typed columnar artifacts (optional, see below):
    steps.parquet                  #  the trajectory header and the steps, tool_calls,
    tool_calls.parquet             #  tool_results views' rows for this one session,
    tool_results.parquet           #  written 0444, claimed by meta.columnar_schema
    session_events.parquet         #  (schema 2+: the session_events view's rows)
  watermark.json                   # {path: mtime_ns} across source corpus
  empty_sessions.json              # {session_id: {converter_schema, ...}} for
                                   #  transcripts with nothing to convert

- corpus-slug: a slug of the source root path; it IS the on-disk dir name.
  One key is reserved and not hashed: `codex` names the Codex CLI corpus.
- session_id boundary: the id comes from the transcript filename and is the
  one piece of outside text that becomes a corpus path. It must match
  `^[A-Za-z0-9][A-Za-z0-9._-]*$` and be at most 255 characters (every id
  Claude Code and Codex produce does). The scanner skips a transcript whose
  id fails, logs the reason, and reports it under `rejected` /
  `rejected_session_ids`; nothing from it is written, and a corpus dir an
  older version wrote under such a name is left alone, never marked.
  atif-duck applies the SAME rule before it builds any path: a session dir
  whose name fails registers nothing and is reported in
  `RawSources.rejected_session_ids`. The pattern lives once per package
  (`atif_corpus.domain.session_id`, `atif_duck.domain.session_id`), twinned
  the way `AgentSource` is, with a test on each side pinning the other's copy.
- meta.agent: the AgentSource value ("claude-code" or "codex") the session was
  materialized from. A corpus written before this key existed reads as NULL in
  SQL and is still valid; nothing may require it to be present.
- One corpus holds ONE agent, and meta.agent is the DISCRIMINATOR that enforces
  it: materialize reads it before marking any session and before any write, and
  a disagreement fails the pass (exit 78) with nothing touched. A meta.json with
  no agent key answers claude-code, because no corpus predating Codex support
  can hold Codex sessions. Per-agent default roots keep the two apart without
  anyone thinking about it; an explicit corpus_root is what this guard covers.
- Source discovery MUST include subagents/agent-*.jsonl AND
  subagents/workflows/wf_*/agent-*.jsonl (and any deeper future nesting: use
  rglob over the session dir filtered to *.jsonl). The watermark also watches
  every *.meta.json under the session dir (the agent-*.meta.json sidecars the
  converter reads), so a sidecar that changes on its own re-materializes the
  session; meta.source_files lists them too.
- Nothing outside sessions/<id>/ belongs to a session: materialize never
  deletes, moves or writes anything under a shared corpus directory such as
  blobs/, and the raw source archive lives only in sessions/<id>/source/.
- Quiescence: a session is (re)materialized when newest source mtime is older
  than quiesce_seconds (default 300) AND either its sources moved since the
  watermark or its generation is stale (below). --force overrides the second
  half. "Now" is read after the scan, and a source mtime up to 1 s in the future
  is jitter, not a clock warning.
- Generation: meta.converter_schema is the converter's CONVERTER_SCHEMA_VERSION
  (atif_converter.domain.schema_version), and a session recording a different
  one, or none, is stale even when no source byte moved. meta.converter_version
  is the atif-sql release that converted it: provenance only. A columnar pass
  also expects meta.columnar_schema to equal the reader's schema version, so a
  schema bump or a --no-columnar session re-converts. Keys are compared only
  when expected; a missing expected key is stale.
- Retention: a session is NEVER deleted. When its main transcript vanishes from
  the scan (a genuine FileNotFoundError, not a stat failure, and not during a
  pass that couldn't list a source directory), its artifacts are kept and its
  meta.json is rewritten once with source_present: false and source_removed_at
  (that pass's materialized_at). meta.source_present is true on every session
  converted from a live source; a meta.json without the key predates this
  rule and reads as present. A reappearing source is converted again from the
  live file. The report's `removed` / `removed_session_ids` are the sessions
  newly marked that pass; `retained` counts every session kept without a
  source.
- Raw source archive: every live conversion writes source/<path>.zst for each
  source file (main transcript, side-file transcripts, and every other regular
  file under the session's side dir; symlinks are skipped), from the exact bytes
  the converter parsed, staged and swapped with the other artifacts.
  meta.source_archive = {codec: "zstd", source_dir, main, files: [{path, size,
  sha256}]}, where source_dir is the main transcript's parent relative to the
  source root. A source-removed session with a stale generation is re-converted
  from its restored archive (reported as `from_archive`), keeping its original
  source_* keys and its archive. A session retained before archives existed
  keeps its artifacts and can't be re-converted.
- Empty sessions: a transcript with no convertible record is recorded in
  empty_sessions.json keyed by session id with the generation it was checked
  under; its watermark advances and it's reported as `empty`, not `failed`.
  A write to it or a new converter schema tries it again.
- Parallel convert: the convert+write stage may run across a process pool
  (--workers N, default min(8, cpu_count)). The pool changes no artifact byte:
  each worker writes the same files through the same per-session
  .staging/ dir and atomic rename, so a session is still the crash-safety
  unit, and the watermark still advances only for sessions that succeeded.
  --workers 1 is the single-process reference path.
- Columnar artifacts: the five parquet files are a query-time cache of what the
  views compute from trajectory.json and session_events.jsonl, never a source
  of truth. materialize
  writes them by default through the ArtifactProducer port (atif-corpus
  declares the port, atif-duck implements it, atif-cli plugs them together);
  they're staged and swapped with the JSON artifacts, so a session has all of
  them or none. meta.columnar_schema names the schema version they were
  written against (currently 2; 2 added session_events.parquet). A reader takes
  the columnar path for a session only when meta.columnar_schema equals its own
  version AND all five files are
  present and non-empty; otherwise it reads trajectory.json for that session.
  A corpus written before this key existed, or with --no-columnar, stays valid
  and answers every query from JSON. Whatever the path, every view and macro
  returns the same rows. `atif-sql status` reports which path a corpus takes.

## Two agents (atif-converter converts, atif-corpus discovers)
- AgentSource is a StrEnum in atif-converter with an AST-pinned twin in
  atif-corpus (the packages may not import each other). Its VALUES are the wire
  contract three ways: the `--agent` spellings, harbor's Trajectory.agent.name,
  and meta.agent above.
- Claude Code layout: <source_root>/<project>/<session>.jsonl, transcript depth
  1, side files present (subagents/, *.meta.json).
- Codex layout: $CODEX_HOME/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl,
  transcript depth 3, no side files. session_id is the trailing UUID of the
  rollout filename, so it survives the date nesting.
- One Codex rollout converts to one trajectory, by construction: the converter
  reads the one file it is given.
- Codex fidelity gaps are their own enum (CodexFidelityGap), all values
  namespaced `codex_*` so one loss_report.gaps_observed array can carry both
  agents' gaps without collision.

## Converter (atif-converter owns the conversion; harbor supplies the contract)
0. harbor is used for its PUBLIC surface only: the ATIF data classes in
   harbor.models.trajectories (RFC 0001) and harbor.utils.trajectory_validator.
   The raw-JSONL -> Trajectory conversion for both agents is ours, ported from
   harbor 0.22.0 (Apache-2.0) and held to PARITY with it by an oracle in
   atif-converter's tests: frozen goldens per synthetic fixture, plus a
   live-corpus diff. Nothing under harbor.agents may be imported from src/.
1. Side-file discovery: every *.jsonl under <session-stem>/ (including
   workflow-nested subagents/workflows/wf_*/agent-*.jsonl, which harbor's own
   discovery cannot see) is read, and named with its nested path parts joined
   by `__` so two files sharing a basename never collide.
2. Post-conversion enrichment pass over the Trajectory (pure function):
   - step.extra["source_uuids"]: list of raw record uuids contributing to the step
     (join on assistant message.id / tool_use_id; user steps via content match order).
   - step.extra["is_compact_summary"] when the source record had it.
   - trajectory.extra["cache_creation_total"] surfaced from final_metrics.extra.
   - Codex enrichment attributes agent steps by a re-derived api_call_id rather
     than by message text, because harbor drops empty text parts and an empty
     assistant message can never be placed by matching. Tool records join on
     call_id; user and system steps are positional with a text cross-check that
     stops at the first mismatch and records enrichment_truncated_at_step.
3. Census + edges + session events are derived from RAW jsonl (never from the
   trajectory). session_events keeps, as rows and NOT as steps: Claude Code
   hook attachments (hook_*), queued_command, the system subtypes
   stop_hook_summary / api_error / compact_boundary / model_refusal_fallback,
   and cost-state / mode / permission-mode records; Codex compacted records
   and event_msg turn_aborted / error / stream_error / context_compacted /
   warning. Payloads are bounded to 8 KB (payload_bytes keeps the original
   size). loss_report.records_captured counts them, and
   records_converted + records_captured + records_dropped = records_total.
   The last Claude Code cost-state's totalCostUSD lands in
   final_metrics.extra.reported_cost_usd; total_cost_usd is NULL when any
   priced step's model has no rates (never a fabricated $0).
4. Validation: TrajectoryValidator MUST pass post-enrichment (extra is free-form).

## atif-duck (reads corpus_root; NEVER imports atif-corpus/atif-converter)
- register(con, corpus_root): TEMP-table raw readers over trajectory.json
  (read_json), edges.jsonl, loss_report.json, meta.json + derived views above.
  Sessions carrying current columnar artifacts are read with read_parquet
  instead of read_json, per session, and the two sets are unioned; the
  returned RawSources says which sessions took which path.
- No corpus path is statement text. The read_json readers take their glob or
  file list as a bound parameter; the read_parquet readers are relations
  built through the connection's own API and registered as views (CREATE
  VIEW can't be prepared). Every remaining f-string placeholder in a SQL
  statement is a module constant, a catalog constant, a projection
  expression, or `sql_literal(...)`, and an AST test over the four SQL
  modules fails on anything else. The two statements DuckDB won't prepare
  (ATTACH for the Lance store, the producer's one-row session projection)
  are the only places `sql_literal` still escapes a value.
- ColumnarArtifactProducer(session_dir, session_id, trajectory) is the
  ArtifactProducer implementation: a pure function of the trajectory (and of
  the staged session_events.jsonl beside it) that writes the five parquet files with the views' own projection expressions,
  so the JSON columns are normalized exactly as read_json would.
- sessions view carries `agent` and `agent_version` from trajectory.agent, and
  coalesces the two shapes harbor emits for working directory and git branch
  (cwds[0]/cwd, git_branches[0]/git.branch). The steps view coalesces
  cache_creation_input_tokens with the Codex spelling cache_write_input_tokens.
- messages-parity view name: `steps` (one row per ATIF step) PLUS a `messages`
  compatibility view reconstructed from edges (uuid-keyed: uuid, parent_uuid,
  session_id, ts, type, is_sidechain, is_compact_summary, role via join).
- Static VIEW_SCHEMA dict + drift test, so a view rename fails a gate.
- Macro signatures are pinned against the DDL by a drift test.

## CLI (atif-cli composes; the only package importing the other five)
atif-sql convert <session.jsonl|dir> [--agent claude-code|codex]
                                       # --agent defaults from ATIF_SQL_AGENT
atif-sql materialize [--force] [--quiesce-seconds N] [--agent ...] [--workers N]  # sync corpus
                                       # report adds rejected / rejected_session_ids
atif-sql status [--agent ...]          # corpus freshness, counts, watermark age
atif-sql query 'SQL' [--format auto|json|csv]
                                       # sized to the host before registration
                                       # (ATIF_SQL_QUERY_MEMORY_LIMIT / _THREADS
                                       # override); private mkdtemp spill dir;
                                       # refuses uid 0 unless ATIF_SQL_ALLOW_ROOT=1;
                                       # COPY/EXPORT/ATTACH/INSTALL/LOAD/PREPARE/EXECUTE
                                       # exit 70 sandbox_refused before execution;
                                       # never installs an extension
atif-sql embed --install-extension     # the ONE place the lance DuckDB extension
                                       # is downloaded (also done by a real embed run)
atif-sql schema                        # static, <50ms, no duckdb bind

## Parity oracle (satisfied and retired)
The migration oracle compared this stack against a prior implementation over
frozen byte-identical session snapshots, with zero tolerance on the counting
gates: (a) equal session set; (b) equal per-session token sums
(input/output/cache_read/cache_write); (c) equal tool_calls per session;
(d) equal skill_invocations per session; (e) subagent transcript coverage a
SUPERSET of the prior implementation (workflow-nested files are visible);
(f) enrichment covers >= 99% of assistant uuids; (g) sidechain step token
sums equal an independent raw subagent-file sum.

All gates passed — 6/6 on a 20-session sample, then 7/7 on 128 sessions
across 41 project dirs. Neither the runners nor the recorded verdicts are
tracked here. Parity is not a standing gate: the comparison target is not a
dependency of this repo, so re-running the oracle would need an install this
repo neither declares nor provides.

## Settings (env prefix ATIF_SQL_)
source_root (default CLAUDE_CONFIG_DIR~/.claude /projects), corpus_root,
quiesce_seconds=300, agent=claude-code (every --agent command reads it),
materialize_workers=min(8, cpu_count) (materialize's pool size; 1 = single process).
ATIF_SQL_QUERY_MEMORY_LIMIT (DuckDB size literal, e.g. 6GB) and ATIF_SQL_QUERY_THREADS
override query's host-derived cap and thread count; ATIF_SQL_ALLOW_ROOT=1 lets
query/search/analyze run as uid 0 (a warning is logged).
_default_*() factories read env at call time. With agent=codex the two roots re-derive to $CODEX_HOME (default
~/.codex)/sessions and ~/.atif-sql/corpus/codex; an explicitly set
ATIF_SQL_SOURCE_ROOT or ATIF_SQL_CORPUS_ROOT always wins over that
re-derivation.
