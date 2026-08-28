# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Cohere-on-Bedrock adapter: body shape, batching, clip.

boto3 is fully mocked — NO live Bedrock calls. The stub client records every
``invoke_model`` body so the tests can pin the exact wire shape (input_type,
truncate RIGHT, embedding_types, output_dimension, 50K clip) and the batch
fan-out (96 per call, semaphore-bounded).
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any, override

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError

from atif_embed.domain.errors import (
    DomainError,
    EmbeddingProviderUnavailable,
    EmbeddingResponseInvalid,
)
from atif_embed.domain.text_stamp import MAX_EMBEDDABLE_CHARS
from atif_embed.infrastructure.cohere_bedrock import (
    RETRY_CODES,
    CohereBedrockEmbedder,
    _invoke_bedrock_sync,
    _invoke_raw,
    _is_retryable,
)
from atif_embed.infrastructure.settings import EmbedSettings

#: The retryable Bedrock code set, spelled out independently of the module
#: under test. atif-models' openai adapter carries a TWIN of this set and
#: pins it against the SAME literal in its own suite; the two packages may
#: not import each other, so a shared literal on both sides is what catches
#: drift between the copies.
BEDROCK_RETRY_CODES = frozenset(
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


class StubBedrockClient:
    """Records invoke_model bodies; returns right-shaped vectors."""

    def __init__(self, dim: int = 4) -> None:
        self.dim = dim
        self.bodies: list[dict[str, Any]] = []

    def invoke_model(self, *, modelId: str, body: str, **_: Any) -> dict[str, Any]:  # noqa: N803 — boto3 kwarg
        parsed = json.loads(body)
        self.bodies.append(parsed)
        etype = parsed["embedding_types"][0]
        raw: list[list[float]] | list[list[int]]
        if etype == "int8":
            raw = [[i % 127 for i in range(self.dim)] for _ in parsed["texts"]]
        else:
            raw = [[0.5] * self.dim for _ in parsed["texts"]]
        payload = {"embeddings": {etype: raw}}
        return {"body": io.BytesIO(json.dumps(payload).encode())}


class FailingNthBatchClient(StubBedrockClient):
    """Fails the Nth invoke_model call terminally; succeeds on the rest."""

    def __init__(self, fail_call: int, exc: BaseException, dim: int = 4) -> None:
        super().__init__(dim=dim)
        self._fail_call = fail_call
        self._exc = exc
        self.calls = 0

    @override
    def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self.calls == self._fail_call:
            # Record the body so batch-order assertions still work.
            self.bodies.append(json.loads(kwargs["body"]))
            raise self._exc
        return super().invoke_model(**kwargs)


@pytest.fixture
def settings(tmp_path: Path) -> EmbedSettings:
    return EmbedSettings(
        output_dimension=1024,
        batch_size=96,
        embed_concurrency=8,
        lance_uri=tmp_path / "lance",
    )


@pytest.fixture
# An autouse-by-usefixtures fixture is resolved by REGISTRATION, so no
# reference to the function exists for a checker to see.
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """Collapse tenacity's exponential waits so retry tests are instant."""
    # tenacity's @retry attaches `.retry` to the wrapped function at run time
    # and its annotations do not describe the attribute.
    wait = _invoke_raw.retry.wait  # ty: ignore[unresolved-attribute]  # pyright: ignore[reportFunctionMemberAccess]
    monkeypatch.setattr(wait, "multiplier", 0)
    monkeypatch.setattr(wait, "min", 0)


def _bind(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def _stub_client_factory(_settings: EmbedSettings) -> Any:
        return client

    monkeypatch.setattr(
        "atif_embed.infrastructure.cohere_bedrock._build_bedrock_client",
        _stub_client_factory,
    )


class TestInvokeBody:
    def test_document_body_shape(self) -> None:
        client = StubBedrockClient()
        _invoke_bedrock_sync(
            client,
            "global.cohere.embed-v4:0",
            ["hello world"],
            input_type="search_document",
            output_dimension=1024,
            embedding_type="int8",
        )
        body = client.bodies[0]
        assert body == {
            "texts": ["hello world"],
            "input_type": "search_document",
            "output_dimension": 1024,
            "embedding_types": ["int8"],
            "truncate": "RIGHT",
        }


class TestEmbedder:
    def test_documents_batch_at_96_and_float_widen(
        self, settings: EmbedSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = StubBedrockClient()
        _bind(client, monkeypatch)
        embedder = CohereBedrockEmbedder(settings)
        texts = [f"text {i}" for i in range(200)]
        vectors = asyncio.run(embedder.embed_documents(texts))

        # 200 texts at batch 96 -> 3 calls (96 + 96 + 8).
        assert sorted(len(b["texts"]) for b in client.bodies) == [8, 96, 96]
        assert all(b["input_type"] == "search_document" for b in client.bodies)
        assert all(b["embedding_types"] == ["int8"] for b in client.bodies)
        assert len(vectors) == 200
        assert all(v is not None for v in vectors)
        # int8 responses are float-widened on the way out.
        assert vectors[0] is not None
        assert all(isinstance(x, float) for x in vectors[0])

    def test_vectors_are_positionally_aligned_with_texts(
        self, settings: EmbedSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = StubBedrockClient()
        _bind(client, monkeypatch)
        vectors = asyncio.run(
            CohereBedrockEmbedder(settings).embed_documents([f"t{i}" for i in range(200)])
        )
        assert len(vectors) == 200

    def test_empty_input_short_circuits(self, settings: EmbedSettings) -> None:
        embedder = CohereBedrockEmbedder(settings)
        assert asyncio.run(embedder.embed_documents([])) == []

    def test_query_forces_float(
        self, settings: EmbedSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = StubBedrockClient()
        _bind(client, monkeypatch)
        embedder = CohereBedrockEmbedder(settings)
        vec = embedder.embed_query("what broke last night?")
        body = client.bodies[0]
        assert body["input_type"] == "search_query"
        assert body["embedding_types"] == ["float"]  # forced despite int8 setting
        assert len(vec) == client.dim

    def test_identity_properties(self, settings: EmbedSettings) -> None:
        embedder = CohereBedrockEmbedder(settings)
        assert embedder.model_id == "global.cohere.embed-v4:0"
        assert embedder.dimension == 1024


class TestTruncation:
    def test_texts_clipped_to_the_cap(
        self, settings: EmbedSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = StubBedrockClient()
        _bind(client, monkeypatch)
        huge = "x" * (MAX_EMBEDDABLE_CHARS + 1000)
        asyncio.run(CohereBedrockEmbedder(settings).embed_documents([huge]))
        assert len(client.bodies[0]["texts"][0]) == MAX_EMBEDDABLE_CHARS

    def test_clipping_logs_a_warning_naming_the_position(
        self, settings: EmbedSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Silent clipping is unattributable: the log must say it happened."""
        from loguru import logger

        records: list[str] = []
        sink_id = logger.add(lambda m: records.append(m.record["message"]), level="WARNING")
        try:
            client = StubBedrockClient()
            _bind(client, monkeypatch)
            huge = "x" * (MAX_EMBEDDABLE_CHARS + 1)
            asyncio.run(CohereBedrockEmbedder(settings).embed_documents(["short", huge]))
        finally:
            logger.remove(sink_id)
        assert any("Clipping text at position 1" in m for m in records)

    def test_short_text_logs_nothing(
        self, settings: EmbedSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from loguru import logger

        records: list[str] = []
        sink_id = logger.add(lambda m: records.append(m.record["message"]), level="WARNING")
        try:
            client = StubBedrockClient()
            _bind(client, monkeypatch)
            asyncio.run(CohereBedrockEmbedder(settings).embed_documents(["short"]))
        finally:
            logger.remove(sink_id)
        assert not any("Clipping" in m for m in records)


class TestPartialBatchFailure:
    """A terminally-failed batch must not discard its siblings' billed vectors."""

    @pytest.mark.usefixtures("_no_backoff")
    def test_sibling_batches_survive_a_terminal_batch_failure(
        self, settings: EmbedSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = FailingNthBatchClient(
            fail_call=2,
            exc=ClientError(
                {"Error": {"Code": "ValidationException", "Message": "bad batch"}},
                "InvokeModel",
            ),
        )
        _bind(client, monkeypatch)
        settings = settings.model_copy(update={"batch_size": 2, "embed_concurrency": 1})
        vectors = asyncio.run(
            CohereBedrockEmbedder(settings).embed_documents(["a", "b", "c", "d", "e", "f"])
        )
        # Batch 2 (texts c, d) is gone; batches 1 and 3 survived.
        assert [v is None for v in vectors] == [False, False, True, True, False, False]

    @pytest.mark.usefixtures("_no_backoff")
    def test_every_batch_failing_yields_all_none_not_an_exception(
        self, settings: EmbedSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class AlwaysFails(StubBedrockClient):
            @override
            def invoke_model(self, **_: Any) -> dict[str, Any]:
                raise NoCredentialsError

        _bind(AlwaysFails(), monkeypatch)
        settings = settings.model_copy(update={"batch_size": 1, "embed_concurrency": 1})
        vectors = asyncio.run(CohereBedrockEmbedder(settings).embed_documents(["a", "b"]))
        assert vectors == [None, None]


class TestErrorTaxonomy:
    """Every adapter failure must be a DomainError; the CLI catches nothing else."""

    @pytest.mark.usefixtures("_no_backoff")
    def test_non_retryable_client_error_becomes_provider_unavailable(self) -> None:
        class Denies(StubBedrockClient):
            @override
            def invoke_model(self, **_: Any) -> dict[str, Any]:
                raise ClientError(
                    {"Error": {"Code": "ExpiredTokenException", "Message": "expired"}},
                    "InvokeModel",
                )

        with pytest.raises(EmbeddingProviderUnavailable, match="ExpiredTokenException"):
            _invoke_bedrock_sync(
                Denies(),
                "m",
                ["t"],
                input_type="search_document",
                output_dimension=4,
                embedding_type="int8",
            )

    @pytest.mark.usefixtures("_no_backoff")
    def test_botocore_error_becomes_provider_unavailable(self) -> None:
        class NoCreds(StubBedrockClient):
            @override
            def invoke_model(self, **_: Any) -> dict[str, Any]:
                raise NoCredentialsError

        with pytest.raises(EmbeddingProviderUnavailable) as excinfo:
            _invoke_bedrock_sync(
                NoCreds(),
                "m",
                ["t"],
                input_type="search_document",
                output_dimension=4,
                embedding_type="int8",
            )
        assert isinstance(excinfo.value, DomainError)

    @pytest.mark.usefixtures("_no_backoff")
    def test_retryable_error_becomes_provider_unavailable_after_the_budget(self) -> None:
        class Throttles(StubBedrockClient):
            @override
            def invoke_model(self, **_: Any) -> dict[str, Any]:
                raise ClientError(
                    {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
                    "InvokeModel",
                )

        with pytest.raises(EmbeddingProviderUnavailable, match="ThrottlingException"):
            _invoke_bedrock_sync(
                Throttles(),
                "m",
                ["t"],
                input_type="search_document",
                output_dimension=4,
                embedding_type="int8",
            )

    def test_missing_embeddings_key_becomes_response_invalid(self) -> None:
        class WrongShape(StubBedrockClient):
            @override
            def invoke_model(self, **_: Any) -> dict[str, Any]:
                return {"body": io.BytesIO(json.dumps({"unexpected": True}).encode())}

        with pytest.raises(EmbeddingResponseInvalid, match="no embeddings"):
            _invoke_bedrock_sync(
                WrongShape(),
                "m",
                ["t"],
                input_type="search_document",
                output_dimension=4,
                embedding_type="int8",
            )

    def test_missing_embedding_type_becomes_response_invalid(self) -> None:
        class WrongType(StubBedrockClient):
            @override
            def invoke_model(self, **_: Any) -> dict[str, Any]:
                return {"body": io.BytesIO(json.dumps({"embeddings": {"float": [[1.0]]}}).encode())}

        with pytest.raises(EmbeddingResponseInvalid, match="int8"):
            _invoke_bedrock_sync(
                WrongType(),
                "m",
                ["t"],
                input_type="search_document",
                output_dimension=4,
                embedding_type="int8",
            )

    def test_non_json_body_becomes_response_invalid(self) -> None:
        class Garbage(StubBedrockClient):
            @override
            def invoke_model(self, **_: Any) -> dict[str, Any]:
                return {"body": io.BytesIO(b"not json {")}

        with pytest.raises(EmbeddingResponseInvalid, match="not valid JSON"):
            _invoke_bedrock_sync(
                Garbage(),
                "m",
                ["t"],
                input_type="search_document",
                output_dimension=4,
                embedding_type="int8",
            )


class TestRetryPolicy:
    def test_throttling_is_retryable(self) -> None:
        exc = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "InvokeModel",
        )
        assert _is_retryable(exc)

    def test_validation_error_is_not_retryable(self) -> None:
        exc = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "bad input"}},
            "InvokeModel",
        )
        assert not _is_retryable(exc)

    def test_network_errors_are_retryable(self) -> None:
        assert _is_retryable(EndpointConnectionError(endpoint_url="https://x"))

    def test_generic_exception_is_not_retryable(self) -> None:
        assert not _is_retryable(ValueError("nope"))

    @pytest.mark.usefixtures("_no_backoff")
    def test_throttle_then_success_does_not_abort_the_run(self) -> None:
        class ThrottlesOnce(StubBedrockClient):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            @override
            def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
                self.calls += 1
                if self.calls == 1:
                    raise ClientError(
                        {"Error": {"Code": "TooManyRequestsException", "Message": "x"}},
                        "InvokeModel",
                    )
                return super().invoke_model(**kwargs)

        client = ThrottlesOnce()
        vectors = _invoke_bedrock_sync(
            client,
            "m",
            ["t"],
            input_type="search_document",
            output_dimension=4,
            embedding_type="int8",
        )
        assert client.calls == 2
        assert len(vectors) == 1


class TestRetryCodePin:
    """Pin the retryable set so it cannot drift from atif-models' twin copy."""

    def test_retry_codes_match_the_shared_set(self) -> None:
        assert RETRY_CODES == BEDROCK_RETRY_CODES

    @pytest.mark.parametrize("code", sorted(BEDROCK_RETRY_CODES))
    def test_every_shared_code_is_retryable_here(self, code: str) -> None:
        assert _is_retryable(ClientError({"Error": {"Code": code, "Message": "x"}}, "InvokeModel"))

    @pytest.mark.usefixtures("_no_backoff")
    @pytest.mark.parametrize("code", sorted(BEDROCK_RETRY_CODES))
    def test_every_shared_code_survives_the_real_retry_seam(self, code: str) -> None:
        """Membership is not the behavior: drive each code through ``_invoke_raw``.

        A set the retry decorator does not consult would satisfy a membership
        assertion while every throttle aborted the run, so the call is made for
        real and the second attempt must return the vector.
        """

        class FailsOnceWith(StubBedrockClient):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            @override
            def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
                self.calls += 1
                if self.calls == 1:
                    raise ClientError({"Error": {"Code": code, "Message": "x"}}, "InvokeModel")
                return super().invoke_model(**kwargs)

        client = FailsOnceWith()
        vectors = _invoke_bedrock_sync(
            client,
            "m",
            ["t"],
            input_type="search_document",
            output_dimension=4,
            embedding_type="int8",
        )
        assert client.calls == 2
        assert len(vectors) == 1

    @pytest.mark.usefixtures("_no_backoff")
    def test_a_non_retryable_code_is_not_retried(self) -> None:
        """The seam must discriminate: a validation error costs exactly one call."""

        class AlwaysRejects(StubBedrockClient):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            @override
            def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
                self.calls += 1
                raise ClientError(
                    {"Error": {"Code": "ValidationException", "Message": "bad"}},
                    "InvokeModel",
                )

        client = AlwaysRejects()
        with pytest.raises(EmbeddingProviderUnavailable):
            _invoke_bedrock_sync(
                client,
                "m",
                ["t"],
                input_type="search_document",
                output_dimension=4,
                embedding_type="int8",
            )
        assert client.calls == 1
