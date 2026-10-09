# SPDX-License-Identifier: Apache-2.0

"""EmbeddingGemma 2 on this machine: the local ``EmbeddingProvider`` adapter.

Loads ``google/embeddinggemma-2`` through sentence-transformers in its
text-only setup (``config_kwargs`` drops the vision and audio encoders, so
270M of the model's 740M parameters load), at a pinned repository revision.
Documents encode under the model's ``Document`` prompt (``title: none |
text: ``) and queries under ``SearchQuery`` (``task: search result | query:
``), truncated to the configured Matryoshka width and re-normalized.

Numerical precision follows the model card: bfloat16 on a CUDA device that
supports it, float32 everywhere else, and never float16, whose range the
model's activations exceed (it returns NaN or degraded vectors without
raising). A non-finite vector is dropped as a failed slot rather than stored.

The heavy libraries (torch, transformers, sentence-transformers) come from
the optional ``local`` extra and are imported by name at load time, never at
module scope: the default Cohere path, CI, and both type checkers run without
them. Selecting this provider without the extra raises
:class:`~atif_embed.domain.errors.EmbeddingProviderNotInstalled`.

The model downloads once into the Hugging Face cache (about 1.5 GB; the
checkpoint is one file even when only the text tower loads) and later runs
read it from there; ``HF_HUB_OFFLINE=1`` keeps every run off the network.
"""

from __future__ import annotations

import asyncio
import importlib
import math
import threading
import time
from typing import TYPE_CHECKING, Any

from loguru import logger

from atif_embed.domain.errors import (
    EmbeddingProviderNotInstalled,
    EmbeddingProviderUnavailable,
    EmbeddingResponseInvalid,
)
from atif_embed.domain.text_stamp import MAX_EMBEDDABLE_CHARS, clip_text

if TYPE_CHECKING:
    from collections.abc import Callable

    from atif_embed.infrastructure.settings import EmbedSettings

#: sentence-transformers prompt names from the model's own
#: ``config_sentence_transformers.json``: the corpus side and the query side
#: of its asymmetric search task.
DOCUMENT_PROMPT = "Document"
QUERY_PROMPT = "SearchQuery"

#: ``AutoConfig`` overrides that load the text tower alone. Text-only and
#: full-model embeddings share one vector space, so the stamped model id
#: carries no mode suffix.
TEXT_ONLY_CONFIG: dict[str, None] = {"vision_config": None, "audio_config": None}

#: The model's context window. The checkpoint's tokenizer declares no
#: maximum (sentence-transformers reads it as 10**30), so without this cap a
#: 50,000-character step would run past the window the model was trained on.
MAX_TOKENS = 8192

#: How to get the extra, named in the error a missing install raises.
INSTALL_HINT = (
    "install the local extra: `uv sync --all-packages --extra local` in a checkout, or "
    "`uv tool install 'atif-sql[local]' --torch-backend cpu`; or unset "
    "ATIF_SQL_EMBED_PROVIDER to embed with Cohere on Bedrock"
)

#: What a forward pass can raise that one batch's loss should absorb: torch
#: raises RuntimeError (its out-of-memory error included), tokenizers raise
#: ValueError, and a device fault surfaces as OSError.
_BATCH_ERRORS = (RuntimeError, ValueError, OSError)

#: Padded characters one forward pass may hold: its text count times its
#: longest text. A batch pads every text to its longest, and attention memory
#: grows with the square of that length, so 32 texts near the model's
#: 8,192-token window took 26 GB of RSS on CPU while short texts batch freely.
#: At about 4 characters a token this is one full-window text per pass.
BATCH_CHAR_BUDGET = 32_000


def plan_batches(lengths: list[int], *, max_texts: int) -> list[list[int]]:
    """Group text positions shortest first into batches a forward pass can hold.

    A batch closes at ``max_texts`` texts, or when the next (longer) text
    would push ``texts x longest`` past :data:`BATCH_CHAR_BUDGET`. A text
    over the budget on its own still gets a batch of one.
    """
    batches: list[list[int]] = []
    current: list[int] = []
    for position in sorted(range(len(lengths)), key=lambda i: lengths[i]):
        if current and (
            len(current) >= max_texts or (len(current) + 1) * lengths[position] > BATCH_CHAR_BUDGET
        ):
            batches.append(current)
            current = []
        current.append(position)
    if current:
        batches.append(current)
    return batches


