# SPDX-License-Identifier: Apache-2.0

"""Inline base64 attachments -> content-addressed blobs plus a short text placeholder.

Claude Code writes every image a tool returned (a ``Read`` of a PNG, a
screenshot) and every image a user pasted into the transcript as base64, and
the conversion used to carry that text all the way into ``trajectory.json``
and the ``tool_results.content`` column, where it was about half of all tool
result text. Anything reading that text (embeddings, an LLM prompt, a regex)
paid for megabytes of noise per image.

This module lifts the bytes out BEFORE conversion. Each attachment is decoded
once, named by the SHA-256 of its bytes, handed to the caller as a
:class:`Blob` to store, and replaced in the raw record by a short, stable
placeholder::

    [image sha256:<64 hex> image/png 123456 bytes]

so the converter, the enrichment pass and every text consumer see the
placeholder and never the base64. The rewrite runs on the parsed records the
converter and the audit share (one read, see
:mod:`atif_converter.infrastructure.raw_records`), so every artifact agrees
about it.

WHICH SHAPES. Measured over the local transcripts on 2026-09-27, three Claude
Code shapes carry base64 and each is handled:

* a ``tool_result`` block's content item
  ``{"type": "image"|"document", "source": {"type": "base64", ...}}``
  (``Read`` results, 10,798 of them);
* the same block shape directly in a user message's content (pasted images
  and PDFs);
* ``toolUseResult.file.base64`` on the ``Read`` record, the harness's own copy
  of the same file, which the converter used to dump into the result text a
  second time as ``[metadata]``.

In a Codex rollout the shape is ``{"type": "input_image", "image_url":
"data:<media>;base64,<data>"}`` inside a ``response_item`` message's content
or a tool output list. A tool output list can also carry an MCP image block,
``{"type": "image", "data": "<base64>", "mimeType": "image/png"}``. The
converter's harbor port names every inline image in a tool output it can save
by a placeholder (:func:`codex_tool_image_ref` is its rule: a supported image
type, a payload that decodes once whitespace is removed, data URL parameters
allowed), so a tool output's MCP image, and an ``input_image`` the strict data
URL match above misses, is lifted by that same rule and the placeholder names
bytes the collector holds. A base64 value anywhere else (a tool CALL's own
arguments, an ``image`` block in message content) is left alone.

Replacement is by block: an attachment block becomes a TEXT block holding the
placeholder (``input_text`` for Codex), which is the one block type every
downstream text rule already renders verbatim. A value that does not decode as
strict base64 is left exactly as it was: a malformed attachment is kept, never
silently dropped.

Pure: no I/O. The one library call is :mod:`imagesize`, which reads the width
and height out of the image header in memory.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import imagesize  # type: ignore[import-untyped]
from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

#: File extension per media type; anything else is stored as ``.bin``.
_EXTENSIONS: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "application/pdf": "pdf",
}

#: A media type is kept only when it looks like one; the placeholder and the
#: typed columns must not carry arbitrary transcript text.
_MEDIA_TYPE_RE = re.compile(r"^[a-z]+/[a-z0-9][a-z0-9.+-]{0,126}$")

#: The fallback media type for an attachment whose declared type is missing or malformed.
_OCTET_STREAM = "application/octet-stream"

#: A Codex data URL: ``data:<media>;base64,<payload>``.
_DATA_URL_RE = re.compile(r"^data:([^;,]{1,128});base64,", re.ASCII)

#: The image types harbor 0.24.0 saves from a Codex tool output
#: (``Codex._image_media_type``); any other type is ``[image omitted]`` there.
CODEX_TOOL_IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

#: harbor's data URL for a Codex tool output image: parameters allowed before ``;base64``.
_TOOL_IMAGE_DATA_URL = re.compile(
    r"data:(?P<media_type>[^;,]+)(?:;[^;,]*)*;base64,(?P<data>.*)", re.DOTALL
)


@dataclass(frozen=True, slots=True)
class BlobRef:
    """What a trajectory keeps of one attachment once its bytes are stored."""

    #: Lowercase hex SHA-256 of the decoded bytes: the blob's name.
    sha256: str
    #: The declared media type (``image/png``), or ``application/octet-stream``.
    media_type: str
    #: Decoded length in bytes.
    byte_count: int
    #: Pixel width from the image header, when the format is one it can read.
    width: int | None = None
    #: Pixel height, same rule.
    height: int | None = None

    @property
    def extension(self) -> str:
        """The stored file's extension (``png``, ``jpg``, ..., ``bin``)."""
        return _EXTENSIONS.get(self.media_type, "bin")

    @property
    def is_image(self) -> bool:
        """True for an ``image/*`` media type."""
        return self.media_type.startswith("image/")

    def placeholder(self) -> str:
        """The stable text left where the base64 was."""
        kind = "image" if self.is_image else "file"
        return f"[{kind} sha256:{self.sha256} {self.media_type} {self.byte_count} bytes]"

    def to_json(self) -> dict[str, Any]:
        """The ``extra.images[]`` entry the enrichment pass writes into the trajectory."""
        return {
            "sha256": self.sha256,
            "media_type": self.media_type,
            "bytes": self.byte_count,
            "width": self.width,
            "height": self.height,
            "extension": self.extension,
        }


