# Experiment 001 — harbor converter fidelity on the real corpus

Status: PROTOCOL ONLY — a later agent runs this. Outputs go to `out/`
(gitignored via `experiments/**/out/`).

## Question

How much of the real `~/.claude/projects` corpus does harbor 0.22.0's
Claude Code -> ATIF conversion preserve, per fidelity gap? The synthetic-session
unit tests in `packages/atif-converter/tests/` pin the MECHANISMS; this
experiment measures the MAGNITUDES on live data.

## Protocol

1. Sample N sessions (start with 20, stratified by size: small / medium /
   large by line count) from `$CLAUDE_CONFIG_DIR/projects/*/*.jsonl`
   (default `~/.claude/projects`).
2. For each session run
   `uv run atif-sql convert <session.jsonl> --trajectory-out out/<session>.json`
   and capture the loss-report JSON from stdout into `out/<session>.loss.json`.
3. Aggregate across sessions:
   - records_dropped / records_total, overall and by RecordType;
   - fraction of sessions observing each FidelityGap;
   - subagent files: found vs convertible vs workflow-nested (gap 1);
   - validation failure count (expect 0; any failure is a finding).
4. Diff spot checks: for 3 sessions, manually compare raw JSONL against the
   trajectory to confirm the census attribution is honest (especially the
   gap-1 upper-bound caveat documented in `convert_and_audit`).

## Deliverable

`out/summary.md` with the aggregate table and any surprises, plus a verdict:
which gaps are material enough to fix (staging-side workarounds in
atif-converter) vs tolerate (document in the fidelity policy only).
