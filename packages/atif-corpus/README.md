# atif-corpus

Corpus materialization for ATIF trajectories — owns CONTRACT.md
§Materialized corpus layout: session discovery (main transcripts +
subagent side-files, incl. workflow-nested), per-session watermarks
(convert only what changed), quiescence detection (skip sessions still
being written), and atomic artifact writes (`trajectory.json`,
`loss_report.json`, `edges.jsonl`, `meta.json`, `watermark.json`). An
optional `ArtifactProducer` (`atif_corpus.domain.ports`) runs inside the
staged session directory after the JSON artifacts and before `meta.json`,
so anything it writes (atif-duck's columnar parquets, in practice) publishes
in the same directory swap and the keys it returns land in `meta.json`.

Hexagonal: `domain/` (pure decisions: plan, quiescence, watermark diff,
layout, `corpus_slug`) < `infrastructure/`
(scanner, settings, atomic writes, `FakeConverter`) < `application/`
(the `materialize` use case). Conversion happens behind
`atif_corpus.domain.ports.ConverterPort`; atif-cli adapts the real
converter — this package never imports atif-converter or atif-duck
(import-linter independence contract).
