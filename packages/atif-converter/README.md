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

## Cost estimation

Claude Code's `final_metrics.total_cost_usd` and Codex's per-call `cost_usd` are
estimates harbor computed with `litellm.cost_per_token`. They still are, in
value: `atif_converter.domain.pricing` returns the same floats, bit for bit,
but it doesn't import litellm to do so. `import litellm` cost about four
seconds per process (openai, anthropic, hundreds of pydantic model builds),
which was most of the time a conversion took. The module reads litellm's own
bundled price table straight off disk, finds it with `importlib.util.find_spec`
so `litellm/__init__` never runs, and repeats litellm 1.100.1's arithmetic in
the same float operation order for the shapes our transcripts produce: bare
model names the table holds under the `anthropic`, `openai`, `bedrock` and
`bedrock_converse` providers, plus the unmapped `claude-<family>-<n>` ids
litellm routes to Anthropic through its `fallback_generalizations` rules and
prices at zero.

Anything else (a `provider/model` string, a fine-tune id, a `tiered_pricing`
table, another provider) falls back to importing litellm and calling it as
before, logged at debug, so correctness never depends on the fast path's
coverage. `tests/test_pricing_identity.py` is the proof: for every covered key
in the table crossed with token shapes that reach each branch of the
arithmetic and every service tier, and for the model names found in the frozen
benchmark corpora, the fast path's floats are `==` to litellm's.

`LITELLM_LOCAL_MODEL_COST_MAP` keeps its litellm meaning. Unset or `true`, the
fast path prices from the bundled table (litellm itself would fetch the table
from GitHub when the variable is unset, which is slow and not reproducible; the
fallback sets it to `true` before importing so one process never prices from
two tables). Set to anything else, the operator has asked for the remote table,
and every call goes through litellm.

## Snapshot discipline

`convert_and_audit` fingerprints every source file before harbor reads, and
re-checks after harbor's read and again after the raw parse; movement anywhere
in that window raises `SourceMutatedDuringConversion` rather than publishing a
census, a trajectory, and an `edges.jsonl` that describe different bytes.

The snapshot carries fingerprints only. Records are parsed after harbor's
converter returns and releases its working set, so the two large allocations
do not overlap — measured peak RSS on an 18 MB / 17,932-record session is
595.4 MiB, against 761.4 MiB when the parse is hoisted ahead of harbor.
