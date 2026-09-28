# atif-sql inter-package contract (v1)

Binding for all packages. Orchestrator-owned; change only via this file.

This file binds the corpus layout, the enrichment pass, the
transcript-derived view/macro surface, and the CLI shapes below. What sits on
top of them — the analytics pipelines, VSS, the model registry, the cron
lanes — is bound by `docs/CONTRACT-V2.md`.

## Scope: the transcript-derived surface
The views
sessions, messages(→steps), tool_calls, tool_results, todo_events,
todo_state_current, subagent views (subagent_spawns, subagent_steps, subagents), images,
task_creations/task_updates/tasks_state_current,
skill_invocations + macros ago, model_used, cost_estimate, tool_rank,
todo_velocity, subagent_fanout, skill_rank, skill_source_mix; CLI query/schema/status.
VSS/semantic_search and the analytics pipelines are shipped commands, bound
by CONTRACT-V2 rather than by this file — their absence from the list above
divides labour between the two contracts and does not deny they exist. Lean
proofs are out of scope for the workspace.

## Materialized corpus layout (atif-corpus writes, atif-duck reads)
<corpus_root>/                     # default: ~/.atif-sql/corpus/<corpus-slug>/
  sessions/<session_id>/
    trajectory.json.zst            # compact JSON (separators=(',',':')), ATIF-v1.7,
                                   #  one zstd frame with its content size in the header
    loss_report.json               # atif_converter LossReport.to_json()
    edges.jsonl.zst                # one line per RAW record: {uuid, parent_uuid,
                                   #  message_id, type, ts, is_sidechain,
                                   #  is_compact_summary, source_file, tool_use_ids: [..]}
    session_events.jsonl.zst       # one line per KEPT non-message record: {seq, ts,
                                   #  event_type, subtype, uuid, parent_uuid,
                                   #  tool_use_id, is_sidechain, source_file, payload,
                                   #  payload_bytes, payload_truncated}; may be empty
    meta.json                      # {session_id, source_mtime_ns, source_files: [...],
                                   #  harbor_version, converter_version, converter_schema,
                                   #  materialized_at,
                                   #  agent, source_present, source_removed_at?,
                                   #  source_archive?, columnar_schema? (old layout)}
    source/<path>.zst              # raw source archive: one zstd file per source file,
                                   #  <path> relative to the main transcript's parent
    (old layout only, until `atif-sql corpus slim` converts it:)
    trajectory.json, edges.jsonl, session_events.jsonl   # the same files, plain
    session.parquet, steps.parquet, tool_calls.parquet,  # per-session columnar
    tool_results.parquet, session_events.parquet         #  artifacts, see below
  blobs/sha256/<ab>/<sha256>.<ext> # inline attachments (images, PDFs), content-addressed,
                                   #  shared by every session, written 0444
  watermark.json                   # {path: mtime_ns} across source corpus
  empty_sessions.json              # {session_id: {converter_schema, ...}} for
                                   #  transcripts with nothing to convert
  sink_pending.json                # {session_ids: [...]}: sessions the SessionSink
                                   #  hasn't taken yet; absent when none are pending

<lake_root>/                       # default: ~/.atif-sql/lake/ (ATIF_SQL_LAKE_ROOT)
  catalog.duckdb                   # the writer's DuckLake catalog
  catalog.reader.duckdb            # read-only (0444) copy the writer publishes after each write
  data/main/<table>/agent=<agent>/corpus=<corpus>/[year=<y>/month=<m>/]*.parquet
<lake_root>.lock                   # the writer's flock, beside the root

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
  rglob over the session dir filtered to *.jsonl, excluding *.meta.json).
  The converter reads the agent-*.meta.json sidecars itself, in the same
  single pass as the transcripts (fingerprinted and re-checked the same way,
  and archived by that re-check), for subagent linkage; they carry no records
  and stay out of the census. The watermark also watches every *.meta.json
  under the session dir, so a sidecar that changes on its own re-materializes
  the session; meta.source_files lists them too.
- Nothing outside sessions/<id>/ belongs to a session: materialize never
  deletes, moves or writes anything under a shared corpus directory such as
  blobs/ except to add a new blob, and the raw source archive lives only in
  sessions/<id>/source/.
- Quiescence: a session is (re)materialized when newest source mtime is older
  than quiesce_seconds (default 300) AND either its sources moved since the
  watermark or its generation is stale (below). --force overrides the second
  half. "Now" is read after the scan, and a source mtime up to 1 s in the future
  is jitter, not a clock warning.