@dataclass(frozen=True, slots=True)
class Blob:
    """One attachment's bytes, to be written to the corpus blob store under ``ref``."""

    ref: BlobRef
    data: bytes


def _media_type(value: Any) -> str:
    lowered = str(value).strip().lower() if isinstance(value, str) else ""
    return lowered if _MEDIA_TYPE_RE.match(lowered) else _OCTET_STREAM


def _dimensions(data: bytes) -> tuple[int | None, int | None]:
    try:
        width, height = imagesize.get(io.BytesIO(data))
    except (ValueError, OSError, IndexError) as exc:  # a truncated or odd header
        logger.debug("blobs: no dimensions from image header: {}", exc)
        return None, None
    # imagesize answers (-1, -1) for a format it cannot read.
    if width < 0 or height < 0:
        return None, None
    return width, height


@dataclass(slots=True)
class BlobCollector:
    """Accumulates one session's decoded attachments, deduplicated by hash."""

    _blobs: dict[str, Blob] = field(default_factory=dict)
    #: How many attachments were replaced (a repeated image counts every time).
    references: int = 0

    def add_base64(self, text: str, media_type: Any) -> BlobRef | None:
        """Decode ``text`` and register it; ``None`` when it is not strict base64.

        Strict means :func:`base64.b64decode` with ``validate=True``: a value
        carrying anything outside the base64 alphabet is not an attachment
        this module understands, and is left in place by the caller.
        """
        try:
            data = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            return None
        if not data:
            return None
        digest = hashlib.sha256(data).hexdigest()
        existing = self._blobs.get(digest)
        declared = _media_type(media_type)
        if existing is not None and existing.ref.media_type == declared:
            self.references += 1
            return existing.ref
        width, height = _dimensions(data) if declared.startswith("image/") else (None, None)
        ref = BlobRef(
            sha256=digest,
            media_type=declared,
            byte_count=len(data),
            width=width,
            height=height,
        )
        # The same bytes declared under two media types keep the first one's
        # blob; both refs name the same content, which is what the hash promises.
        self._blobs.setdefault(digest, Blob(ref=ref, data=data))
        self.references += 1
        return ref

    @property
    def blobs(self) -> tuple[Blob, ...]:
        """Every distinct blob, ordered by hash so the output is deterministic."""
        return tuple(self._blobs[key] for key in sorted(self._blobs))


@dataclass(slots=True)
class BlobIndex:
    """Where each replaced attachment sat, so enrichment can type it per row."""

    #: Tool call id -> the attachments in that call's result, in order, deduplicated.
    by_tool_call_id: dict[str, list[BlobRef]] = field(default_factory=dict)
    #: Record key (Claude Code ``uuid``; Codex edge uuid) -> attachments in
    #: that record's own message content (not inside a tool result).
    by_record_key: dict[str, list[BlobRef]] = field(default_factory=dict)

    def add_tool_result(self, call_id: Any, refs: Sequence[BlobRef]) -> None:
        """Attach ``refs`` to one tool call's result."""
        if not isinstance(call_id, str) or not call_id or not refs:
            return
        _extend_unique(self.by_tool_call_id.setdefault(call_id, []), refs)

    def add_record(self, key: Any, refs: Sequence[BlobRef]) -> None:
        """Attach ``refs`` to one record's message content."""
        if not isinstance(key, str) or not key or not refs:
            return
        _extend_unique(self.by_record_key.setdefault(key, []), refs)


