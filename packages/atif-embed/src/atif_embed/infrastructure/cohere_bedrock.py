# SPDX-License-Identifier: Apache-2.0

"""Cohere Embed v4 on Bedrock: the ``EmbeddingProvider`` adapter.

The raw ``invoke_model`` call (:func:`_invoke_raw`) under a tenacity retry
over :data:`RETRY_CODES`, batch orchestration under an asyncio semaphore,
and the document-``int8`` / query-``float`` asymmetry. One process-wide
``bedrock-runtime`` client per ``(region, pool_size)``, with botocore
retries disabled so tenacity owns the policy.

Every botocore failure that reaches the caller is wrapped in an
:class:`~atif_embed.domain.errors.EmbeddingProviderUnavailable` (or
:class:`~atif_embed.domain.errors.EmbeddingResponseInvalid` for an
unreadable response shape): the CLI classifies ``DomainError`` and nothing
else, so an expired AWS session must not escape as a raw traceback.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import TYPE_CHECKING, Any, cast

from botocore.config import Config as BotoConfig
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionError as BotoConnectionError,
    EndpointConnectionError,
    ReadTimeoutError,
    SSLError,
)
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from atif_embed.domain.errors import (
    EmbeddingProviderUnavailable,
    EmbeddingResponseInvalid,
)
from atif_embed.domain.text_stamp import MAX_EMBEDDABLE_CHARS, clip_text

if TYPE_CHECKING:
    from collections.abc import Callable

    from tenacity import RetryCallState

    from atif_embed.infrastructure.settings import EmbedSettings

#: Bedrock error codes that tenacity should retry. TWIN of
#: ``atif_models.infrastructure.openai_bedrock.RETRY_CODES`` — the two
#: packages may not import each other (independence contract), so each pins
#: this set against the same literal in its own test suite. Dropping a
#: throttle code here turns a mid-backfill throttle into an aborted run.
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


def _loguru_before_sleep(level: str = "WARNING") -> Callable[[RetryCallState], None]:
    """Return a tenacity ``before_sleep`` callback that logs via loguru.

    Mirrors :func:`tenacity.before_sleep_log`'s message shape while keeping
    stdlib ``logging`` out of the loguru-only workspace.
    """

    def _before_sleep(retry_state: RetryCallState) -> None:
        if retry_state.outcome is None or retry_state.next_action is None:
            return
        if retry_state.fn is None:
            fn_name = "<unknown>"
        else:
            fn_name = getattr(retry_state.fn, "__qualname__", repr(retry_state.fn))
        if retry_state.outcome.failed:
            exc = retry_state.outcome.exception()
            verb, value = "raised", f"{exc.__class__.__name__}: {exc}"
        else:
            verb, value = "returned", retry_state.outcome.result()
        logger.log(
            level,
            "Retrying {} in {:.3g} seconds as it {} {}.",
            fn_name,
            retry_state.next_action.sleep,
            verb,
            value,
        )

    return _before_sleep


def _is_retryable(exc: BaseException) -> bool:
    """Return True if ``exc`` is a Bedrock error worth retrying.

    Two buckets:
    * ``ClientError`` with a code in :data:`RETRY_CODES`: service-level
      throttling and transient model failures.
    * Network-layer errors (SSL, connection, endpoint, read-timeout) that
      surface when long-running batches hit flaky TCP connections.
    """
    if isinstance(exc, SSLError | BotoConnectionError | EndpointConnectionError | ReadTimeoutError):
        return True
    if not isinstance(exc, ClientError):
        return False
    code = exc.response.get("Error", {}).get("Code")
    return code in RETRY_CODES


_CLIENT_LOCK = threading.Lock()
_CLIENT_CACHE: dict[tuple[str, int], Any] = {}


def _build_bedrock_client(settings: EmbedSettings) -> Any:
    """Return a process-wide ``bedrock-runtime`` client keyed on region + pool size.

    boto3 clients are thread-safe and intended to be shared; creating one per
    request wastes the TCP pool. ``max_pool_connections`` sizes to at least
    ``2 × embed_concurrency`` with a floor of 32 so concurrent batches never
    starve; ``retries.max_attempts=0`` disables botocore's retry loop so the
    tenacity decorator below sees errors immediately.
    """
    import boto3

    pool_size = max(32, settings.embed_concurrency * 2)
    key = (settings.region, pool_size)
    with _CLIENT_LOCK:
        client = _CLIENT_CACHE.get(key)
        if client is None:
            boto_cfg = BotoConfig(
                region_name=settings.region,
                retries={"max_attempts": 0, "mode": "adaptive"},
                max_pool_connections=pool_size,
                connect_timeout=10,
                read_timeout=600,
            )
            client = boto3.client("bedrock-runtime", config=boto_cfg)
            _CLIENT_CACHE[key] = client
        return client


@retry(
    # Cohere Embed v4 on Bedrock has a strict TPM bucket that replenishes over
    # tens of seconds; wait up to 60s between attempts and try up to 10 times
    # before surfacing the ThrottlingException.
    stop=stop_after_attempt(10),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    retry=retry_if_exception(_is_retryable),
    before_sleep=_loguru_before_sleep("WARNING"),
    reraise=True,
)
def _invoke_raw(client: Any, model_id: str, body: str) -> dict[str, Any]:
    """One retried ``invoke_model`` call, raising botocore errors unchanged.

    Wrapping into the domain taxonomy happens in the CALLER, not here:
    tenacity's ``retry_if_exception(_is_retryable)`` inspects the botocore
    exception type and error code, so a wrap at this level would make every
    throttle look non-retryable and abort the run on the first one.
    """
    resp = client.invoke_model(
        modelId=model_id,
        body=body,
        contentType="application/json",
        accept="application/json",
    )
    return cast("dict[str, Any]", json.loads(resp["body"].read()))


def _invoke_bedrock_sync(
    client: Any,
    model_id: str,
    texts: list[str],
    *,
    input_type: str,
    output_dimension: int,
    embedding_type: str,
) -> list[list[int]] | list[list[float]]:
    """Make one synchronous ``invoke_model`` call and return the vectors.

    Parameters
    ----------
    client
        A boto3 ``bedrock-runtime`` client.
    model_id
        Cohere Embed v4 model ID (direct or CRIS profile).
    texts
        Up to 96 strings, each already clipped by :func:`clip_text`.
    input_type
        Either ``"search_document"`` (corpus) or ``"search_query"``.
    output_dimension
        Target Matryoshka dimension: 256, 512, 1024, or 1536.
    embedding_type
        One of ``"int8"``, ``"float"``, ``"uint8"``, ``"binary"``, ``"ubinary"``.

    Returns
    -------
    list of list of int or float
        Flat list of vectors matching the order of ``texts``.

    Raises
    ------
    EmbeddingProviderUnavailable
        Any botocore failure — retryable codes only after the tenacity budget
        is spent, non-retryable ones (denied or expired credentials, rejected
        request) immediately.
    EmbeddingResponseInvalid
        The call succeeded but the payload carries no vectors under
        ``embeddings[embedding_type]``.
    """
    body = json.dumps(
        {
            "texts": texts,
            "input_type": input_type,
            "output_dimension": output_dimension,
            "embedding_types": [embedding_type],
            "truncate": "RIGHT",
        }
    )
    try:
        payload = _invoke_raw(client, model_id, body)
    except (ClientError, BotoCoreError) as exc:
        msg = f"Bedrock invoke_model failed for {model_id}: {type(exc).__name__}: {exc}"
        raise EmbeddingProviderUnavailable(msg) from exc
    except json.JSONDecodeError as exc:
        msg = f"Bedrock response for {model_id} is not valid JSON: {exc}"
        raise EmbeddingResponseInvalid(msg) from exc
    try:
        vectors = payload["embeddings"][embedding_type]
    except (KeyError, TypeError) as exc:
        # `_invoke_raw` CASTS the decoded body to `dict[str, Any]`; Bedrock is not
        # obliged to honour that, and this handler runs precisely when it did not
        # (the `TypeError` arm above). So the check is redundant to the declared
        # type and load-bearing at run time.
        shape = (
            sorted(payload)
            if isinstance(payload, dict)  # pyright: ignore[reportUnnecessaryIsInstance]
            else type(payload).__name__
        )
        msg = (
            f"Bedrock response for {model_id} has no embeddings[{embedding_type!r}] "
            f"(payload shape: {shape})"
        )
        raise EmbeddingResponseInvalid(msg) from exc
    # Untyped-JSON boundary: name the shape once here rather than returning Any.
    return cast("list[list[int]] | list[list[float]]", vectors)


async def _embed_one_batch(
    client: Any,
    texts: list[str],
    model_id: str,
    *,
    input_type: str,
    output_dimension: int,
    embedding_type: str,
    sem: asyncio.Semaphore,
) -> list[list[int]] | list[list[float]]:
    """Embed a single batch under a concurrency-limiting semaphore."""
    async with sem:
        return await asyncio.to_thread(
            _invoke_bedrock_sync,
            client,
            model_id,
            texts,
            input_type=input_type,
            output_dimension=output_dimension,
            embedding_type=embedding_type,
        )


class CohereBedrockEmbedder:
    """``EmbeddingProvider`` over Cohere Embed v4 on Amazon Bedrock.

    Documents embed at ``settings.embedding_type`` (``int8`` by default) and
    are float-widened on the way out; queries force ``embedding_type="float"``
    because the HNSW distance math needs float vectors. The Matryoshka
    ``output_dimension`` knob (256/512/1024/1536) is Cohere-specific and is
    the provider's fixed :attr:`dimension`.
    """

    provider = "cohere-bedrock"

    def __init__(self, settings: EmbedSettings) -> None:
        self._settings = settings

    @property
    def model_id(self) -> str:
        """The Bedrock model id this embedder invokes."""
        return self._settings.embed_model_id

    @property
    def dimension(self) -> int:
        """The output vector width this embedder requests from Cohere."""
        return int(self._settings.output_dimension)

    async def embed_documents(self, texts: list[str]) -> list[list[float] | None]:
        """Embed corpus documents in parallel; one slot per input text, in order.

        A slot is ``None`` when that text's batch failed terminally. Batches
        run under ``return_exceptions`` on purpose: one batch exhausting its
        retry budget must not discard its siblings' vectors, which were
        already billed. The caller writes the rows it got and lets the next
        run's staleness anti-join re-pick the ``None`` slots.

        Texts over :data:`MAX_EMBEDDABLE_CHARS` are clipped (logged, and flagged
        on the stored row) before the call.
        """
        if not texts:
            return []

        settings = self._settings
        client = _build_bedrock_client(settings)
        batch_size = settings.batch_size

        clipped: list[str] = []
        for position, text in enumerate(texts):
            sent, was_truncated = clip_text(text)
            if was_truncated:
                logger.warning(
                    "Clipping text at position {} for embedding: {} chars -> {} "
                    "(content past the cap will not match a search)",
                    position,
                    len(text),
                    MAX_EMBEDDABLE_CHARS,
                )
            clipped.append(sent)

        starts = list(range(0, len(clipped), batch_size))
        batches = [clipped[i : i + batch_size] for i in starts]
        sem = asyncio.Semaphore(settings.embed_concurrency)

        logger.info(
            "Embedding {} texts in {} batches (batch_size={}, concurrency={}, model={})",
            len(texts),
            len(batches),
            batch_size,
            settings.embed_concurrency,
            self.model_id,
        )

        t0 = time.monotonic()
        outcomes = await asyncio.gather(
            *(
                _embed_one_batch(
                    client,
                    batch,
                    self.model_id,
                    input_type="search_document",
                    output_dimension=self.dimension,
                    embedding_type=settings.embedding_type,
                    sem=sem,
                )
                for batch in batches
            ),
            return_exceptions=True,
        )
        elapsed = time.monotonic() - t0

        vectors: list[list[float] | None] = [None] * len(texts)
        failed_batches = 0
        for start, batch, outcome in zip(starts, batches, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                failed_batches += 1
                logger.error(
                    "Batch at offset {} ({} texts) failed terminally ({}: {}); "
                    "keeping the batches that succeeded — the next run re-picks these rows",
                    start,
                    len(batch),
                    type(outcome).__name__,
                    outcome,
                )
                continue
            for offset, vector in enumerate(outcome):
                vectors[start + offset] = [float(x) for x in vector]

        embedded = sum(1 for v in vectors if v is not None)
        logger.info(
            "Embedded {} vectors across {}/{} batches in {:.2f}s ({:.1f} vec/s)",
            embedded,
            len(batches) - failed_batches,
            len(batches),
            elapsed,
            embedded / elapsed if elapsed > 0 else 0.0,
        )
        return vectors

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string for HNSW nearest-neighbor search.

        Uses ``input_type="search_query"`` and forces ``embedding_type="float"``
        regardless of ``settings.embedding_type`` because HNSW distance math
        needs float vectors. Returns a single vector of length :attr:`dimension`.
        """
        client = _build_bedrock_client(self._settings)
        sent, was_truncated = clip_text(text)
        if was_truncated:
            logger.warning(
                "Clipping query for embedding: {} chars -> {}", len(text), MAX_EMBEDDABLE_CHARS
            )
        vectors = _invoke_bedrock_sync(
            client,
            self.model_id,
            [sent],
            input_type="search_query",
            output_dimension=self.dimension,
            embedding_type="float",
        )
        return [float(x) for x in vectors[0]]


__all__ = [
    "RETRY_CODES",
    "CohereBedrockEmbedder",
    "_build_bedrock_client",
    "_embed_one_batch",
    "_invoke_bedrock_sync",
    "_invoke_raw",
    "_is_retryable",
]