- Generation: meta.converter_schema is the converter's CONVERTER_SCHEMA_VERSION
  (atif_converter.domain.schema_version), and a session recording a different
  one, or none, is stale even when no source byte moved. meta.converter_version
  is the atif-sql release that converted it: provenance only. The storage
  layout is not part of the generation: a session stored the old way is as
  current as one stored compressed.
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
- Compressed artifacts: trajectory.json, edges.jsonl and session_events.jsonl
  are stored as <name>.zst, one zstd frame each, recording the decompressed
  size in its header. Decompressed, each is byte for byte the plain file an
  earlier version wrote. A reader resolves each session's stored file,
  compressed first, then plain, so a corpus holding both layouts (or one
  session holding both spellings, midway through `corpus slim`) reads to the
  same rows. Path columns name the logical file (sessions.trajectory_path is
  <session>/trajectory.json either way).
- Columnar artifacts (old layout): an earlier materialize also wrote five
  parquet files per session, a cache of what the views compute from
  trajectory.json and session_events.jsonl, claimed by meta.columnar_schema.
  materialize no longer writes them. A reader takes the columnar path for a
  session only when meta.columnar_schema equals its own version AND all of
  those files are present and non-empty; otherwise it reads the session's
  trajectory. Whatever the path, every view and macro returns the same rows.
  `atif-sql status` reports the layout and which path a corpus takes without
  the lake.
- corpus slim: the one operation that rewrites existing artifacts. It
  compresses each plain JSON artifact in place (read back and compared before
  the plain file is removed, keeping its mtime), then deletes the parquet
  files only after `lake verify` for that corpus, reading every session from
  its trajectory, is clean. Dry run unless --no-dry-run. It never rewrites
  meta.json.
- Blob store: the converter replaces every inline base64 attachment with the
  placeholder `[image sha256:<hash> <media_type> <n> bytes]` (`[file ...]` for
  a non-image) and hands the bytes to the writer, which stores each under
  blobs/sha256/<first two hex>/<hash>.<ext> BEFORE it swaps in the session
  that references it, so a published placeholder always resolves. The store
  is shared rather than per-session because a hash-named write is idempotent:
  a blob two sessions share is stored once, and re-converting a session
  rewrites nothing. Ghost removal does not collect blobs; an unreferenced blob
  costs space only. The hash and extension are validated before either
  becomes a path.

## Agents (atif-converter converts, atif-corpus discovers)
- AgentSource is a StrEnum in atif-converter with an AST-pinned twin in
  atif-corpus (the packages may not import each other). Its VALUES are the wire
  contract for the `--agent` spellings, harbor's Trajectory.agent.name,
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

## Converter (atif-converter owns the conversion; harbor's ATIF models are the contract)
0. src/ imports nothing from harbor (or litellm); both are dev dependencies.
   The ATIF data classes (harbor.models.trajectories, RFC 0001) and
   harbor.utils.trajectory_validator are vendored from harbor 0.23.0
   (Apache-2.0) as atif_converter.domain.atif and held to upstream by a
   conformance test. The raw-JSONL -> Trajectory conversion for both agents is
   ours, ported from harbor 0.23.0 and held to PARITY with it by an oracle in
   atif-converter's tests: frozen goldens per synthetic fixture, plus a
   live-corpus diff.
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
   - Attachments and typed signals (atif_converter.domain.blobs /
     domain.result_signals), both agents: inline base64 is replaced by a blob
     placeholder BEFORE conversion; after enrichment every
     observation.results[].extra may carry is_error, exit_code, interrupted
     and images, a user step's extra may carry images, every sidechain step's
     extra carries agent_id, and trajectory.extra.subagents lists each
     subagent with its parent_tool_call_id and link_source ("meta" from the
     sidecar, "tool_result" from toolUseResult.agentId). A key absent means
     the transcript did not say; nothing is guessed.
   - atif_converter.domain.schema_version.CONVERTER_SCHEMA_VERSION (currently 2) is
     bumped with any change to the converter's output for the same input.
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
- register(con, corpus_root): TEMP-table raw readers over each session's
  stored trajectory (read_json, which decompresses a .zst file itself),
  edges, loss_report.json, meta.json + derived views above. Sessions carrying
  current columnar artifacts are read with read_parquet instead of read_json,
  per session, and the two sets are unioned; the returned RawSources says
  which sessions took which path. The lake loader passes stage_columnar:
  every session without current parquet of its own is decoded in Python and
  typed into parquet staged outside the corpus, and read from there
  (RawSources.staged_session_ids), which bounds the loader's memory.
