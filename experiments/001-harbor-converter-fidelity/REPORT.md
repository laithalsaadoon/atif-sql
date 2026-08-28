# Experiment 001 — harbor converter fidelity on the live corpus (REPORT)

Executed 2026-08-22 against a 20-session stratified sample of one live project
dir, `$CLAUDE_CONFIG_DIR/projects/<project-slug>`
(sample ids in `/tmp/atif-sample-sessions.txt` at run time; sizes 20 KB – 41.6 MB).
Every session was converted through
`atif_converter.application.convert_and_audit.convert_and_audit` and validated
with harbor's `TrajectoryValidator`. Per-session outputs live in `out/<session-id>/`
(`trajectory.json`, `loss_report.json`, `meta.json`) — gitignored.

Sessions are named `session-A` through `session-T`: stable pseudonyms, assigned
in order of first mention, so a claim about one session can be followed across
the tables below. The real ids are transcript identifiers from the machine the
sample was drawn on. They are not reproducible by a reader and resolve to
nothing outside that machine, so publishing them would add no evidence — every
number here stands on the counts, not on which session produced them.

Headline: **20/20 conversions succeeded, 20/20 trajectories passed validation,
zero timeouts** (max wall-clock 4.2 s per session with a 600 s budget). The
census attributes **14,948 of 63,301 raw records (23.6%) as dropped** — but see
the taxonomy below: most of that mass is `attachment` and UI-state records, not
conversation content.

## 1. Summary table

`other*` = system + queue-operation + mode + last-prompt + summary + unrecognized
types (`pr-link`, `started`, `result` were the unrecognized types observed).
"Subagents conv/found" counts side-files harbor's `rglob("subagents/*.jsonl")`
discovery converts vs all side-files found; "WF files" are workflow-nested
side-files (`subagents/workflows/wf_*/*.jsonl`) invisible to harbor (gap 1).
Dropped % is per the census upper bound (main file + all side-files, including
workflow-nested ones).

| Session | Raw MB | user | asst | attach | other* | Steps | Dropped (n, %) | Subagents conv/found | WF files | Valid | Wall s | Out MB |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|
| `session-A` | 11.08 | 1324 | 2478 | 640 | 369 | 420 | 1009 (21.0%) | 3/41 | 38 | PASS | 2.58 | 23.89 |
| `session-B` | 10.89 | 6191 | 10241 | 2679 | 1296 | 4457 | 3975 (19.5%) | 31/117 | 86 | PASS | 4.20 | 30.81 |
| `session-C` | 3.47 | 509 | 687 | 248 | 132 | 53 | 380 (24.1%) | 0/50 | 50 | PASS | 2.54 | 5.45 |
| `session-D` | 17.92 | 681 | 1034 | 425 | 249 | 532 | 674 (28.2%) | 11/11 | 0 | PASS | 2.69 | 45.54 |
| `session-E` | 1.21 | 178 | 312 | 165 | 61 | 137 | 226 (31.6%) | 7/7 | 0 | PASS | 2.39 | 1.88 |
| `session-F` | 0.56 | 85 | 149 | 36 | 15 | 64 | 51 (17.9%) | 6/6 | 0 | PASS | 2.55 | 0.93 |
| `session-G` | 19.93 | 2501 | 4054 | 569 | 109 | 2139 | 678 (9.4%) | 17/17 | 0 | PASS | 3.47 | 52.22 |
| `session-H` | 41.59 | 666 | 1138 | 600 | 463 | 640 | 1063 (37.1%) | 8/8 | 0 | PASS | 2.73 | 65.95 |
| `session-I` | 40.34 | 2230 | 3771 | 1113 | 318 | 1989 | 1431 (19.3%) | 15/15 | 0 | PASS | 3.61 | 88.21 |
| `session-J` | 37.21 | 602 | 924 | 861 | 314 | 588 | 1175 (43.5%) | 4/4 | 0 | PASS | 2.59 | 83.59 |
| `session-K` | 25.58 | 679 | 1130 | 1451 | 503 | 645 | 1954 (51.9%) | 10/10 | 0 | PASS | 2.53 | 45.35 |
| `session-L` | 24.89 | 1876 | 3190 | 1224 | 478 | 1605 | 1702 (25.1%) | 20/20 | 0 | PASS | 3.27 | 51.92 |
| `session-M` | 1.02 | 311 | 512 | 72 | 52 | 227 | 124 (13.1%) | 6/6 | 0 | PASS | 2.43 | 2.40 |
| `session-N` | 1.34 | 247 | 428 | 153 | 73 | 209 | 226 (25.1%) | 6/6 | 0 | PASS | 2.45 | 2.72 |
| `session-O` | 1.02 | 62 | 152 | 195 | 49 | 60 | 244 (53.3%) | 0/0 | 0 | PASS | 2.32 | 0.71 |
| `session-P` | 0.04 | 1 | 2 | 4 | 4 | 2 | 8 (72.7%) | 0/0 | 0 | PASS | 2.24 | 0.01 |
| `session-Q` | 0.02 | 1 | 1 | 4 | 3 | 2 | 7 (77.8%) | 0/0 | 0 | PASS | 2.36 | 0.01 |
| `session-R` | 0.02 | 1 | 1 | 4 | 3 | 2 | 7 (77.8%) | 0/0 | 0 | PASS | 2.50 | 0.01 |
| `session-S` | 0.02 | 1 | 1 | 4 | 3 | 2 | 7 (77.8%) | 0/0 | 0 | PASS | 2.21 | 0.01 |
| `session-T` | 0.02 | 1 | 1 | 4 | 3 | 2 | 7 (77.8%) | 0/0 | 0 | PASS | 2.44 | 0.01 |
| **Total (20)** | **238.18** | | | | | | **14948 (23.6%)** | | | **20/20 PASS** | | **501.62** |