def _extend_unique(target: list[BlobRef], refs: Iterable[BlobRef]) -> None:
    seen = {ref.sha256 for ref in target}
    for ref in refs:
        if ref.sha256 not in seen:
            target.append(ref)
            seen.add(ref.sha256)


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------


def _anthropic_block_ref(block: Any, collector: BlobCollector) -> BlobRef | None:
    """The ref for an ``image``/``document`` block with a base64 source, else ``None``."""
    if not isinstance(block, dict) or block.get("type") not in {"image", "document"}:
        return None
    source = block.get("source")
    if not isinstance(source, dict) or source.get("type") != "base64":
        return None
    data = source.get("data")
    if not isinstance(data, str):
        return None
    return collector.add_base64(data, source.get("media_type"))


def _rewrite_anthropic_blocks(blocks: list[Any], collector: BlobCollector) -> list[BlobRef]:
    """Replace every base64 block in ``blocks`` in place; return the refs in order."""
    refs: list[BlobRef] = []
    for index, block in enumerate(blocks):
        ref = _anthropic_block_ref(block, collector)
        if ref is None:
            continue
        blocks[index] = {"type": "text", "text": ref.placeholder()}
        refs.append(ref)
    return refs


def _rewrite_tool_use_result(tool_use_result: Any, collector: BlobCollector) -> list[BlobRef]:
    """Replace ``toolUseResult.file.base64`` (the ``Read`` shape) with a ``blob`` placeholder."""
    if not isinstance(tool_use_result, dict):
        return []
    file_value = tool_use_result.get("file")
    if not isinstance(file_value, dict):
        return []
    data = file_value.get("base64")
    if not isinstance(data, str):
        return []
    media_type = file_value.get("type")
    if not isinstance(media_type, str) and tool_use_result.get("type") == "pdf":
        media_type = "application/pdf"
    ref = collector.add_base64(data, media_type)
    if ref is None:
        return []
    del file_value["base64"]
    file_value["blob"] = ref.placeholder()
    return [ref]


def extract_claude_code_blobs(records: Iterable[Any], collector: BlobCollector) -> BlobIndex:
    """Rewrite every inline attachment in ``records`` IN PLACE; index where each one was.

    ``records`` are the session's parsed records (every file, main and side),
    the same objects the converter and the audit will read next.
    """
    index = BlobIndex()
    for record in records:
        if not isinstance(record, dict):
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        result_ids: list[str] = []
        if isinstance(content, list):
            own_refs = _rewrite_anthropic_blocks(content, collector)
            index.add_record(record.get("uuid"), own_refs)
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                call_id = block.get("tool_use_id")
                if isinstance(call_id, str) and call_id:
                    result_ids.append(call_id)
                inner = block.get("content")
                if isinstance(inner, list):
                    index.add_tool_result(call_id, _rewrite_anthropic_blocks(inner, collector))
        file_refs = _rewrite_tool_use_result(record.get("toolUseResult"), collector)
        # ``toolUseResult`` describes the record's one tool result. A record
        # carrying several results cannot say which one it belongs to, so its
        # attachment is still replaced but attributed to none of them.
        if file_refs and len(result_ids) == 1:
            index.add_tool_result(result_ids[0], file_refs)
    return index


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


def codex_tool_image_media_type(value: str) -> str | None:
    """The supported image MIME type for ``value``, as harbor's ``Codex._image_media_type`` maps it."""
    media_type = value.strip().lower()
    if media_type == "image/jpg":
        media_type = "image/jpeg"
    return media_type if media_type in CODEX_TOOL_IMAGE_MEDIA_TYPES else None


def codex_tool_image_ref(data: str, media_type: Any, collector: BlobCollector) -> BlobRef | None:
    """Register one inline Codex tool output image the way harbor 0.24.0 would save it.

    ``data`` is base64 or a ``data:`` URL (whose media type then wins over
    ``media_type``); harbor saves it only when the media type is one of
    :data:`CODEX_TOOL_IMAGE_MEDIA_TYPES` and the payload, with whitespace
    removed, decodes. ``None`` means harbor would write ``[image omitted]``:
    nothing is registered. The converter's port and the pre-pass both call
    this, so the placeholder text and the stored blob agree byte for byte.
    """
    data_url = _TOOL_IMAGE_DATA_URL.fullmatch(data)
    base64_text = data
    if data_url is not None:
        media_type, base64_text = data_url.group("media_type"), str(data_url.group("data"))
    if not isinstance(media_type, str):
        return None
    valid_media_type = codex_tool_image_media_type(media_type)
    if valid_media_type is None:
        return None
    return collector.add_base64("".join(base64_text.split()), valid_media_type)


