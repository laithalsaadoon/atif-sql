# atif-sql wave-2 contract: analytics, VSS, cron (addendum to CONTRACT.md)

Status: EXECUTED. Every item below is built and shipped: atif-models,
atif-analytics, atif-embed, the VSS stack, and the cron lanes all exist. The
imperative phrasing ("port X", "use Y") is the original build instruction,
kept because it records WHICH shape was chosen and why. When it disagrees
with the code, the code is authoritative: read this file for design intent,
not as a description of current behavior.

Note (2026-09-27): the trajectory pipeline and the structural
pipelines (cluster, terms, community) were removed, along with their views,
macros, and the structural cron lane. atif-analytics now runs the LLM
pipelines classify, conflicts, friction, and perceived. classify labels only
work category and goal (autonomy tier and success were dropped as
unreliable) and runs on small (luna). The cron lanes are materialize, llm,
and compact. The trajectory, autonomy-tier, and structural lines below
describe the original build.

## New packages
- atif-models: model alias registry + structured-output LLM client. NO other
  package hardcodes the id of an LLM it calls.
- atif-analytics: the v2 pipelines (classify, trajectory, conflicts,
  friction, perceived, cluster, terms, community). Depends on atif-models
  ONLY; the `forbidden` contract bars atif-duck. atif-cli composes.

## Model registry (atif-models domain)
Size aliases -> per-family concrete IDs (bedrock-runtime, GLOBAL inference profiles):
  small : openai    = global.openai.gpt-6-luna
          anthropic = global.anthropic.claude-haiku-5-5
  medium: openai    = global.openai.gpt-6.1-sol
          anthropic = global.anthropic.claude-sonnet-5-5
  large : openai    = global.openai.gpt-6-astra
          anthropic = global.anthropic.claude-opus-5-5
Default family: openai (per operator decision 2026-08-23). Both families are
runnable: ATIF_SQL_LLM_FAMILY=anthropic selects the Claude column.
Defaults: reasoning_effort="high", max_completion_tokens=32_000.
GPT-6 bills a request over 272K input tokens at 2x input and 1.5x output,
and Haiku 5.5 bills a request over 100K input tokens at $0.50/$2.50. Each adapter marks the calls that cross the line.
Settings env: ATIF_SQL_LLM_FAMILY, ATIF_SQL_LLM_SIZE_<PIPELINE> overrides.

## Client contract (atif-models infrastructure)
- Port: LlmStructuredProvider protocol, the one seam to a model provider:
  async classify_structured(*, system, prompt, schema: type[BaseModel]) -> BaseModel.
  Errors: RefusalError (terminal), ProviderUnavailable (retryable -> retry queue).
  build_structured_provider picks the adapter for spec.family.
- openai adapter: bedrock-runtime invoke_model, OpenAI chat-completions body:
  {messages, max_completion_tokens, reasoning_effort,
   response_format: {type: json_schema, json_schema: {name, strict: true, schema}}}
  system goes in as a "system" role message. Strict-mode schema rules:
  additionalProperties=false everywhere, all properties required (optional
  fields become nullable-typed). Verified live on all three GPT-6 ids
  2026-10-12.
- anthropic adapter: bedrock-runtime invoke_model, Anthropic Messages body:
  {anthropic_version: bedrock-2023-05-31, max_tokens, system, messages,
   output_config: {effort, format: {type: json_schema, schema}}}
  The schema goes through anthropic.transform_schema, because Claude 5.5
  rejects minimum/maximum/minLength/maxLength in a structured-output schema.
  No temperature, top_p, or partial assistant turn (Claude 5.5 rejects all
  three). Thinking
  stays at the model default; the parser reads text blocks only and folds a
  case-variant enum value onto the declared member. stop_reason refusal ->
  RefusalError; max_tokens -> one retry at one effort step lower. Verified
  live on all three Claude 5.5 ids 2026-10-12.
- Pydantic validation is the second gate behind both providers' schema
  enforcement, and the only gate for the constraints transform_schema moves
  into descriptions.
- tenacity retry over the retryable provider codes; token accounting accumulated
  per pipeline; concurrency via one anyio CapacityLimiter (default 16).

## Pipeline size assignments (defaults)
classify=small, perceived=medium, conflicts=large (hardest judgment task),
friction=small (per-message enum), embed unchanged
(cohere embed v4 1024-dim int8 by default; EmbeddingGemma 2 768-dim is the
second adapter behind EmbeddingProvider).

## Ports & state: one shape per seam, declared in domain/ports.py
SessionSource (atif-analytics) and VectorStorePort (atif-embed), each in its
package's domain/ports.py; ParquetCache/SqliteCheckpoint/SqliteRetryQueue in
atif-analytics infrastructure over a sqlite WAL state.db per corpus,
parquet shard dirs under <corpus_root>/analytics/<name>/, checkpoint keyed
(session_id, pipeline) on (last_ts, last_mtime).
KEY PROPERTY: pipelines read the MATERIALIZED corpus
(trajectory.json steps + edges.jsonl, or the lake), not raw JSONL.
Transcript text for prompts = steps-based rendering under fixed caps (800K/session). Tool
calls render with their arguments (400 chars); each tool result renders as a
size stub, so tool output stays on disk.
ATIF_SQL_SESSION_TEXT_TOOL_RESULT_BODIES=true sends result bodies instead,
clipped at 50K each. conflicts needs uuid-addressable turns -> use messages(edges)
uuids in headers; validate returned uuids against edges.
Schemas/enums are fixed vocabularies (autonomy tiers, work categories,
transition_kinds, conflict kinds, friction labels): parity of meaning.
Friction tiers: regex + SQL stamp layers run first (zero-cost), LLM tier on luna.
Structural pipelines (cluster/terms/community): fixed hyperparameters
(UMAP 50-dim/HDBSCAN 20,5/Leiden k15 floor .3 min 3 seed 42/c-TF-IDF 2,.95,1-2,top10).

## VSS
The stack: cohere embed v4 (the default; EmbeddingGemma 2 on this machine with
ATIF_SQL_EMBED_PROVIDER=gemma, in its own store) -> lancedb IvfHnswSq -> duckdb lance
ATTACH -> message_embeddings view + semantic_search(query_vec,k) macro +
search CLI (cosine-distance ranking, int8-doc caveat). Embeds messages_text
analogue = steps text >=32 chars (main+sidechain), uuid-keyed via source_uuids
primary uuid (first source uuid per step; document choice).

## Cron
scripts/atif-sql-refresh.sh: lanes (materialize */10, structural :17,
llm nightly 10:20Z, compact nightly 03:40), flock per lane, AWS env hygiene +
bearer-token read at runtime, every corpus (the primary config root plus each
ATIF_SQL_EXTRA_CONFIG_DIRS entry) via CLAUDE_CONFIG_DIR + ATIF_SQL_SOURCE_ROOT,
log to scripts/.run/, selftest script with the opt-out tripwire pattern. Installs via
`atif-sql cron install` printing the crontab block (no silent crontab writes).

## Cost guards
analyze defaults dry_run=true; --no-dry-run only in the cron llm lane;
estimate_cost in the registry; per-pipeline token accounting logged;
embed --limit 500.