def _import(name: str) -> Any:
    """Import one module of the ``local`` extra, or say how to install it.

    By name rather than as a statement: the extra is optional, and the type
    checkers and CI run without it, so a static import would not resolve.
    """
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        msg = f"the gemma embedding provider needs {name}, which is not installed; {INSTALL_HINT}"
        raise EmbeddingProviderNotInstalled(msg) from exc


def pick_device(requested: str, torch: Any) -> str:
    """The device to load on: ``requested``, or for ``auto`` CUDA, then MPS, then CPU."""
    if requested != "auto":
        return requested
    if torch.cuda.is_available():
        return "cuda"
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"


def pick_dtype(device: str, torch: Any) -> Any:
    """bfloat16 on a CUDA device that supports it, else float32; never float16."""
    if device.startswith("cuda") and torch.cuda.is_bf16_supported():
        return torch.bfloat16
    return torch.float32


def load_sentence_transformer(settings: EmbedSettings) -> Any:
    """Load EmbeddingGemma 2 text-only at the pinned revision, on the chosen device.

    Raises
    ------
    EmbeddingProviderNotInstalled
        torch or sentence-transformers is not installed.
    EmbeddingProviderUnavailable
        The model could not be fetched or loaded (no network and no cached
        copy, a revision the hub does not have, a device that is not there).
    """
    torch = _import("torch")
    sentence_transformers = _import("sentence_transformers")
    device = pick_device(settings.gemma_device, torch)
    dtype = pick_dtype(device, torch)
    logger.info(
        "Loading {} at {} text-only on {} ({})",
        settings.gemma_model_id,
        settings.gemma_revision[:12],
        device,
        str(dtype).removeprefix("torch."),
    )
    try:
        model = sentence_transformers.SentenceTransformer(
            settings.gemma_model_id,
            revision=settings.gemma_revision,
            device=device,
            config_kwargs=dict(TEXT_ONLY_CONFIG),
            model_kwargs={"dtype": dtype},
        )
    except (OSError, RuntimeError, ValueError) as exc:
        msg = (
            f"could not load {settings.gemma_model_id} at revision {settings.gemma_revision} "
            f"on {device}: {type(exc).__name__}: {exc}"
        )
        raise EmbeddingProviderUnavailable(msg) from exc
    model.max_seq_length = MAX_TOKENS
    return model


def _finite_rows(encoded: Any, expected: int, dim: int) -> list[list[float] | None]:
    """The encoder's rows as float lists, a non-finite or mis-sized row as ``None``."""
    rows = encoded.tolist() if hasattr(encoded, "tolist") else list(encoded)
    if len(rows) != expected:
        msg = f"the local model returned {len(rows)} vectors for {expected} texts"
        raise EmbeddingResponseInvalid(msg)
    out: list[list[float] | None] = []
    for row in rows:
        vector = [float(x) for x in row]
        out.append(vector if len(vector) == dim and all(map(math.isfinite, vector)) else None)
    return out


