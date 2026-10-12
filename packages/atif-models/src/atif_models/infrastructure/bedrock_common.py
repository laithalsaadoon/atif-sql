# SPDX-License-Identifier: Apache-2.0

"""What both Bedrock structured-output adapters share.

The retryable error set and its predicate, the backoff log line, the one-step
effort ladder for a truncated response, the truncation marker, and the
``bedrock-runtime`` client both adapters build the same way.
"""

from __future__ import annotations

from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import (
    ClientError,
    ConnectionError as BotoConnectionError,
    EndpointConnectionError,
    ReadTimeoutError,
    SSLError,
)
from loguru import logger

from atif_models.domain.ports import ProviderUnavailable

#: Bedrock error codes worth retrying. TWIN of
#: ``atif_embed.infrastructure.cohere_bedrock.RETRY_CODES`` — the two
#: packages may not import each other (independence contract), so each pins
#: this set against the same literal in its own test suite.
RETRY_CODES: frozenset[str] = frozenset(
    {
        "ThrottlingException",
        "ServiceUnavailableException",
        "ModelTimeoutException",
        "ModelErrorException",
        "ProvisionedThroughputExceededException",
        "TooManyRequestsException",
        "InternalServerException",
        "InternalFailure",
    }
)

#: One-step reasoning-effort degradation ladder for the truncation retry.
#: Truncation is DETERMINISTIC for a fixed (prompt, parameters) pair —
#: resending the same request would truncate identically and just double the
#: bill — so the single retry changes a parameter: less reasoning leaves more
#: of the completion budget for the answer. Efforts at "low" or below don't
#: retry (nothing left to degrade to).
EFFORT_DEGRADE: dict[str, str] = {
    "max": "xhigh",
    "xhigh": "high",
    "high": "medium",
    "medium": "low",
}


class LengthTruncation(ProviderUnavailable):
    """Internal marker for a response cut off at the completion-token limit.

    Callers only ever see :class:`ProviderUnavailable`; each adapter catches
    this one to run the one-shot effort-degrade retry.
    """


def is_retryable(exc: BaseException) -> bool:
    """True for throttle/service ``ClientError`` codes + network errors."""
    if isinstance(exc, SSLError | BotoConnectionError | EndpointConnectionError | ReadTimeoutError):
        return True
    if not isinstance(exc, ClientError):
        return False
    code = exc.response.get("Error", {}).get("Code")
    return code in RETRY_CODES


def log_before_sleep(retry_state: Any) -> None:
    """Log one WARNING per backoff (loguru — stdlib logging is banned)."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    logger.warning(
        "bedrock invoke retry {}/10 in {:.1f}s after {}: {}",
        retry_state.attempt_number,
        getattr(retry_state.next_action, "sleep", 0.0),
        type(exc).__name__ if exc else "?",
        exc,
    )


def build_runtime_client(*, region: str, pool_size: int) -> Any:
    """One thread-safe ``bedrock-runtime`` client for an adapter.

    botocore retries are OFF (``max_attempts=0``) — each adapter's tenacity
    decorator owns the retry policy. ``read_timeout=600`` because high
    reasoning effort can hold the connection well past botocore's 60s
    default.
    """
    boto_cfg = BotoConfig(
        region_name=region,
        retries={"max_attempts": 0, "mode": "standard"},
        max_pool_connections=pool_size,
        connect_timeout=10,
        read_timeout=600,
    )
    return boto3.client("bedrock-runtime", config=boto_cfg)


__all__ = [
    "EFFORT_DEGRADE",
    "RETRY_CODES",
    "LengthTruncation",
    "build_runtime_client",
    "is_retryable",
    "log_before_sleep",
]
