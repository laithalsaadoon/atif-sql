# SPDX-License-Identifier: Apache-2.0

"""atif-models: model alias registry + structured-output LLM client.

Owns CONTRACT-V2.md §Model registry and §Client contract — size aliases
(small/medium/large) resolve to concrete Bedrock GLOBAL inference-profile
ids per family (openai default, anthropic escape hatch), and the
``LlmStructuredProvider`` port turns (system, prompt, pydantic schema)
into a validated schema instance. NO other atif-sql package hardcodes a
model id. Hexagonal: ``domain`` (registry, port, schema transform, usage
accounting — pure) < ``infrastructure`` (bedrock-runtime adapters, env
settings). Independence contract: only atif-cli and atif-analytics may
import this package.
"""