class EmbeddingGemmaLocalEmbedder:
    """``EmbeddingProvider`` over EmbeddingGemma 2, run on this machine.

    The model loads once, lazily, on the first call that needs it, so
    constructing the embedder costs nothing and a run with nothing pending
    never touches torch. ``model_factory`` replaces the loader (tests pass a
    fake; production uses :func:`load_sentence_transformer`).
    """

    provider = "gemma-local"

    def __init__(
        self,
        settings: EmbedSettings,
        *,
        model_factory: Callable[[EmbedSettings], Any] | None = None,
    ) -> None:
        self._settings = settings
        self._factory = model_factory or load_sentence_transformer
        self._model: Any = None
        self._lock = threading.Lock()

    @property
    def model_id(self) -> str:
        """The Hugging Face repository this embedder loads, stamped on every row."""
        return self._settings.gemma_model_id

    @property
    def dimension(self) -> int:
        """The Matryoshka width every vector is truncated to (768, 512, 256 or 128)."""
        return self._settings.embedding_dim

    def _loaded(self) -> Any:
        """The model, loaded on first use and shared by every later call."""
        with self._lock:
            if self._model is None:
                self._model = self._factory(self._settings)
            return self._model

    def _encode(self, model: Any, texts: list[str], *, prompt_name: str) -> Any:
        """One forward pass: prompt, truncate to the width, re-normalize."""
        return model.encode(
            texts,
            prompt_name=prompt_name,
            batch_size=len(texts),
            truncate_dim=self.dimension,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )

    async def embed_documents(self, texts: list[str]) -> list[list[float] | None]:
        """Embed corpus documents one batch at a time; one slot per input text, in order.

        Texts are batched shortest first under :func:`plan_batches`, so each
        forward pass pads to a similar length and a long text never pads a
        whole batch to its length, then put back in input order. A batch that
        raises is
        logged and left as ``None`` slots, and so is any single non-finite
        vector; the other batches are kept, and the next run's staleness
        anti-join re-picks the rows that are missing.
        """
        if not texts:
            return []
        model = await asyncio.to_thread(self._loaded)

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

        batches = plan_batches([len(t) for t in clipped], max_texts=self._settings.gemma_batch_size)
        vectors: list[list[float] | None] = [None] * len(texts)
        failed_batches = 0
        n_batches = len(batches)
        t0 = time.monotonic()
        for index, positions in enumerate(batches):
            batch = [clipped[i] for i in positions]
            try:
                encoded = await asyncio.to_thread(
                    self._encode, model, batch, prompt_name=DOCUMENT_PROMPT
                )
                rows = _finite_rows(encoded, len(batch), self.dimension)
            except (*_BATCH_ERRORS, EmbeddingResponseInvalid) as exc:
                failed_batches += 1
                logger.error(
                    "Local batch {} ({} texts) failed ({}: {}); keeping the batches that "
                    "succeeded — the next run re-picks these rows",
                    index,
                    len(batch),
                    type(exc).__name__,
                    exc,
                )
                continue
            for position, row in zip(positions, rows, strict=True):
                vectors[position] = row
        elapsed = time.monotonic() - t0

        embedded = sum(1 for v in vectors if v is not None)
        dropped = len(texts) - embedded
        if dropped and not failed_batches:
            logger.error("Dropped {} non-finite vectors; those rows stay unembedded", dropped)
        logger.info(
            "Embedded {} vectors locally across {}/{} batches in {:.2f}s ({:.1f} vec/s)",
            embedded,
            n_batches - failed_batches,
            n_batches,
            elapsed,
            embedded / elapsed if elapsed > 0 else 0.0,
        )
        return vectors

    def embed_query(self, text: str) -> list[float]:
        """Embed one query under the ``SearchQuery`` prompt, at :attr:`dimension`.

        Raises
        ------
        EmbeddingProviderUnavailable
            The forward pass failed.
        EmbeddingResponseInvalid
            The vector came back non-finite or at the wrong width.
        """
        sent, was_truncated = clip_text(text)
        if was_truncated:
            logger.warning(
                "Clipping query for embedding: {} chars -> {}", len(text), MAX_EMBEDDABLE_CHARS
            )
        model = self._loaded()
        try:
            encoded = self._encode(model, [sent], prompt_name=QUERY_PROMPT)
        except _BATCH_ERRORS as exc:
            msg = f"the local model failed on the query: {type(exc).__name__}: {exc}"
            raise EmbeddingProviderUnavailable(msg) from exc
        vector = _finite_rows(encoded, 1, self.dimension)[0]
        if vector is None:
            msg = (
                f"the local model returned a non-finite or {self.dimension}-mismatched query vector"
            )
            raise EmbeddingResponseInvalid(msg)
        return vector


__all__ = [
    "BATCH_CHAR_BUDGET",
    "DOCUMENT_PROMPT",
    "INSTALL_HINT",
    "MAX_TOKENS",
    "QUERY_PROMPT",
    "TEXT_ONLY_CONFIG",
    "EmbeddingGemmaLocalEmbedder",
    "load_sentence_transformer",
    "pick_device",
    "pick_dtype",
    "plan_batches",
]