def _mcp_image_ref(item: dict[str, Any], collector: BlobCollector) -> BlobRef | None:
    """An MCP ``image`` block's ref, read the way the converter's port reads it."""
    data = item.get("data")
    if not isinstance(data, str) or ("://" in data and not data.startswith("data:")):
        return None
    return codex_tool_image_ref(data, item.get("mimeType") or item.get("mime_type"), collector)


def _tool_input_image_ref(item: dict[str, Any], collector: BlobCollector) -> BlobRef | None:
    """A tool output ``input_image``'s ref by the port's rule (see :func:`codex_tool_image_ref`)."""
    url = item.get("image_url")
    if not isinstance(url, str) or ("://" in url and not url.startswith("data:")):
        return None
    return codex_tool_image_ref(url, None, collector)


def _input_image_ref(item: dict[str, Any], collector: BlobCollector) -> BlobRef | None:
    """An ``input_image`` item's ref when its ``image_url`` is a base64 data URL."""
    url = item.get("image_url")
    if not isinstance(url, str):
        return None
    match = _DATA_URL_RE.match(url)
    if match is None:
        return None
    return collector.add_base64(url[match.end() :], match.group(1))


def _codex_image_ref(item: Any, collector: BlobCollector, *, tool_output: bool) -> BlobRef | None:
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if item_type == "input_image":
        ref = _input_image_ref(item, collector)
        if ref is None and tool_output:
            # The port's rule is wider (data URL parameters, whitespace in
            # the payload); whatever it would name, the collector must hold.
            ref = _tool_input_image_ref(item, collector)
        return ref
    if item_type == "image" and tool_output:
        return _mcp_image_ref(item, collector)
    return None


def _rewrite_codex_items(
    items: list[Any], collector: BlobCollector, *, tool_output: bool = False
) -> list[BlobRef]:
    refs: list[BlobRef] = []
    for position, item in enumerate(items):
        ref = _codex_image_ref(item, collector, tool_output=tool_output)
        if ref is None:
            continue
        items[position] = {"type": "input_text", "text": ref.placeholder()}
        refs.append(ref)
    return refs


def extract_codex_blobs(
    records: Sequence[Any],
    collector: BlobCollector,
    record_keys: Sequence[str] | None = None,
) -> BlobIndex:
    """Rewrite every inline image in a rollout's response items.

    An ``input_image`` data URL is lifted from message content and tool
    outputs; an MCP ``image`` block with inline base64 only from a tool output
    list, by :func:`codex_tool_image_ref`'s rule.

    ``record_keys`` (parallel to ``records``) names each record the way the
    enrichment pass names it in ``source_uuids``; without it, attachments in
    message content are replaced but indexed nowhere. A tool output's
    attachments are indexed by the output's ``call_id``.
    """
    index = BlobIndex()
    for position, record in enumerate(records):
        if not isinstance(record, dict) or record.get("type") != "response_item":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        payload_type = payload.get("type")
        if payload_type == "message" and isinstance(payload.get("content"), list):
            refs = _rewrite_codex_items(payload["content"], collector)
            if record_keys is not None and refs:
                index.add_record(record_keys[position], refs)
        elif payload_type in {"function_call_output", "custom_tool_call_output"}:
            output = payload.get("output")
            if isinstance(output, list):
                index.add_tool_result(
                    payload.get("call_id"),
                    _rewrite_codex_items(output, collector, tool_output=True),
                )
    return index


__all__ = [
    "CODEX_TOOL_IMAGE_MEDIA_TYPES",
    "Blob",
    "BlobCollector",
    "BlobIndex",
    "BlobRef",
    "codex_tool_image_media_type",
    "codex_tool_image_ref",
    "extract_claude_code_blobs",
    "extract_codex_blobs",
]
