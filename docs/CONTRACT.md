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
                                   #  harbor_version, converter_version, materialized_at}
  watermark.json                   # {path: mtime_ns} across source corpus

- corpus-slug: a slug of the source root path; it IS the on-disk dir name.
- Source discovery MUST include subagents/agent-*.jsonl AND
  subagents/workflows/wf_*/agent-*.jsonl (and any deeper future nesting: use
  rglob over the session dir filtered to *.jsonl, excluding *.meta.json).
- Quiescence: a session is (re)materialized when newest source mtime is older
  than quiesce_seconds (default 300) AND newer than its meta.source_mtime_ns.
  --force overrides.

## Converter enrichment (atif-converter owns; wrap-local, NO upstream patches)
1. Staging fix: per-FILE symlinks; workflow-nested agent files staged flat into
   the harbor-visible subagents/ dir with collision-safe names.
2. Post-conversion enrichment pass over the harbor Trajectory (pure function):
   - step.extra["source_uuids"]: list of raw record uuids contributing to the step
     (join on assistant message.id / tool_use_id; user steps via content match order).
   - step.extra["is_compact_summary"] when the source record had it.
   - trajectory.extra["cache_creation_total"] surfaced from final_metrics.extra.
3. Census + edges are derived from RAW jsonl (never from the trajectory).
4. Validation: TrajectoryValidator MUST pass post-enrichment (extra is free-form).

## atif-duck (reads corpus_root; NEVER imports atif-corpus/atif-converter)
- register(con, corpus_root): TEMP-table raw readers over trajectory.json
  (read_json), edges.jsonl, loss_report.json, meta.json + derived views above.
- messages-parity view name: `steps` (one row per ATIF step) PLUS a `messages`
  compatibility view reconstructed from edges (uuid-keyed: uuid, parent_uuid,
  session_id, ts, type, is_sidechain, is_compact_summary, role via join).
- Static VIEW_SCHEMA dict + drift test, so a view rename fails a gate.
- Macro signatures are pinned against the DDL by a drift test.

## CLI (atif-cli composes; the only package importing the other five)
atif-sql convert <session.jsonl|dir>   # one-shot, prints trajectory path + loss summary
atif-sql materialize [--force] [--quiesce-seconds N]  # sync corpus
atif-sql status                        # corpus freshness, counts, watermark age
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
quiesce_seconds=300. _default_*() factories read env at call time.
