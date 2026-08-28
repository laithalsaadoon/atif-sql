# atif-converter

Wraps harbor's `ClaudeCode` adapter to convert Claude Code session JSONL into
ATIF trajectories, and owns the FIDELITY POLICY: the seven known upstream
conversion gaps are encoded as `atif_converter.domain.fidelity.FidelityGap`,
and every conversion is audited into a `LossReport` (raw-side census vs
converted output). The tests pin harbor 0.22.0 behavior — they are the drift
alarm for version bumps.

## Census scope

`LossReport.record_counts` (and therefore `records_total` / `records_dropped`)
counts every record of every `*.jsonl` discovered under the session's side
directory, at any depth and regardless of whether a `subagents/` part appears
in the path. That is the same file set the adapter stages into harbor and the
same set `edges.jsonl` is built from, so `records_total` always equals the
edges line count. A session with a side-file outside `subagents/` reports a
higher `records_total` than a census scoped to `subagents/` alone would.

## Snapshot discipline

`convert_and_audit` fingerprints every source file before harbor reads, and
re-checks after harbor's read and again after the raw parse; movement anywhere
in that window raises `SourceMutatedDuringConversion` rather than publishing a
census, a trajectory, and an `edges.jsonl` that describe different bytes.

The snapshot carries fingerprints only. Records are parsed after harbor's
converter returns and releases its working set, so the two large allocations
do not overlap — measured peak RSS on an 18 MB / 17,932-record session is
595.4 MiB, against 761.4 MiB when the parse is hoisted ahead of harbor.
