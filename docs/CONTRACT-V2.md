# atif-sql wave-2 contract: analytics, VSS, cron (addendum to CONTRACT.md)

Status: EXECUTED. Every item below is built and shipped — atif-models,
atif-analytics, atif-embed, the VSS stack, and the cron lanes all exist. The
imperative phrasing ("port X", "use Y") is the original build instruction,
kept because it records WHICH shape was chosen and why. When it disagrees
with the code, the code is authoritative: read this file for design intent,
not as a description of current behavior.

## New packages
- atif-models: model alias registry + structured-output LLM client. NO other
  package hardcodes a model id.
- atif-analytics: eight v2 pipelines (classify, trajectory, conflicts,
  friction, perceived, cluster, terms, community). Depends on atif-models
  ONLY — the `forbidden` contract bars atif-duck. atif-cli composes.

## Model registry (atif-models domain)
Size aliases -> per-family concrete IDs (bedrock-runtime, GLOBAL inference profiles):
  small : openai gpt-5.6 luna  = global.openai.gpt-5.6-luna
          anthropic equivalent = global.anthropic.claude-haiku-4-5
  medium: openai gpt-5.6 terra = global.openai.gpt-5.6-terra
          anthropic equivalent = global.anthropic.claude-sonnet-5
  large : openai gpt-5.6 sol   = global.openai.gpt-5.6-sol
          anthropic equivalent = global.anthropic.claude-opus-5
Default family: openai (per operator decision 2026-08-23 — GPT 5.6 supports
native strict structured outputs). Anthropic equivalents are registry entries
only (selection escape hatch), not wired defaults.
Defaults: reasoning_effort="high", max_completion_tokens=32_000.
Settings env: ATIF_SQL_LLM_FAMILY, ATIF_SQL_LLM_SIZE_<PIPELINE> overrides.

## Client contract (atif-models infrastructure)
- bedrock-runtime invoke_model, OpenAI chat-completions body shape:
  {messages, max_completion_tokens, reasoning_effort,
   response_format: {type: json_schema, json_schema: {name, strict: true, schema}}}
  (verified live on global.openai.gpt-5.6-terra 2026-08-23: strict schema honored,
  usage carries reasoning_tokens + cached_tokens).
- Port: LlmStructuredProvider protocol, the one seam to a model provider:
  async classify_structured(*, system, prompt, schema: type[BaseModel]) -> BaseModel.
  system goes in as a "system" role message. Errors: RefusalError (terminal),
  ProviderUnavailable (retryable -> retry queue).
- Strict-mode schema rules for OpenAI: additionalProperties=false everywhere,
  all properties required (OpenAI strict requirement — optional fields become
  nullable-typed), $defs inlined where Bedrock chokes. Keep pydantic validation
  as the second gate behind provider strict mode.
- tenacity retry over the retryable provider codes; token accounting accumulated
  per pipeline; concurrency via one anyio CapacityLimiter (default 16).

## Pipeline size assignments (defaults)
classify=medium(terra), trajectory=medium, conflicts=large(sol — hardest
judgment task), friction=small(luna — per-message enum), embed unchanged
(cohere embed v4 1024d int8, one adapter behind EmbeddingProvider).

## Ports & state: one shape per seam, declared in domain/ports.py
CachePort/CheckpointPort/RetryQueuePort/VectorStorePort, sqlite WAL state.db
per corpus, parquet shard dirs under <corpus_root>/analytics/<name>/,
checkpoint keyed (session_id, pipeline) on (last_ts, last_mtime).
KEY PROPERTY: pipelines read the MATERIALIZED corpus
(atif-duck views: steps/messages/edges), not raw JSONL. Transcript text for
prompts = steps-based rendering under fixed caps (50K/tool_result,
800K/session). conflicts needs uuid-addressable turns -> use messages(edges)
uuids in headers; validate returned uuids against edges.
Schemas/enums are fixed vocabularies (autonomy tiers, work categories,
6 transition_kinds, 4 conflict kinds, 7 friction labels) — parity of meaning.
Friction tiers: regex + SQL stamp layers run first (zero-cost), LLM tier on luna.
Structural pipelines (cluster/terms/community): fixed hyperparameters
(UMAP 50d/HDBSCAN 20,5/Leiden k15 floor .3 min 3 seed 42/c-TF-IDF 2,.95,1-2,top10).

## VSS
The stack: cohere embed v4 -> lancedb IvfHnswSq -> duckdb lance
ATTACH -> message_embeddings view + semantic_search(query_vec,k) macro +
search CLI (cosine-distance ranking, int8-doc caveat). Embeds messages_text
analogue = steps text >=32 chars (main+sidechain), uuid-keyed via source_uuids
primary uuid (first source uuid per step; document choice).

## Cron
scripts/atif-sql-refresh.sh: three lanes (materialize */10, structural :17,
llm nightly 10:20Z), flock per lane, AWS env hygiene + bearer-token read at
runtime, both corpora via ATIF_SQL_SOURCE_ROOT, log to
scripts/.run/, selftest script with the opt-out tripwire pattern. Installs via
`atif-sql cron install` printing the crontab block (no silent crontab writes).

## Cost guards
analyze defaults dry_run=true; --no-dry-run only in the cron llm lane;
estimate_cost port; per-pipeline token accounting logged; embed --limit 500.
