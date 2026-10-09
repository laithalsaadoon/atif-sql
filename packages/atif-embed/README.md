# atif-embed

Embedding pipeline for the atif-sql workspace: two embedders behind one
port (Cohere Embed v4 on Bedrock, or EmbeddingGemma 2 on this machine), a
local LanceDB vector store, and the backfill use case that discovers and
embeds corpus steps with no current embedding.

## Providers

`ATIF_SQL_EMBED_PROVIDER` selects one, and `providers.build_embedder` is the
only place that names a concrete adapter.

| | `cohere` (default) | `gemma` |
| --- | --- | --- |
| Model | `global.cohere.embed-v4:0` on Bedrock | `google/embeddinggemma-2`, text-only, pinned revision |
| Where it runs | Amazon Bedrock, billed | this machine, free, after a 1.5 GB download |
| Widths (`ATIF_SQL_OUTPUT_DIMENSION`) | 1024 (default), 256, 512, 1536 | 768 (default), 512, 256, 128 |
| Default store | `<corpus_root>/embeddings_lance` | `<corpus_root>/embeddings_lance_gemma` |
| Install | always | the `local` extra (torch, transformers, sentence-transformers, torchvision, pillow) |

The Gemma adapter loads the model lazily, once, through sentence-transformers
with the vision and audio encoders dropped. Documents embed under the model's
`Document` prompt and queries under `SearchQuery`, truncated to the width and
re-normalized. It runs bfloat16 on a CUDA device that supports it and float32
everywhere else, never float16, which the model card says returns NaN or
degraded vectors. Batches form shortest first under a padded-character budget,
so a long step never pads a whole batch to its length, and the loader caps
each text at the model's 8,192-token window. torch and its libraries are
imported by name inside the adapter, so the default path, CI and both type
checkers run without the extra.

## Layout

```
atif_embed/
  domain/
    ports.py             EmbeddingProvider / VectorStorePort / TextRowsPort /
                         LakeStepsPort (+ LakePosition, LakeStepSnapshot)
    discovery.py         PendingSelection — first-wins, hash skip, stale, limit
    embedding_guard.py   ensure_store_matches — fail-loud (model, dim) guard
    errors.py            DomainError taxonomy
    text_stamp.py        text_hash + the 50K cap + PendingText
  infrastructure/
    cohere_bedrock.py    Cohere Embed v4 adapter (batch 96, semaphore 8,
                         int8 docs / float queries, 50K clip, truncate RIGHT)
    gemma_local.py       EmbeddingGemma 2 adapter (text-only, pinned revision,
                         Document / SearchQuery prompts, bf16 or fp32)
    providers.py         build_embedder — the adapter the settings select
    lance_store.py       LanceDB embeddings table + IvfHnswSq cosine index
    corpus_text_rows.py  DuckDbTextRows — TextRowsPort over the contract corpus
    lake_text_rows.py    LakeTextRows — TextRowsPort over LakeStepsPort, with
                         the lake watermark; falls back to DuckDbTextRows
    settings.py          EmbedSettings (env prefix ATIF_SQL_)
  application/
    embed.py             run_backfill / discover_unembedded / embed_query
    prune.py             prune_orphans — stored rows no lake step names
```

## Independence: why TextRowsPort exists

atif-embed is a workspace-independent package (import-linter independence
contract: converter / corpus / duck / embed may never import each other;
only atif-cli composes). But the backfill needs the corpus's embeddable
text rows — a surface atif-duck's `steps` view already computes.

Resolution (the ConverterPort precedent from atif-corpus): the use case
depends on a domain port, `TextRowsPort.iter_unembedded(corpus_root,
exclude, limit)`, and atif-embed ships its OWN DuckDB implementation
(`DuckDbTextRows`) that reads the CONTRACT corpus layout directly —
`read_json` over each session's stored trajectory (`trajectory.json.zst`, or
the plain `trajectory.json` of an older corpus, batched by decompressed size) with the
meta.json torn-set gate — rather than importing `atif_duck.register`. The
two packages are coupled through docs/CONTRACT.md's artifact shapes, not
through code.

## Discovery from the lake

Reading every trajectory on every run costs minutes on a large corpus to find
a handful of new rows, and the lake's `steps` table already holds each step's
flattened `message` and `source_uuids`. `LakeStepsPort` is the seam: atif-cli
implements it over atif-duck's `infrastructure.lake_steps`, and
`LakeTextRows` reads through it:

- Rows come back in the per-session reader's order (session id, then step
  id), keyed the same way, so the row set and every stored vector are
  unchanged. `PendingSelection` applies the same rules to both sources.
- `lake_watermark.json`, inside the store directory, records the lake
  position of the last run that read to the end and embedded every row
  (the corpus's `registered_at` as the lineage, the snapshot id), the text
  floor, and the store's row count. The next run reads only rows whose uuid
  a lake snapshot inserted or deleted after it, every occurrence in corpus
  order, so the first-wins rule lands where a full read would.
- A missing or foreign watermark, a rebuilt lake, expired snapshots, a
  changed floor, or a store row count that moved outside a run each force a
  full lake read. No usable lake means the per-session reader.
- `prune_orphans` counts, and on request deletes, stored rows whose uuid is
  no lake step's primary uuid.

## Selection + keying semantics (CONTRACT-V2 §VSS)

- The embeddable unit is a step's flattened text (`str | ContentPart[]`;
  the ARRAY branch joins part texts with blank lines, mirroring harbor's
  bundling and atif-duck's `steps` view).
- Main chain AND sidechain steps are included.
- Only texts of >= 32 characters qualify.
- **uuid keying:** each row is keyed by the step's PRIMARY raw-record uuid —
  the FIRST `extra.source_uuids` entry. Harbor bundles all events sharing an
  assistant `message.id` into one step, so a step maps to 1..n raw uuids;
  the first entry is the bundle's head record, which joins back to the
  `messages` view (edges.jsonl) for snippets/session filters. Steps without
  `source_uuids` are skipped: they cannot be keyed against the raw-record
  surface. Consequence: non-head uuids of a bundle have no vector of their
  own; queries against their content resolve to the head record's vector.

## Store contract

Lance table `embeddings` at `ATIF_SQL_LANCE_URI`
(default `<corpus_root>/embeddings_lance` for Cohere and
`<corpus_root>/embeddings_lance_gemma` for Gemma):

| column      | type                  |
|-------------|-----------------------|
| uuid        | string (primary key by convention) |
| model       | string                |
| dim         | int32                 |
| embedding   | FixedSizeList<float32, dim> |
| embedded_at | timestamp[us, UTC]    |
| text_hash   | string                |
| truncated   | bool                  |

Every row stamps the writing provider's `(model, dim)`. Both the write path
(`run_backfill`) and the read/bind path (atif-duck's `register_vss`) call
`ensure_store_matches` and raise `EmbeddingProviderMismatch` on drift —
vectors from different models live in incompatible spaces, so the guard
fails loud instead of silently corrupting kNN search. Recovery: `rm -rf`
the Lance directory and re-run `atif-sql embed --all --no-dry-run` (a bare
`atif-sql embed` exits 64; a real run needs an explicit scope).

Schema evolution is versioned (`schema_version.json` sidecar next to the
table; currently v2) and ADDITIVE changes migrate online: opening a store
that predates a column evolves the table in place (`add_columns`,
metadata-only) and backfills a sentinel, which the staleness anti-join then
treats as a mismatch — the store re-embeds itself incrementally over
successive runs while search stays up. Only a provider/dimension switch is
a rebuild, via the fail-loud guard above.

`text_hash` is what makes re-conversion safe. A step's uuid survives a
harbor/converter fix but its flattened text may not, so discovery compares
hashes, not just uuids: a changed text is STALE, its old row is deleted, and
the new vector replaces it. Without the stamp the first vector would be
permanent and search would rank against pre-fix text undetectably.

`truncated` marks rows whose source text exceeded the 50K cap and embedded
head-only, so a search that never matches content past the cap is
attributable rather than mysterious.

## Bounded loss

An `embed --all` over a large corpus is a multi-hour, billed run, so nothing
already paid for is thrown away:

- discovery splits the trajectory files into batches of at most 4 MB of total
  file bytes and runs ONE DuckDB statement per batch. DuckDB materializes a
  result set fully inside `execute()`, so paging a single all-paths query off
  `fetchmany` bounds nothing — the split is what bounds it. Bytes, rather
  than a file count, is the axis because residency tracks the bytes one
  statement reads while wall time tracks the statement COUNT; batching by
  bytes bounds both. Peak resident text is therefore
  `max(4 MB budget, largest single session)`, so a corpus holding one huge
  session cannot be brought below that session.

  Measured against the last committed version of this adapter, which used a
  single all-paths query (peak RSS via `ru_maxrss`, consuming
  `iter_unembedded` in 256-row chunks and dropping them, 3 runs each).
  Numbers are before -> after:

  | shape | memory | wall |
  |---|---|---|
  | real corpus, 5,436 sessions / 225,426 rows | 24,689-25,886 MB -> 2,727-2,748 MB | 72-94 s -> 199-223 s |
  | 32 fat sessions x 100 steps x 50K chars | 1,829-1,961 MB -> 181-183 MB | 2.5-2.8 s -> 4.5-4.6 s |
  | 8,000 sessions x 3 steps x 200 chars | 264-271 MB -> 174-178 MB | 2.2-2.4 s -> 2.3-2.8 s |
  | 2,000 sessions x 3 steps x 200 chars | 126-127 MB -> 128-129 MB | 0.7 s -> 0.6-0.8 s |

  So the memory win is ~9x on the real corpus and ~10x on fat-session shapes,
  and it is roughly nothing on many-small-session shapes — those already fit.
  The wall cost is the honest tradeoff: about 2.6x on the real corpus, 1.7x on
  the fat shape, and none at the small end. Which mode wins by shape: batching
  wins wherever any single session is large (the fat shape and the real corpus,
  where peak drops by an order of magnitude), and is a wash on corpora of
  uniformly small sessions, where it costs nothing and buys little. Note the
  real corpus's peak (2.7 GB) sits far above the 4 MB budget because its
  largest single trajectory.json is 436 MB; the budget cannot bound below one
  session, and that session is the floor.

  The wall cost above is amortized against billed Bedrock calls only on a real
  `embed` run. `--dry-run` makes zero Bedrock calls, so it has nothing to
  amortize against, and it goes through this same discovery. `--dry-run`
  peak RSS / wall against the same single-query baseline, 3 runs each:

  | shape | memory | wall |
  |---|---|---|
  | real corpus, 5,436 sessions | 24,827-28,257 MB -> 2,750-2,766 MB | 61-64 s -> 176-182 s |
  | 32 fat sessions x 100 steps x 50K chars | 1,793-1,884 MB -> 199-200 MB | 2.8-3.2 s -> 3.9-4.0 s |
  | 8,000 sessions x 3 steps x 200 chars | 286-300 MB -> 195-203 MB | 3.6-3.9 s -> 2.2-2.4 s |

  So on the synthetic shapes dry-run is now cheaper on memory everywhere and
  on wall time at the 8,000-session end. On the real corpus it is still about
  2.8-3x slower in wall time while using ~10x less memory, and that is the
  honest state: parsing 7.4 GB of trajectory JSON is the floor, and a dry run
  has to parse it to know what is pending. A count that skipped the
  text egress was measured and REJECTED — it was faster on an empty store
  (2.0 s vs 2.1 s on the 8,000-session shape) but far slower on a populated one
  (17.1 s vs 2.4 s), which is the state an operator actually re-runs against;
- chunk checkpoints commit `max(batch_size * 4, 256)` rows at a time;
- a batch that exhausts its retry budget returns `None` slots instead of
  aborting the chunk — its sibling batches' vectors are written, and the next
  run's staleness comparison re-picks the gap;
- throttle codes (`ThrottlingException`, `TooManyRequestsException`,
  `ProvisionedThroughputExceededException`, `InternalServerException`,
  `InternalFailure`, …) are retried, not treated as terminal. This set is a
  deliberate twin of atif-models' copy and is pinned by a test on both sides.

Every adapter failure surfaces as a `DomainError` subclass
(`EmbeddingProviderUnavailable` / `EmbeddingResponseInvalid`), which is what
the CLI catches to emit a classified `{kind, exit_code, hint}` envelope; a raw
botocore traceback would escape that.

Documents are stored int8-encoded then float-widened; queries always embed
as float. Cosine ranking is therefore on mixed-magnitude vectors — cosine
similarity/distance is magnitude-invariant, but do NOT swap in L2
(`array_distance`) downstream: raw int8-derived document vectors have
magnitudes in the thousands while query vectors are unit-normalized.