- No corpus path is statement text. The read_json readers take their glob or
  file list as a bound parameter; the read_parquet readers are relations
  built through the connection's own API and registered as views (CREATE
  VIEW can't be prepared). Every remaining f-string placeholder in a SQL
  statement is a module constant, a catalog constant, a projection
  expression, or `sql_literal(...)`, and an AST test over the SQL
  modules fails on anything else. The statements DuckDB won't prepare
  (ATTACH for the Lance store and the lake, the producer's one-row session
  projection, the lake reader's corpus filter)
  are the only places `sql_literal` still escapes a value.
- ColumnarArtifactProducer(session_dir, session_id, trajectory, events_path?)
  is a pure function of the trajectory (and of the session's events file,
  plain or compressed) that writes the parquet files with the views' own
  projection expressions, so the JSON columns are normalized exactly as
  read_json would. The lake loader stages each session through it.
- sessions view carries `agent` and `agent_version` from trajectory.agent, and
  coalesces the shapes harbor emits for working directory and git branch
  (cwds[0]/cwd, git_branches[0]/git.branch). Claude Code's lists are in
  first-seen order, so cwds[0] is where the session started. The steps view coalesces
  cache_creation_input_tokens with the Codex spelling cache_write_input_tokens.
- messages-parity view name: `steps` (one row per ATIF step) PLUS a `messages`
  compatibility view reconstructed from edges (uuid-keyed: uuid, parent_uuid,
  session_id, ts, type, is_sidechain, is_compact_summary, role via join).
- Static VIEW_SCHEMA dict + drift test, so a view rename fails a gate.
- Macro signatures are pinned against the DDL by a drift test.
- sessions view carries `corpus`, the corpus directory's name, on both read
  paths.
- DuckLakeSessionSink is the SessionSink implementation: per batch, one lake
  transaction that deletes the batch's sessions from every lake table and
  inserts their rows from the registry's raw relations over the published
  artifacts, then publishes catalog.reader.duckdb. It does nothing when the
  lake doesn't exist, rebuilds a lake whose recorded schema is stale, and
  loads a corpus the lake doesn't hold yet in full.
- register(con, corpus_root, lake=LakeReader) binds the raw relations as views
  over the lake tables (scoped to one corpus, or every corpus) instead of the
  TEMP-table readers; every view and macro above binds over them unchanged.
  The lake is attached READ_ONLY from catalog.reader.duckdb before the sandbox
  locks the connection, and the grants name each live lake file.

## CLI (atif-cli composes; the only package importing the others)
atif-sql convert <session.jsonl|dir> [--agent claude-code|codex]
                                       # --agent defaults from ATIF_SQL_AGENT
atif-sql materialize [--force] [--quiesce-seconds N] [--agent ...] [--workers N] [--no-lake]  # sync corpus
                                       # report adds rejected / rejected_session_ids,
                                       # lake_synced / lake_pending / lake_error
atif-sql status [--agent ...]          # corpus freshness, counts, watermark age
atif-sql query 'SQL' [--format auto|json|csv]
                                       # sized to the host before registration
                                       # (ATIF_SQL_QUERY_MEMORY_LIMIT / _THREADS
                                       # override); private mkdtemp spill dir;
                                       # refuses uid 0 unless ATIF_SQL_ALLOW_ROOT=1;
                                       # COPY/EXPORT/ATTACH/INSTALL/LOAD/PREPARE/EXECUTE
                                       # exit 70 sandbox_refused before execution;
                                       # never installs an extension;
                                       # --all-corpora (lake only, exit 78 without one),
                                       # --no-lake (per-session artifacts)
atif-sql lake rebuild [--corpus-root P ...]  # fresh lake, directory swap
atif-sql lake verify                   # exit 65 lake_mismatch, 78 lake_unavailable
atif-sql lake status                   # also folded into `atif-sql status`
atif-sql corpus slim [--corpus-root P ...] [--no-dry-run]
                                       # old layout -> compressed; dry run by default;
                                       # exit 65 / 78 when a corpus kept its parquet
atif-sql lake compact [--expire-older-than-days N] [--memory-limit SIZE]
                                       # default 30 days; SIZE overrides the host-derived
                                       # DuckDB budget (the writer ceiling still applies)
atif-sql embed --install-extension     # the ONE place the lance DuckDB extension
                                       # is downloaded (also done by a real embed run)
atif-sql embed --dry-run               # plan; discovery reads the lake's steps changed
                                       # since the last complete run (watermark in the
                                       # store dir), --no-lake reads every trajectory
atif-sql embed --prune-orphans [--no-dry-run]  # stored rows no lake step names;
                                       # dry run by default, exit 78 without a lake
atif-sql search 'text' [--all-corpora] [--no-lake]  # kNN joined to the lake's steps
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

All gates passed — first on a small sample, then on a larger set of sessions
across many project dirs. Neither the runners nor the recorded verdicts are
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
lake_root (default ~/.atif-sql/lake), corpus_base (default ~/.atif-sql/corpus;
where `lake rebuild` looks for corpora), lake_sync_batch_size=64 and
lake_load_batch_size=512 (sessions per lake transaction), lake_stage_workers
(processes that stage a batch from its trajectories; default min(4, cpu_count)),
lake_lock_timeout_seconds, lake_expire_days=30.
_default_*() factories read env at call time. With agent=codex the two roots re-derive to $CODEX_HOME (default
~/.codex)/sessions and ~/.atif-sql/corpus/codex; an explicitly set
ATIF_SQL_SOURCE_ROOT or ATIF_SQL_CORPUS_ROOT always wins over that
re-derivation.
