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
    meta.json                      # {session_id, source_mtime_ns, source_files: [...],
                                   #  harbor_version, converter_version, materialized_at,
                                   #  agent}
  watermark.json                   # {path: mtime_ns} across source corpus

- corpus-slug: a slug of the source root path; it IS the on-disk dir name.
  One key is reserved and not hashed: `codex` names the Codex CLI corpus.
- meta.agent: the AgentSource value ("claude-code" or "codex") the session was
  materialized from. A corpus written before this key existed reads as NULL in
  SQL and is still valid; nothing may require it to be present.
- One corpus holds ONE agent, and meta.agent is the DISCRIMINATOR that enforces
  it: materialize reads it before ghost removal and before any write, and a
  disagreement fails the pass (exit 78) with nothing removed. A meta.json with
  no agent key answers claude-code, because no corpus predating Codex support
  can hold Codex sessions. Per-agent default roots keep the two apart without
  anyone thinking about it; an explicit corpus_root is what this guard covers.
- Source discovery MUST include subagents/agent-*.jsonl AND
  subagents/workflows/wf_*/agent-*.jsonl (and any deeper future nesting: use
  rglob over the session dir filtered to *.jsonl, excluding *.meta.json).
- Quiescence: a session is (re)materialized when newest source mtime is older
  than quiesce_seconds (default 300) AND newer than its meta.source_mtime_ns.
  --force overrides.
- Parallel convert: the convert+write stage may run across a process pool
  (--workers N, default min(8, cpu_count)). The pool changes no artifact byte:
  each worker writes the same four files through the same per-session
  .staging/ dir and atomic rename, so a session is still the crash-safety
  unit, and the watermark still advances only for sessions that succeeded.
  --workers 1 is the single-process reference path.

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
3. Census + edges are derived from RAW jsonl (never from the trajectory).
4. Validation: TrajectoryValidator MUST pass post-enrichment (extra is free-form).

## atif-duck (reads corpus_root; NEVER imports atif-corpus/atif-converter)
- register(con, corpus_root): TEMP-table raw readers over trajectory.json
  (read_json), edges.jsonl, loss_report.json, meta.json + derived views above.
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
atif-sql status [--agent ...]          # corpus freshness, counts, watermark age
atif-sql query 'SQL' [--format auto|json|csv]
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
_default_*() factories read env at call time. With agent=codex the two roots re-derive to $CODEX_HOME (default
~/.codex)/sessions and ~/.atif-sql/corpus/codex; an explicitly set
ATIF_SQL_SOURCE_ROOT or ATIF_SQL_CORPUS_ROOT always wins over that
re-derivation.
