# SPDX-License-Identifier: Apache-2.0

"""Pure domain layer for atif-models.

The model registry (frozen value objects, no I/O), the
``LlmStructuredProvider`` port + error taxonomy + usage accounting, and
the pydantic → OpenAI strict-mode schema transform. No boto3, no env,
no clock — infrastructure adapts to these shapes.
"""