Notes on the table (all measured, not extrapolated):

- Aggregate census: 63,301 raw records, 48,353 attributed as converted
  (user + assistant), 14,948 dropped. "Converted" counts raw records, not ATIF
  steps — harbor bundles multiple assistant events sharing one `message.id`
  into one step by design.
- **Output inflation:** total trajectory output (501.62 MB) is 2.1x the raw
  input (238.18 MB); peak single output is 88.21 MB (`session-I`, raw 40.34 MB).
  Cause: `json.dumps(indent=2)` in the runner plus subagent transcripts inlined
  into the flat step list. Plan disk/DB budgets accordingly.
- The five ~20 KB sessions (bottom rows) are single-turn queue-operation
  sessions; their 72–78% drop rate is 7–8 non-message records around 2–3
  message records, so the high percentage is an artifact of tiny denominators.
- Validation: zero `TrajectoryValidator` errors across all 20 sessions —
  nothing to quote verbatim.
- No conversion failures, no timeouts: every session finished in 2.2–4.2 s
  wall-clock under a 600 s per-session `timeout`.

## 2. Fidelity-gap observations (per `atif_converter.domain.fidelity.FidelityGap`)

| Gap | Sessions observed | Concrete example |
|---|---|---|
| 1 `workflow_subagents_missed` | 3/20 (`session-A`, `session-B`, `session-C`) | `session-C`: all 50 of its side-files are workflow-nested (e.g. `session-C-…/subagents/workflows/wf_synthetic/agent-synthetic.jsonl`); 1,243 records across them are invisible to harbor — 0 of the session's subagent transcripts made it into the trajectory. |
| 2 `non_message_records_dropped` | 20/20 | `session-K`: 1,451 `attachment` + 503 other non-message records dropped (51.9% of the session's raw records) — the largest attachment-heavy drop in the sample. Three record types in the wild are not even in the RecordType taxonomy: `pr-link` (265), `started` (166), `result` (132) across the 3 workflow sessions. |
| 3 `parent_chain_flattened` | 20/20 (structural) | `session-B`: 498 parentUuids have >1 child (e.g. `uuid-parent-with-two-children` has 2 children); every one of those branch/rewind points is linearized by timestamp sort in the output. |
| 4 `subagents_inlined` | 13/20 | `session-F`: 48 of its 64 steps are subagent steps flagged only via `extra.is_sidechain: true`; the trajectory has no `subagent_trajectories` key, so main-thread vs subagent structure must be reconstructed downstream. |
| 5 `compact_summary_unhandled` | 20/20 (structural attribution); 1/20 with an actual compaction record | `session-B`: one record with `isCompactSummary: true` (uuid `uuid-compact-summary-record`, type `user`) is converted as an ordinary user step — a query counting "real" user turns would miscount it. |
| 6 `cache_split_partial` | 20/20 (structural) | `session-F`: `final_metrics.total_cached_tokens` = 3,214,419 is cache **read** only; the 449,730 cache **creation** tokens survive only in `final_metrics.extra.total_cache_creation_input_tokens`. A consumer reading only the typed fields under-sees ~12% of the priced input tokens. |
| 7 `uuid_not_preserved` | 20/20 (structural) | `session-F` step[0]: `extra` keys are exactly `['is_sidechain']` — no event uuid anywhere in any step, so step→raw-record identity joins are impossible without re-deriving order heuristically. |

Gaps 3, 5, 6, 7 are attributed structurally by `convert_and_audit` (every
harbor 0.22.0 conversion has them); the examples above confirm each one is
real on this corpus, not just theoretical. For gap 5 note the asymmetry:
attribution says 20/20 but only one session in the sample actually contained a
compaction summary.

## 3. Cost/token fidelity spot-check (2 sessions)

Method: direct sum over raw JSONL `message.usage` fields (main file + the
side-files harbor can see), compared against the trajectory's `final_metrics`.
Two summation modes: naive (every usage record) and deduplicated (keep the
**last** usage per assistant `message.id`, matching harbor's logic — Claude
Code streams accumulate usage across chunks of one API message).

| Session | Field | final_metrics | Raw dedup sum | Delta | Raw naive sum |
|---|---|---:|---:|---:|---:|
| `session-F` | prompt (input+cache_read+cache_creation) | 3,665,820 | 3,665,820 | **0** | 10,796,332 |
| `session-F` | completion | 53,572 | 53,572 | **0** | 125,628 |
| `session-F` | cached (cache_read) | 3,214,419 | 3,214,419 | **0** | 9,508,560 |
| `session-F` | cache_creation (extra only) | 449,730 | 449,730 | **0** | 1,282,779 |
| `session-E` | prompt | 11,868,786 | 11,868,786 | **0** | 29,575,845 |
| `session-E` | completion | 107,059 | 107,059 | **0** | 212,716 |
| `session-E` | cached (cache_read) | 10,759,977 | 10,759,977 | **0** | 26,039,497 |
| `session-E` | cache_creation (extra only) | 1,106,629 | 1,106,629 | **0** | 3,527,958 |

Explanation: harbor's totals are **exact** relative to the raw transcript once
the `message.id` dedup is applied; the naive per-record sum overcounts ~2.4–3x
because each streaming chunk of an assistant message repeats (accumulated)
usage. The cache split caveat (gap 6) holds: `total_prompt_tokens` correctly
folds cache_read + cache_creation in, but the typed `total_cached_tokens`
field carries cache_read only, and cache_creation exists solely in
`final_metrics.extra`. Cost on both sessions came from
`cost_source: litellm_estimate` (no `result` event in these transcripts'
stream-json), so `total_cost_usd` is a litellm price-table estimate, not a
billing record.

## 4. Verdict

Per-gap: **extend-upstream** = worth a harbor PR/issue; **wrap-locally** =
handle in atif-converter staging/enrichment and keep the fidelity policy
honest.

| # | Gap | Verdict | One-line recommendation |
|---|---|---|---|
| 1 | `workflow_subagents_missed` | **Wrap locally** | Stage workflow-nested files into a harbor-visible `subagents/` layout at symlink time (the staging layer in `harbor_adapter._stage_session` already owns this exact surface); an upstream glob fix is welcome but not worth blocking on. |
| 2 | `non_message_records_dropped` | **Wrap locally** | ATIF is a message-step format — attachments/queue-ops don't belong in it; ingest the census `record_counts` as first-class metadata alongside each trajectory, and add `pr-link`/`started`/`result` to the RecordType taxonomy. |
| 3 | `parent_chain_flattened` | **Wrap locally** | Branch/rewind structure is a SQL-side join on raw `parentUuid`; store the raw JSONL (or a uuid→parentUuid edge table) next to the trajectory rather than teaching harbor tree semantics. |
| 4 | `subagents_inlined` | **Extend upstream** | `extra.is_sidechain` preserves the partition losslessly, so tolerate short-term, but nested `subagent_trajectories` is the ATIF-native shape — file the upstream issue and re-evaluate at the 0.23 bump. |
| 5 | `compact_summary_unhandled` | **Extend upstream** | A one-line `isCompactSummary` passthrough into `step.extra` upstream is trivial and rare in the wild (1/20 sessions); meanwhile flag it locally from the raw record when enriching. |
| 6 | `cache_split_partial` | **Tolerate / wrap locally** | Totals are exact (spot-check delta 0); just make atif-sql's ingestion read `extra.total_cache_creation_input_tokens` and never treat `total_cached_tokens` as the full cache picture. |
| 7 | `uuid_not_preserved` | **Extend upstream** | Step→record identity cannot be reconstructed after the fact; propose `step.extra["uuid"]` upstream, and until then persist the census + raw file references so joins stay possible at the file level. |

**Overall: GO** — harbor 0.22.0's converter is fit as atif-sql's ingestion
engine. Evidence: 20/20 sessions converted, 20/20 validated with zero errors,
token totals exact to the raw transcript, and worst-case wall-clock 4.2 s on a
41.6 MB session. The conditions attached to the GO: (a) keep the loss-report
census attached to every ingested trajectory (the drop accounting is the
honesty layer), (b) fix gap 1 in our staging layer since 3/20 sessions lose
some or all subagent transcripts today — one session (`session-C`) loses 100%
of them, (c) read the cache split from `extra`, and (d) budget for ~2x output
inflation relative to raw JSONL.

## 5. Reproducibility

- harbor pin: `harbor>=0.22.0,<0.23` (resolved: 0.22.0, per `uv.lock`)
- python: 3.13.12 (uv-managed)
- corpus root: one project dir under `$CLAUDE_CONFIG_DIR/projects/<project-slug>`
- exact command (per session id in the sample file):

```bash
while IFS= read -r id || [ -n "$id" ]; do
  timeout 600 uv run python experiments/001-harbor-converter-fidelity/run.py \
    "$CLAUDE_CONFIG_DIR/projects/<project-slug>" \
    "$id" experiments/001-harbor-converter-fidelity/out
done < /tmp/atif-sample-sessions.txt
```
