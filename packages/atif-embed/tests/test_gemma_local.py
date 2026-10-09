# SPDX-License-Identifier: Apache-2.0

"""EmbeddingGemma 2 on this machine, without the model, the network, or torch.

The adapter's real loader is driven through fake ``torch`` and
``sentence_transformers`` modules placed in ``sys.modules``, so these tests
check what it ASKS the libraries for (text-only config, pinned revision,
device, dtype) whether or not the ``local`` extra is installed. The encode
side runs against a fake SentenceTransformer that records each call.
"""

from __future__ import annotations

import asyncio
import math
import subprocess
import sys
import types
from pathlib import Path
from typing import Any, override

import pytest
from embed_fixtures import EXPECTED_EMBEDDABLE_UUIDS, FakeEmbedder

from atif_embed.application.embed import embed_query, run_backfill
from atif_embed.domain.errors import (
    EmbeddingProviderMismatch,
    EmbeddingProviderNotInstalled,
    EmbeddingProviderUnavailable,
    EmbeddingResponseInvalid,
)
from atif_embed.domain.ports import EmbeddingProvider
from atif_embed.infrastructure import gemma_local
from atif_embed.infrastructure.cohere_bedrock import CohereBedrockEmbedder
from atif_embed.infrastructure.gemma_local import (
    BATCH_CHAR_BUDGET,
    DOCUMENT_PROMPT,
    MAX_TOKENS,
    QUERY_PROMPT,
    EmbeddingGemmaLocalEmbedder,
    load_sentence_transformer,
    plan_batches,
)
from atif_embed.infrastructure.lance_store import LanceVectorStore
from atif_embed.infrastructure.providers import build_embedder
from atif_embed.infrastructure.settings import EmbedSettings

GEMMA_MODEL = "google/embeddinggemma-2"
GEMMA_REVISION = "914f7f89142e33e77833254d9c9b90c3cef7303b"


def _settings(**overrides: Any) -> EmbedSettings:
    """Gemma settings that ignore the developer's own environment and .env."""
    values: dict[str, Any] = {"embed_provider": "gemma", "lance_uri": None, **overrides}
    return EmbedSettings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


