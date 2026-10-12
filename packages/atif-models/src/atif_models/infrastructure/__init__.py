# SPDX-License-Identifier: Apache-2.0

"""Infrastructure layer for atif-models.

bedrock-runtime adapters (OpenAI chat-completions, the default family, and
Anthropic Messages with ``output_config.format``), the factory that picks one
per model spec, and the ``ATIF_SQL_``-prefixed env settings. Everything here
imports DOWN into ``domain`` only.
"""
