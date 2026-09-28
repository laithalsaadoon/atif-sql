# atif-converter

Converts Claude Code session JSONL and Codex rollouts into ATIF trajectories
(converters ported from harbor and held to parity with harbor 0.23.0, on harbor's ATIF models vendored in
`atif_converter.domain.atif`), and owns the FIDELITY POLICY: the seven known upstream
conversion gaps are encoded as `atif_converter.domain.fidelity.FidelityGap`,
and every conversion is audited into a `LossReport` (raw-side census vs
converted output). harbor is a dev dependency only; the tests pin harbor 0.23.0
behavior and hold the vendored models to it, so they are the drift alarm for a
harbor bump.

## Census scope

`LossReport.record_counts` (and therefore `records_total` / `records_dropped`)
counts every record of every `*.jsonl` discovered under the session's side
directory, at any depth and regardless of whether a `subagents/` part appears
in the path. That is the same file set the adapter stages into harbor and the
same set `edges.jsonl` is built from, so `records_total` always equals the
edges line count. A session with a side-file outside `subagents/` reports a
higher `records_total` than a census scoped to `subagents/` alone would.

## Cost estimation

Claude Code's per-step `cost_usd` and `final_metrics.total_cost_usd`, and Codex's
per-call `cost_usd`, are estimates harbor computes with `litellm.cost_per_token`. They still are, in
value: `atif_converter.domain.pricing` returns the same floats, bit for bit,
without litellm being installed. It reads `domain/model_prices.json`, a
filtered copy of litellm's public `model_prices_and_context_window.json` (MIT;
the notice and source ref are in its `meta` block), and repeats litellm
1.102.0's arithmetic in the same float operation order. The table holds the
Claude and OpenAI text models our transcripts name under the `anthropic`,
`openai`, `bedrock` and `bedrock_converse` providers, and keeps litellm's
`fallback_generalizations` rules, which route an unmapped `claude-<family>-<n>`
id to Anthropic at zero rates; the converter reports such a model as unpriced
(`None`), never $0.

Anything else (a `provider/model` string, a fine-tune id, a `tiered_pricing`
table, a model the table doesn't hold) is unpriced, and so is its step: harbor
writes $0 there, the converter writes no `cost_usd`. Local overrides price
models litellm doesn't carry yet, from the vendor's published rates, and a
step or session priced from one is labeled `litellm_estimate+local_overrides`.

`scripts/update_prices.py` regenerates the table by hand from a litellm git
ref and retires an override once upstream prices the same key.
`tests/test_pricing_identity.py` is the proof of the arithmetic, run where the
dev litellm is installed: for every vendored entry whose data the installed
litellm shares, crossed with token shapes that reach each branch and every
service tier, and for the model names found in the frozen benchmark corpora,
our floats are `==` to litellm's. `tests/test_pricing_policy.py` pins frozen
values that run without litellm.

## One read, then the snapshot re-check

`convert_and_audit` reads each source file once. `raw_records.load_session`
stats the file, then streams its bytes through a sha256 digest on the way to
the JSON parser, so the fingerprint it records is the fingerprint of the bytes
that were parsed. The converter, the census, the `edges.jsonl` emitter and the
enrichment pass all consume that one list of records, so the three artifacts
describe the same bytes by construction.

Once the artifacts are built the fingerprints are re-checked, stat and digest
both: a same-length rewrite inside one mtime tick is invisible to the stat
pair, so the re-check hashes again. Movement anywhere between the read and the
check raises `SourceMutatedDuringConversion` rather than publishing artifacts
that describe bytes the session no longer holds.

Per file that's two opens, one parse and two hash passes. The previous flow
(fingerprint, converter read, re-check, audit parse, re-check) opened each
file five times, parsed it twice and hashed it three times; on the 76 MB
benchmark session with its 59 MB of side files the use case went from 1.55 s
to 1.01 s in-process. `tests/test_snapshot_and_drift.py::TestSinglePass` pins
the counts for both agents.