class FakeSentenceTransformer:
    """Records every encode call; vectors depend on the text, the prompt and the width.

    ``fail_on`` makes a batch holding that text raise as a torch forward pass
    would; ``nan_on`` returns a NaN vector for that text, as float16 does.
    """

    def __init__(self, *, fail_on: str | None = None, nan_on: str | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail_on = fail_on
        self._nan_on = nan_on

    def encode(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        self.calls.append({"texts": list(texts), **kwargs})
        if self._fail_on is not None and self._fail_on in texts:
            msg = "CUDA out of memory"
            raise RuntimeError(msg)
        dim = int(kwargs["truncate_dim"])
        rows: list[list[float]] = []
        for text in texts:
            if text == self._nan_on:
                rows.append([math.nan] * dim)
                continue
            seed = sum(map(ord, kwargs["prompt_name"] + text)) or 1
            raw = [((seed * (i + 1)) % 97) / 97.0 + 0.01 for i in range(dim)]
            norm = math.sqrt(sum(x * x for x in raw))
            rows.append([x / norm for x in raw])
        return rows


def _embedder(
    model: FakeSentenceTransformer, **overrides: Any
) -> tuple[EmbeddingGemmaLocalEmbedder, list[EmbedSettings]]:
    """An embedder whose loader hands back ``model`` and counts the loads."""
    loads: list[EmbedSettings] = []

    def factory(settings: EmbedSettings) -> FakeSentenceTransformer:
        loads.append(settings)
        return model

    return EmbeddingGemmaLocalEmbedder(_settings(**overrides), model_factory=factory), loads


class TestSettings:
    def test_cohere_stays_the_default(self) -> None:
        settings = EmbedSettings(_env_file=None)  # pyright: ignore[reportCallIssue]
        assert settings.embed_provider == "cohere"
        assert settings.expected_embedding_identity() == ("global.cohere.embed-v4:0", 1024)
        assert settings.resolve_lance_uri(Path("/c")) == Path("/c/embeddings_lance")

    def test_gemma_has_its_own_store_identity_and_batching(self) -> None:
        settings = _settings()
        assert settings.expected_embedding_identity() == (GEMMA_MODEL, 768)
        assert settings.gemma_revision == GEMMA_REVISION
        assert settings.resolve_lance_uri(Path("/c")) == Path("/c/embeddings_lance_gemma")
        assert (settings.active_batch_size, settings.active_concurrency) == (32, 1)

    def test_lance_uri_overrides_either_provider(self, tmp_path: Path) -> None:
        assert _settings(lance_uri=tmp_path).resolve_lance_uri(Path("/c")) == tmp_path

    @pytest.mark.parametrize("dim", [768, 512, 256, 128])
    def test_gemma_takes_its_matryoshka_widths(self, dim: int) -> None:
        assert _settings(output_dimension=dim).embedding_dim == dim

    @pytest.mark.parametrize(("provider", "dim"), [("gemma", 1024), ("cohere", 768)])
    def test_a_width_the_provider_cannot_emit_is_refused(self, provider: str, dim: int) -> None:
        with pytest.raises(ValueError, match="is not a width"):
            _settings(embed_provider=provider, output_dimension=dim)

    def test_provider_and_width_read_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An environment variable is a string; the width must still parse from one."""
        monkeypatch.setenv("ATIF_SQL_EMBED_PROVIDER", "gemma")
        monkeypatch.setenv("ATIF_SQL_OUTPUT_DIMENSION", "256")
        settings = EmbedSettings(_env_file=None)  # pyright: ignore[reportCallIssue]
        assert settings.expected_embedding_identity() == (GEMMA_MODEL, 256)
        monkeypatch.setenv("ATIF_SQL_EMBED_PROVIDER", "cohere")
        monkeypatch.setenv("ATIF_SQL_OUTPUT_DIMENSION", "512")
        cohere = EmbedSettings(_env_file=None)  # pyright: ignore[reportCallIssue]
        assert cohere.expected_embedding_identity() == ("global.cohere.embed-v4:0", 512)


class TestBuildEmbedder:
    def test_dispatches_on_the_provider(self) -> None:
        assert isinstance(build_embedder(_settings()), EmbeddingGemmaLocalEmbedder)
        assert isinstance(build_embedder(_settings(embed_provider="cohere")), CohereBedrockEmbedder)

    def test_gemma_embedder_satisfies_the_port_and_loads_nothing(self) -> None:
        loads: list[EmbedSettings] = []
        provider: EmbeddingProvider = EmbeddingGemmaLocalEmbedder(
            _settings(), model_factory=loads.append
        )
        assert (provider.model_id, provider.dimension) == (GEMMA_MODEL, 768)
        for member in ("model_id", "dimension", "embed_documents", "embed_query"):
            assert hasattr(provider, member)
        assert loads == []


class TestEncode:
    def test_documents_use_the_document_prompt_width_and_normalization(self) -> None:
        model = FakeSentenceTransformer()
        embedder, loads = _embedder(model, output_dimension=128)
        vectors = asyncio.run(embedder.embed_documents(["alpha text", "beta"]))

        assert len(loads) == 1
        (call,) = model.calls
        assert call["prompt_name"] == DOCUMENT_PROMPT == "Document"
        assert call["truncate_dim"] == 128
        assert call["normalize_embeddings"] is True
        assert all(v is not None and len(v) == 128 for v in vectors)

    def test_query_uses_the_search_query_prompt(self) -> None:
        model = FakeSentenceTransformer()
        embedder, _ = _embedder(model, output_dimension=256)
        vector = embedder.embed_query("where is the flaky test")
        (call,) = model.calls
        assert call["prompt_name"] == QUERY_PROMPT == "SearchQuery"
        assert call["texts"] == ["where is the flaky test"]
        assert len(vector) == 256
        assert math.isclose(sum(x * x for x in vector), 1.0, rel_tol=1e-6)

    def test_query_and_document_of_one_text_differ(self) -> None:
        """A missing prompt would make the two sides one vector."""
        model = FakeSentenceTransformer()
        embedder, _ = _embedder(model)
        doc = asyncio.run(embedder.embed_documents(["same words"]))[0]
        assert doc != embedder.embed_query("same words")

    def test_the_model_loads_once_across_calls(self) -> None:
        embedder, loads = _embedder(FakeSentenceTransformer())
        asyncio.run(embedder.embed_documents(["one"]))
        asyncio.run(embedder.embed_documents(["two"]))
        embedder.embed_query("three")
        assert len(loads) == 1

    def test_batches_shortest_first_and_answers_in_input_order(self) -> None:
        model = FakeSentenceTransformer()
        embedder, _ = _embedder(model, gemma_batch_size=2)
        texts = ["cccccc", "a", "bbbb", "dd"]
        vectors = asyncio.run(embedder.embed_documents(texts))

        assert [c["texts"] for c in model.calls] == [["a", "dd"], ["bbbb", "cccccc"]]
        alone = [
            asyncio.run(_embedder(FakeSentenceTransformer())[0].embed_documents([t])) for t in texts
        ]
        assert vectors == [a[0] for a in alone]

    def test_a_long_text_never_pads_a_whole_batch(self) -> None:
        """32 texts padded to the 8K-token window took 26 GB of RSS on CPU."""
        full = BATCH_CHAR_BUDGET
        lengths = [100] * 40 + [full, full // 2, full // 2, full * 3]
        batches = plan_batches(lengths, max_texts=32)
        assert sorted(i for b in batches for i in b) == list(range(len(lengths)))
        for batch in batches:
            longest = max(lengths[i] for i in batch)
            assert len(batch) <= 32
            assert len(batch) == 1 or len(batch) * longest <= BATCH_CHAR_BUDGET
        assert [len(b) for b in batches] == [32, 8, 2, 1, 1]

    def test_long_texts_go_alone_through_the_encoder(self) -> None:
        model = FakeSentenceTransformer()
        embedder, _ = _embedder(model)
        long_text = "x" * BATCH_CHAR_BUDGET
        asyncio.run(
            embedder.embed_documents([long_text, "short one", "short two", long_text + "y"])
        )
        assert [len(c["texts"]) for c in model.calls] == [2, 1, 1]

    def test_a_failing_batch_leaves_none_slots_and_keeps_the_rest(self) -> None:
        model = FakeSentenceTransformer(fail_on="bbbb")
        embedder, _ = _embedder(model, gemma_batch_size=2)
        vectors = asyncio.run(embedder.embed_documents(["cccccc", "a", "bbbb", "dd"]))
        assert vectors[1] is not None
        assert vectors[3] is not None
        assert vectors[0] is None
        assert vectors[2] is None

    def test_a_non_finite_vector_is_dropped_not_stored(self) -> None:
        embedder, _ = _embedder(FakeSentenceTransformer(nan_on="bad"))
        vectors = asyncio.run(embedder.embed_documents(["good text", "bad"]))
        assert vectors[0] is not None
        assert vectors[1] is None

    def test_a_non_finite_query_vector_raises(self) -> None:
        embedder, _ = _embedder(FakeSentenceTransformer(nan_on="bad"))
        with pytest.raises(EmbeddingResponseInvalid):
            embedder.embed_query("bad")

    def test_a_failing_query_is_a_domain_error(self) -> None:
        embedder, _ = _embedder(FakeSentenceTransformer(fail_on="q"))
        with pytest.raises(EmbeddingProviderUnavailable, match="out of memory"):
            embedder.embed_query("q")

    def test_no_texts_loads_no_model(self) -> None:
        embedder, loads = _embedder(FakeSentenceTransformer())
        assert asyncio.run(embedder.embed_documents([])) == []
        assert loads == []


class _FakeDtype:
    def __init__(self, name: str) -> None:
        self.name = name

    @override
    def __repr__(self) -> str:
        return f"torch.{self.name}"


def _fake_torch(*, cuda: bool, bf16: bool, mps: bool = False) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        float16=_FakeDtype("float16"),
        bfloat16=_FakeDtype("bfloat16"),
        float32=_FakeDtype("float32"),
        cuda=types.SimpleNamespace(is_available=lambda: cuda, is_bf16_supported=lambda: bf16),
        backends=types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: mps)),
    )


@pytest.fixture
def loader_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """A fake ``sentence_transformers`` module recording each SentenceTransformer(...)."""
    calls: list[dict[str, Any]] = []

    def sentence_transformer(name: str, **kwargs: Any) -> types.SimpleNamespace:
        calls.append({"name": name, **kwargs})
        return types.SimpleNamespace(name="model", max_seq_length=10**30)

    module = types.SimpleNamespace(SentenceTransformer=sentence_transformer)
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    return calls


class TestLoader:
    def _load(
        self, monkeypatch: pytest.MonkeyPatch, torch: types.SimpleNamespace, **overrides: Any
    ) -> None:
        monkeypatch.setitem(sys.modules, "torch", torch)
        model = load_sentence_transformer(_settings(**overrides))
        assert model.name == "model"
        # The tokenizer declares no maximum; the loader caps it at the model's window.
        assert model.max_seq_length == MAX_TOKENS == 8192

    def test_loads_text_only_at_the_pinned_revision(
        self, monkeypatch: pytest.MonkeyPatch, loader_calls: list[dict[str, Any]]
    ) -> None:
        self._load(monkeypatch, _fake_torch(cuda=False, bf16=False))
        (call,) = loader_calls
        assert call["name"] == GEMMA_MODEL
        assert call["revision"] == GEMMA_REVISION
        assert call["config_kwargs"] == {"vision_config": None, "audio_config": None}
        assert call["device"] == "cpu"
        assert call["model_kwargs"]["dtype"].name == "float32"

    @pytest.mark.parametrize(
        ("cuda", "bf16", "mps", "device", "dtype"),
        [
            (True, True, False, "cuda", "bfloat16"),
            (True, False, False, "cuda", "float32"),
            (False, False, True, "mps", "float32"),
            (False, False, False, "cpu", "float32"),
        ],
    )
    def test_device_and_dtype_never_float16(
        self,
        monkeypatch: pytest.MonkeyPatch,
        loader_calls: list[dict[str, Any]],
        cuda: bool,
        bf16: bool,
        mps: bool,
        device: str,
        dtype: str,
    ) -> None:
        self._load(monkeypatch, _fake_torch(cuda=cuda, bf16=bf16, mps=mps))
        (call,) = loader_calls
        assert (call["device"], call["model_kwargs"]["dtype"].name) == (device, dtype)

    def test_an_explicit_cpu_device_wins_over_cuda(
        self, monkeypatch: pytest.MonkeyPatch, loader_calls: list[dict[str, Any]]
    ) -> None:
        self._load(monkeypatch, _fake_torch(cuda=True, bf16=True), gemma_device="cpu")
        assert loader_calls[0]["device"] == "cpu"
        assert loader_calls[0]["model_kwargs"]["dtype"].name == "float32"

    def test_a_load_failure_is_a_domain_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def offline(*_args: Any, **_kwargs: Any) -> None:
            msg = "We couldn't connect to 'https://huggingface.co'"
            raise OSError(msg)

        monkeypatch.setitem(sys.modules, "torch", _fake_torch(cuda=False, bf16=False))
        monkeypatch.setitem(
            sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=offline)
        )
        with pytest.raises(EmbeddingProviderUnavailable, match="couldn't connect"):
            load_sentence_transformer(_settings())

    @pytest.mark.parametrize("missing", ["torch", "sentence_transformers"])
    def test_a_missing_extra_names_the_install(
        self, monkeypatch: pytest.MonkeyPatch, loader_calls: list[dict[str, Any]], missing: str
    ) -> None:
        monkeypatch.setitem(sys.modules, "torch", _fake_torch(cuda=False, bf16=False))
        monkeypatch.setitem(sys.modules, missing, None)
        with pytest.raises(EmbeddingProviderNotInstalled) as excinfo:
            load_sentence_transformer(_settings())
        assert excinfo.value.terminal is True
        assert "--extra local" in str(excinfo.value)
        assert "atif-sql[local]" in str(excinfo.value)
        assert loader_calls == []

    def test_the_default_factory_is_the_real_loader(self) -> None:
        embedder = EmbeddingGemmaLocalEmbedder(_settings())
        assert embedder._factory is gemma_local.load_sentence_transformer


class TestBackfill:
    def test_writes_rows_stamped_with_the_gemma_identity(
        self, corpus_root: Path, tmp_path: Path
    ) -> None:
        settings = _settings(lance_uri=tmp_path / "lance", output_dimension=128)
        embedder, _ = _embedder(FakeSentenceTransformer(), output_dimension=128)
        store = LanceVectorStore(settings.resolve_lance_uri(corpus_root), dim=128)
        written = asyncio.run(
            run_backfill(corpus_root=corpus_root, settings=settings, embedder=embedder, store=store)
        )
        assert written == len(EXPECTED_EMBEDDABLE_UUIDS)
        assert store.table_identity() == (GEMMA_MODEL, 128)

    def test_dry_run_plans_the_local_provider(self, corpus_root: Path) -> None:
        settings = _settings()
        plan = asyncio.run(run_backfill(corpus_root=corpus_root, settings=settings, dry_run=True))
        assert isinstance(plan, dict)
        assert plan["provider"] == "gemma"
        assert (plan["model"], plan["dim"]) == (GEMMA_MODEL, 768)
        assert plan["store"] == str(corpus_root / "embeddings_lance_gemma")
        assert (plan["batch_size"], plan["concurrency"]) == (32, 1)
        assert not (corpus_root / "embeddings_lance").exists()

    def test_appending_gemma_vectors_to_a_cohere_store_is_refused(
        self, corpus_root: Path, tmp_path: Path
    ) -> None:
        uri = tmp_path / "shared"
        cohere = EmbedSettings(_env_file=None, lance_uri=uri, output_dimension=256)  # pyright: ignore[reportCallIssue]
        asyncio.run(
            run_backfill(
                corpus_root=corpus_root,
                settings=cohere,
                embedder=FakeEmbedder(model_id="global.cohere.embed-v4:0", dim=256),
                store=LanceVectorStore(uri, dim=256),
                limit=1,
            )
        )
        gemma = _settings(lance_uri=uri, output_dimension=256)
        embedder, _ = _embedder(FakeSentenceTransformer(), output_dimension=256)
        with pytest.raises(EmbeddingProviderMismatch) as excinfo:
            asyncio.run(
                run_backfill(
                    corpus_root=corpus_root,
                    settings=gemma,
                    embedder=embedder,
                    store=LanceVectorStore(uri, dim=256),
                )
            )
        assert excinfo.value.terminal is True

    def test_embed_query_goes_through_the_selected_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        model = FakeSentenceTransformer()

        def load(_settings: EmbedSettings) -> FakeSentenceTransformer:
            return model

        monkeypatch.setattr(gemma_local, "load_sentence_transformer", load)
        vector = embed_query("flaky test", settings=_settings(output_dimension=512))
        assert len(vector) == 512
        assert model.calls[0]["prompt_name"] == QUERY_PROMPT


def test_the_cohere_path_loads_no_local_model_library() -> None:
    """Building and planning with the default provider imports no torch stack.

    Meaningful when the ``local`` extra is installed, where an eager import
    would cost seconds on every Cohere run; without the extra the import
    could not succeed anyway.
    """
    probe = (
        "import sys\n"
        "from atif_embed.application import embed\n"
        "from atif_embed.infrastructure.providers import build_embedder\n"
        "from atif_embed.infrastructure.settings import EmbedSettings\n"
        "build_embedder(EmbedSettings(_env_file=None, embed_provider='cohere'))\n"
        "from atif_embed.infrastructure import gemma_local\n"
        "heavy = ('torch', 'transformers', 'sentence_transformers')\n"
        "print('|'.join(sorted(m for m in sys.modules if m.split('.')[0] in heavy)))\n"
    )
    result = subprocess.run(  # noqa: S603 — argv is this interpreter plus a literal probe
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr[-500:]
    assert result.stdout.strip() == ""
