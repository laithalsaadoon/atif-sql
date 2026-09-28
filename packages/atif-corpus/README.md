# atif-corpus

Corpus materialization for ATIF trajectories — owns CONTRACT.md
§Materialized corpus layout: session discovery (main transcripts +
subagent side-files, incl. workflow-nested), per-session watermarks
(convert only what changed), quiescence detection (skip sessions still
being written), and atomic artifact writes (`trajectory.json.zst`,
`edges.jsonl.zst`, `session_events.jsonl.zst`, `loss_report.json`,
`meta.json`, `watermark.json`). An optional `ArtifactProducer`
(`atif_corpus.domain.ports`) runs inside the staged session directory after
the JSON artifacts and before `meta.json`, so anything it writes publishes in
the same directory swap and the keys it returns land in `meta.json`.
`atif_corpus.infrastructure.compress_artifacts` compresses an older session's
plain files in place for `atif-sql corpus slim`.

Sessions are never deleted: one whose source transcript vanished keeps its
artifacts and is marked `source_present: false`. Each live conversion also
writes a zstd archive of the raw source files to `source/` in the same swap,
and `atif_corpus.infrastructure.source_archive.restore_session_sources`
rebuilds the tree from it. A session is re-converted when its sources move or
when its recorded `converter_schema` differs from the running one, from its archive if the source is gone. Transcripts with
nothing to convert are recorded in `empty_sessions.json` instead of failing
every pass. `docs/CONTRACT.md` has the rules.

Hexagonal: `domain/` (pure decisions: plan, quiescence, watermark diff,
layout, `corpus_slug`) < `infrastructure/`
(scanner, settings, atomic writes, `FakeConverter`) < `application/`
(the `materialize` use case). Conversion happens behind
`atif_corpus.domain.ports.ConverterPort`; atif-cli adapts the real
converter — this package never imports atif-converter or atif-duck
(import-linter independence contract).
