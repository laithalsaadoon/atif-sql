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
- `atif_models.infrastructure.settings` — `ATIF_SQL_LLM_*` env settings,
  per-pipeline size overrides, and `RUNNABLE_FAMILIES`.

`openai` is the only runnable family: the registry carries anthropic model
ids and pricing, but no anthropic provider adapter exists, so
`ATIF_SQL_LLM_FAMILY=anthropic` is rejected at settings load rather than
400-ing on every Bedrock call.

Only atif-cli and atif-analytics may import this package (import-linter
independence contract at the workspace root).
