# SPDX-License-Identifier: Apache-2.0

"""The structured-output provider port + error taxonomy + usage accounting.

The seam is narrow on purpose: a system prompt, a user prompt, and a
pydantic schema in; a validated instance of that schema out. Everything
provider-specific (model id, region, reasoning effort, the boto3 client,
the concurrency limiter) is owned by the adapter and fixed at construction.

Error taxonomy follows the atif-converter ``DomainError`` pattern:
``RefusalError`` is terminal (content filter / model refusal — do not
retry), ``ProviderUnavailable`` is retryable (the caller's retry queue
owns the second chance after the adapter's in-process tenacity budget is
exhausted).

Usage accounting: each call yields one frozen :class:`CallUsage`;
:class:`UsageAccumulator` sums them thread-safely per pipeline. A
``threading.Lock`` (not an ``anyio.CapacityLimiter``) guards the counters
because adapters dispatch blocking ``invoke_model`` calls via
``anyio.to_thread`` — two worker threads can land in
:meth:`~UsageAccumulator.add` concurrently, and a lock is the right
primitive for a critical section.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypeVar, runtime_checkable

if TYPE_CHECKING:
    from pydantic import BaseModel

#: A structured-output schema, bound to ``BaseModel`` so the adapter can
#: both derive the wire schema and ``model_validate`` the response.
SchemaT = TypeVar("SchemaT", bound="BaseModel")


class DomainError(Exception):
    """Base for every atif-models domain error."""


class RefusalError(DomainError):
    """Terminal: the model refused (content filter / refusal finish reason).

    Never retried — the input itself is the problem. Callers record the
    unit as refused and move on.
    """


class ProviderUnavailable(DomainError):  # noqa: N818 — names a provider state, not an "*Error"
    """Retryable: transport/service failure or an unusable response.

    Raised after the adapter's in-process tenacity budget is exhausted,
    or immediately for non-retryable-but-recoverable shapes (e.g. a
    ``length`` finish reason — bump ``max_completion_tokens`` or shrink
    the prompt). Callers enqueue the unit on the retry queue.
    """


@dataclass(frozen=True, slots=True)
class CallUsage:
    """Token accounting for one structured-output call.

    ``reasoning_tokens`` is the GPT-5.6 ``completion_tokens_details.reasoning_tokens``
    count (a subset of ``output_tokens``, already billed as output);
    ``cached_tokens`` is ``prompt_tokens_details.cached_tokens`` (a subset
    of ``input_tokens`` billed at the cached rate). Both are 0 when the
    provider does not report them.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0


class UsageAccumulator:
    """Thread-safe per-pipeline accumulation of :class:`CallUsage`.

    One instance per pipeline run; adapters call :meth:`add` after every
    successful response, and the pipeline reads :meth:`summary` at the
    end for the cost-guard log line.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._reasoning_tokens = 0
        self._cached_tokens = 0

    def add(self, usage: CallUsage) -> None:
        """Fold one call's usage into the totals (safe from any thread)."""
        with self._lock:
            self._calls += 1
            self._input_tokens += usage.input_tokens
            self._output_tokens += usage.output_tokens
            self._reasoning_tokens += usage.reasoning_tokens
            self._cached_tokens += usage.cached_tokens

    def summary(self) -> dict[str, int]:
        """Snapshot of the totals: calls + the four token counters."""
        with self._lock:
            return {
                "calls": self._calls,
                "input_tokens": self._input_tokens,
                "output_tokens": self._output_tokens,
                "reasoning_tokens": self._reasoning_tokens,
                "cached_tokens": self._cached_tokens,
            }


@runtime_checkable
class LlmStructuredProvider(Protocol):
    """Port: one structured-output call. One adapter per backend.

    The seam is deliberately narrow — everything provider-specific is
    fixed at adapter construction.
    """

    async def classify_structured(
        self, *, system: str, prompt: str, schema: type[SchemaT]
    ) -> SchemaT:
        """Run one structured-output call and return a validated ``schema`` instance.

        ``system`` goes in as a ``system``-role message; ``prompt`` is the
        per-call user payload. Raises :class:`RefusalError` (terminal) or
        :class:`ProviderUnavailable` (retryable).
        """
        ...


__all__ = [
    "CallUsage",
    "DomainError",
    "LlmStructuredProvider",
    "ProviderUnavailable",
    "RefusalError",
    "SchemaT",
    "UsageAccumulator",
]
