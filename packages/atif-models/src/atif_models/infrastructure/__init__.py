# SPDX-License-Identifier: Apache-2.0

"""Infrastructure layer for atif-models.

bedrock-runtime adapters (OpenAI chat-completions default, Anthropic
``output_config`` experimental stub) and the ``ATIF_SQL_``-prefixed env
settings. Everything here imports DOWN into ``domain`` only.
"""
