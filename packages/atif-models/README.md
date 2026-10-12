# atif-models

Model alias registry + structured-output LLM client for the atif-sql
workspace (CONTRACT-V2 §Model registry / §Client contract).

- `atif_models.domain.registry` — frozen `ModelSpec` + size-alias registry.
  NO other package hardcodes a Bedrock model id.
- `atif_models.domain.ports` — `LlmStructuredProvider` protocol, error
  taxonomy (`RefusalError` / `ProviderUnavailable`), `CallUsage` +
  `UsageAccumulator` token accounting.
- `atif_models.domain.schema` — pydantic → OpenAI strict-mode JSON Schema
  transform (`to_openai_strict`).
- `atif_models.infrastructure.openai_bedrock` — the default provider:
  OpenAI chat-completions body shape on `bedrock-runtime` `invoke_model`
  with `response_format: json_schema, strict: true`.
- `atif_models.infrastructure.anthropic_bedrock` — the Claude provider,
  with the Anthropic Messages body shape on `invoke_model` and
  `output_config.format: json_schema`, the schema passed through
  `anthropic.transform_schema`.
- `atif_models.infrastructure.providers` — `build_structured_provider`, which
  picks the adapter for a spec's family.
- `atif_models.infrastructure.settings` — `ATIF_SQL_LLM_*` env settings,
  per-pipeline size overrides, and `RUNNABLE_FAMILIES`.

Both families are runnable. `openai` (GPT-6) is the default, and
`ATIF_SQL_LLM_FAMILY=anthropic` switches every pipeline to Claude 5.5.

Only atif-cli and atif-analytics may import this package (import-linter
independence contract at the workspace root).
