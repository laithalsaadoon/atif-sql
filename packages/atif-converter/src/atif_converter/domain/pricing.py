# SPDX-License-Identifier: Apache-2.0

"""Per-call USD pricing that reproduces ``litellm.cost_per_token`` without importing litellm.

WHY. ``import litellm`` costs about four seconds and pulls in openai, anthropic
and hundreds of pydantic model builds, all for one ``cost_per_token`` call per
step. Every process that converts a transcript paid that once. This module
prices the shapes our transcripts produce by reading litellm's OWN bundled
price table (``model_prices_and_context_window_backup.json``) straight off
disk, located through ``importlib.util.find_spec`` so ``litellm/__init__`` is
never executed, and by repeating litellm 1.100.1's arithmetic in the same
float operation order. The output is bit for bit what litellm returns, which
``tests/test_pricing_identity.py`` proves against litellm itself over every
bare key of the table and over the model names found in the frozen corpora.

WHAT IS COVERED. A model name with no ``/`` that is either

- an exact key of the table whose ``litellm_provider`` is ``anthropic``,
  ``openai``, ``bedrock`` or ``bedrock_converse`` (Claude Code models, Codex
  ``gpt-*`` models, Bedrock-hosted ids), or
- absent from the table but routed to Anthropic by the table's own
  ``fallback_generalizations`` rules (a bare ``claude-<family>-<n>`` id litellm
  does not know yet). litellm prices those from the capability rules' union,
  which carries no rates, so the result is ``(0.0, 0.0)``; that is what the
  frozen trajectories hold and it is kept.

Everything else (a ``provider/model`` string, a fine-tune ``ft:`` id, a
``tiered_pricing`` table, a provider with its own cost calculator, a model
whose case differs from its key) raises :class:`FastPathUnsupported` inside
:func:`fast_cost_per_token`, and :func:`cost_per_token` then imports litellm
and calls it exactly as before, logging the fallback at debug. Correctness
therefore never regresses; only the covered shapes get faster.

THE ARITHMETIC, as litellm 1.100.1 performs it for these providers
(``cost_calculator.cost_per_token`` -> ``generic_cost_per_token``)::

    text = max(prompt_tokens - cache_read - cache_creation, 0)
    prompt_cost = float(text) * input_rate
    prompt_cost += float(cache_read) * cache_read_rate
    if cache_creation:
        writing = 0.0
        writing += cache_creation * cache_creation_rate
        prompt_cost += writing
    completion_cost = float(completion_tokens) * output_rate

where the four rates come from ``_get_token_base_cost``: the service-tier
variant of each key when the tier is ``flex`` / ``priority`` / ``fast`` /
``ultrafast`` (``fast`` reads the ``_priority`` key; ``standard`` and any
other tier read the base key), then the ``*_above_<n>k_tokens`` variants when
``prompt_tokens`` (input tokens alone, cache excluded) exceeds the largest
threshold the entry prices. A missing rate is ``0.0``, except that a missing
service-tier key falls back to its base key.

LITELLM_LOCAL_MODEL_COST_MAP. litellm reads the bundled table only when that
variable is ``true``; otherwise it fetches the table from GitHub at import,
which is slow and non-deterministic. This module always prices from the
bundled table, so it is active when the variable is unset or ``true``. An
operator who sets it to anything else has asked for the remote table, and
every call then falls back to litellm. When the fallback imports litellm with
the variable unset, it is set to ``true`` first so one process never prices
from two tables.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any, Final, NoReturn, cast

from loguru import logger

#: litellm's switch between the bundled table and the remote one.
LOCAL_TABLE_ENV: Final = "LITELLM_LOCAL_MODEL_COST_MAP"

#: The price table litellm ships inside its own package directory.
TABLE_FILENAME: Final = "model_prices_and_context_window_backup.json"

#: Providers whose ``cost_per_token`` branch is plain ``generic_cost_per_token``.
_COVERED_PROVIDERS: Final = frozenset({"anthropic", "openai", "bedrock"})

#: ``litellm.constants.REPLICATE_MODEL_NAME_WITH_ID_LENGTH`` (default 64): a
#: model with a ``:`` and a longer name than this is checked as a Replicate id
#: before the Bedrock membership test is reached.
_REPLICATE_ID_LENGTH: Final = 64

#: ``_SERVICE_TIER_SUFFIXES``: ``ServiceTier`` values as key suffixes, longest
#: first (a stable sort keeps the enum order among equal lengths).
_SERVICE_TIER_SUFFIXES: Final = ("_ultrafast", "_priority", "_auto", "_flex", "_fast")

#: ``_SERVICE_TIER_TO_COST_KEY_SUFFIX``.
_SERVICE_TIER_TO_COST_KEY_SUFFIX: Final = {
    "flex": "flex",
    "priority": "priority",
    "fast": "priority",
    "ultrafast": "ultrafast",
}

#: ``_ABOVE_THRESHOLD_COST_KEY``: an entry key with this tail is copied into
#: litellm's ``ModelInfoBase`` even when it is not one of the named fields.
_ABOVE_THRESHOLD_KEY: Final = re.compile(r"_above_\d+k?_tokens$")

#: The ``ModelInfoBase`` fields ``_get_token_base_cost`` and
#: ``_calculate_input_cost`` can read on our path. A key outside this set and
#: not matching :data:`_ABOVE_THRESHOLD_KEY` reads as ``None`` in litellm even
#: when the raw entry carries it, so the same must hold here.
_MODEL_INFO_FIELDS: Final = frozenset(
    {
        "input_cost_per_token",
        "input_cost_per_token_flex",
        "input_cost_per_token_priority",
        "input_cost_per_token_ultrafast",
        "input_cost_per_token_above_200k_tokens_priority",
        "input_cost_per_token_above_272k_tokens_priority",
        "input_cost_per_token_above_272k_tokens_flex",
        "output_cost_per_token",
        "output_cost_per_token_flex",
        "output_cost_per_token_priority",
        "output_cost_per_token_ultrafast",
        "output_cost_per_token_above_200k_tokens_priority",
        "output_cost_per_token_above_272k_tokens_priority",
        "output_cost_per_token_above_272k_tokens_flex",
        "output_cost_per_image_token",
        "cache_creation_input_token_cost",
        "cache_creation_input_token_cost_flex",
        "cache_creation_input_token_cost_priority",
        "cache_creation_input_token_cost_ultrafast",
        "cache_creation_input_token_cost_above_1hr",
        "cache_creation_input_token_cost_above_272k_tokens_priority",
        "cache_creation_input_token_cost_above_272k_tokens_flex",
        "cache_read_input_token_cost",
        "cache_read_input_token_cost_flex",
        "cache_read_input_token_cost_priority",
        "cache_read_input_token_cost_ultrafast",
        "cache_read_input_token_cost_above_200k_tokens_priority",
        "cache_read_input_token_cost_above_272k_tokens_priority",
        "cache_read_input_token_cost_above_272k_tokens_flex",
    }
)

#: ``ModelInfoBase`` names the ``input_cost_per_token_above_*`` fields below
#: whether or not the entry prices them; they take part in the threshold scan
#: (with ``None`` values, which the scan skips) exactly like a raw key would.
_NAMED_THRESHOLD_KEYS: Final = (
    "input_cost_per_token_above_128k_tokens",
    "input_cost_per_token_above_200k_tokens",
    "input_cost_per_token_above_272k_tokens",
    "input_cost_per_token_above_512k_tokens",
)

_ANTHROPIC_TEXT_MODELS: Final = frozenset({"claude-2", "claude-instant-1"})
_OPENAI_HARDCODED: Final = frozenset({"dall-e-2", "dall-e-3", "sora-2"})

#: ``litellm.azure_llms`` remaps these bare names to ``azure/...`` keys before lookup.
_AZURE_REMAPPED: Final = frozenset({"gpt-35-turbo", "gpt-35-turbo-16k", "gpt-35-turbo-instruct"})


class FastPathUnsupported(Exception):  # noqa: N818, a routing signal rather than an error
    """The fast path does not cover this call; price it through litellm instead."""


def _decline(reason: str) -> NoReturn:
    raise FastPathUnsupported(reason)


class UnpriceableModelError(ValueError):
    """litellm itself would raise for this model, so the caller records no cost."""


@dataclass(frozen=True, slots=True)
class _CapabilityRule:
    pattern: re.Pattern[str]
    model_info: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _RoutingRule:
    pattern: re.Pattern[str]
    provider: str


@dataclass(frozen=True, slots=True)
class PricingTable:
    """litellm's price table as ``litellm.model_cost`` holds it, plus its fallback rules."""

    path: Path
    entries: dict[str, dict[str, Any]]
    lowercase_keys: dict[str, str]
    routing_rules: tuple[_RoutingRule, ...]
    capability_rules: tuple[_CapabilityRule, ...]

    def lookup_key(self, name: str) -> str | None:
        """``litellm.utils._get_model_cost_key``: exact match, then case-insensitive."""
        if name in self.entries:
            return name
        return self.lowercase_keys.get(name.lower())

    def match_routing(self, model: str) -> str | None:
        """The provider of the first routing rule whose regex matches ``model``."""
        if not model:
            return None
        return next(
            (rule.provider for rule in self.routing_rules if rule.pattern.search(model)),
            None,
        )

    def match_capabilities(self, model: str) -> dict[str, Any] | None:
        """The union, in file order, of every capability rule matching ``model``."""
        if not model:
            return None
        matched = [rule.model_info for rule in self.capability_rules if rule.pattern.search(model)]
        if not matched:
            return None
        return {key: value for info in matched for key, value in info.items()}


@dataclass(frozen=True, slots=True)
class _Rates:
    prompt: float
    completion: float
    cache_creation: float
    cache_creation_above_1hr: float
    cache_read: float


# --------------------------------------------------------------------------- table loading


def locate_table() -> Path | None:
    """The bundled table's path, found WITHOUT importing litellm; ``None`` when not installed."""
    try:
        spec = importlib.util.find_spec("litellm")
    except (ImportError, ValueError):
        return None
    if spec is None or spec.origin is None:
        return None
    path = Path(spec.origin).parent / TABLE_FILENAME
    return path if path.is_file() else None


def _resolve_legacy_extends(rules: list[Any]) -> list[Any]:
    """``fallback_generalizations._resolve_legacy_extends``: single-level ``extends``."""
    base_by_name: dict[str, dict[str, Any]] = {
        rule["name"]: rule["model_info"]
        for rule in rules
        if isinstance(rule, dict)
        and isinstance(rule.get("name"), str)
        and isinstance(rule.get("model_info"), dict)
    }

    def resolved(rule: Any) -> Any:
        if not isinstance(rule, dict):
            return rule
        parent_name = rule.get("extends")
        own_info = rule.get("model_info")
        parent_info = base_by_name.get(parent_name) if isinstance(parent_name, str) else None
        if parent_info is None or not isinstance(own_info, dict):
            return rule
        return {**rule, "model_info": {**parent_info, **own_info}}

    return [resolved(rule) for rule in rules]


def _compile_rules(raw_rules: Any) -> tuple[tuple[_RoutingRule, ...], tuple[_CapabilityRule, ...]]:
    """``fallback_generalizations._compile_rule`` over the whole list; malformed rules are skipped."""
    routing: list[_RoutingRule] = []
    capability: list[_CapabilityRule] = []
    installed = raw_rules if isinstance(raw_rules, list) else []
    for rule in _resolve_legacy_extends(installed):
        if not isinstance(rule, dict):
            continue
        pattern = rule.get("pattern")
        model_info = rule.get("model_info")
        if not isinstance(pattern, str) or not isinstance(model_info, dict):
            continue
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error:
            continue
        if "litellm_provider" not in model_info:
            capability.append(_CapabilityRule(compiled, model_info))
            continue
        provider = model_info["litellm_provider"]
        if not isinstance(provider, str):
            continue
        routing.append(_RoutingRule(compiled, provider))
        if len(model_info) > 1:
            capability.append(_CapabilityRule(compiled, model_info))
    return tuple(routing), tuple(capability)


def _expand_model_aliases(entries: dict[str, dict[str, Any]]) -> None:
    """``get_model_cost_map._expand_model_aliases``: alias keys share the canonical dict."""
    aliases_to_add: dict[str, dict[str, Any]] = {}
    keys_with_aliases: list[str] = []
    for model_name, model_info in entries.items():
        aliases = model_info.get("aliases")
        if aliases is None:
            continue
        keys_with_aliases.append(model_name)
        if not isinstance(aliases, list) or not aliases:
            continue
        for alias in aliases:
            if alias in entries or alias in aliases_to_add:
                continue
            aliases_to_add[alias] = model_info
    for key in keys_with_aliases:
        entries[key].pop("aliases", None)
    entries.update(aliases_to_add)


@cache
def load_table() -> PricingTable | None:
    """Parse the bundled table once per process; ``None`` when litellm is not installed."""
    path = locate_table()
    if path is None:
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return None
    generalizations = raw.pop("fallback_generalizations", None)
    rules = generalizations.get("rules") if isinstance(generalizations, dict) else None
    routing, capability = _compile_rules(rules)
    entries: dict[str, dict[str, Any]] = {k: v for k, v in raw.items() if isinstance(v, dict)}
    _expand_model_aliases(entries)
    return PricingTable(
        path=path,
        entries=entries,
        lowercase_keys={k.lower(): k for k in entries},
        routing_rules=routing,
        capability_rules=capability,
    )


def fast_path_enabled() -> bool:
    """True unless the operator pointed litellm at the remote table."""
    raw = os.environ.get(LOCAL_TABLE_ENV)
    return raw is None or raw.lower() == "true"


def _active_table() -> PricingTable | None:
    if not fast_path_enabled():
        return None
    return load_table()


def _import_litellm() -> ModuleType:
    """The fallback's ``import litellm``, pinned to the bundled table unless told otherwise."""
    os.environ.setdefault(LOCAL_TABLE_ENV, "true")
    import litellm

    return litellm


# --------------------------------------------------------------------------- resolution


def _infer_provider(table: PricingTable, model: str, entry: dict[str, Any] | None) -> str:
    """``get_llm_provider`` for a bare model name, restricted to the covered providers.

    The order is litellm's: the OpenAI membership tests, then Anthropic, then
    the Replicate id-length test that shadows the Bedrock test, then Bedrock,
    then the table's routing rules as the last resort for an unmapped name.
    """
    if "ft:" in model or model in _AZURE_REMAPPED:
        _decline("fine-tune id or azure alias")
    if entry is None:
        return _infer_unmapped_provider(table, model)
    litellm_provider = entry.get("litellm_provider")
    if litellm_provider == "openai" or model in _OPENAI_HARDCODED or model.startswith("gpt-image"):
        if litellm_provider != "openai":
            _decline("hardcoded OpenAI name under another provider")
        return "openai"
    if litellm_provider == "anthropic":
        if model in _ANTHROPIC_TEXT_MODELS:
            _decline("anthropic_text model")
        return "anthropic"
    if litellm_provider not in {"bedrock", "bedrock_converse"}:
        _decline(f"provider {litellm_provider!r}")
    if ":" in model and len(model) > _REPLICATE_ID_LENGTH:
        # litellm takes the Replicate branch, sets nothing, and lands on the routing rules.
        if table.match_routing(model) != "bedrock":
            _decline("long Bedrock id not routed back to bedrock")
        return "bedrock"
    if litellm_provider == "bedrock" and entry.get("mode") == "guardrail":
        _decline("bedrock guardrail")
    return "bedrock"


def _infer_unmapped_provider(table: PricingTable, model: str) -> str:
    """The routing-rule tail of ``get_llm_provider`` for a name the table does not hold."""
    if (
        model in _OPENAI_HARDCODED
        or model.startswith(("gpt-image", "amazon_nova"))
        or model == "*"
        or (":" in model and len(model) > _REPLICATE_ID_LENGTH)
    ):
        _decline("unmapped name with a hardcoded provider rule")
    routed = table.match_routing(model)
    if routed is None:
        raise UnpriceableModelError(model)
    if routed != "anthropic":
        _decline(f"unmapped model routed to {routed!r}")
    return routed


def _provider_matches(entry: dict[str, Any], provider: str) -> bool:
    """``litellm.utils._check_provider_match`` for the covered providers."""
    litellm_provider = entry.get("litellm_provider")
    if litellm_provider is None or litellm_provider == provider:
        return True
    return provider.startswith("bedrock") and str(litellm_provider).startswith("bedrock")


def _resolve(table: PricingTable, model: str) -> dict[str, Any]:
    """``get_model_info``'s entry for ``model``: the raw dict litellm builds ``ModelInfoBase`` from."""
    entry = table.entries.get(model)
    provider = _infer_provider(table, model, entry)
    prefixed = table.lookup_key(f"{provider}/{model}")
    if prefixed is not None and _provider_matches(table.entries[prefixed], provider):
        return table.entries[prefixed]
    if entry is not None:
        return entry
    if table.lookup_key(model) is not None:
        _decline("key differs from the model name by case")
    # An unmapped bare Claude id: litellm builds the entry from the capability rules.
    candidates = (f"{provider}/{model}", model)
    if any(table.lookup_key(candidate) is not None for candidate in candidates):
        _decline("a stripped candidate is a table key")
    for candidate in candidates:
        generalized = table.match_capabilities(candidate)
        if generalized is not None:
            return {**generalized, "litellm_provider": provider}
    raise UnpriceableModelError(model)


# --------------------------------------------------------------------------- rates


def _info_get(entry: dict[str, Any], key: str) -> Any:
    """``ModelInfoBase.get(key)`` for the dict litellm would have built from ``entry``."""
    if key not in _MODEL_INFO_FIELDS and _ABOVE_THRESHOLD_KEY.search(key) is None:
        return None
    value = entry.get(key)
    if value is None and key in {"input_cost_per_token", "output_cost_per_token"}:
        return 0
    return value


def _coerce_rate(value: Any) -> float | None:
    if isinstance(value, float):
        return value
    if isinstance(value, int):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _cost_per_unit(
    entry: dict[str, Any], cost_key: str, default: float | None = 0.0
) -> float | None:
    """``_get_cost_per_unit``: the rate, else the base rate when a tier suffix is in the key."""
    value = _info_get(entry, cost_key)
    coerced = _coerce_rate(value)
    if coerced is not None:
        return coerced
    if value is None:
        for suffix in _SERVICE_TIER_SUFFIXES:
            if suffix in cost_key:
                fallback = _coerce_rate(_info_get(entry, cost_key.replace(suffix, "")))
                if fallback is not None:
                    return fallback
                break
    return default


def _rate(entry: dict[str, Any], cost_key: str, default: float = 0.0) -> float:
    """:func:`_cost_per_unit` with a float default, so the result is always a float."""
    value = _cost_per_unit(entry, cost_key, default)
    return default if value is None else value


def _tier_key(base_key: str, service_tier: str | None) -> str:
    """``_get_service_tier_cost_key``."""
    if service_tier is None:
        return base_key
    suffix = _SERVICE_TIER_TO_COST_KEY_SUFFIX.get(service_tier.lower())
    if suffix is None:
        return base_key
    return f"{base_key}_{suffix}"


def _parse_threshold(key: str) -> float:
    """``_parse_above_token_threshold``: ``..._above_200k_tokens`` -> 200000.0."""
    threshold_str = key.split("_above_")[1].split("_tokens", maxsplit=1)[0]
    return float(threshold_str.replace("k", "")) * (1000 if "k" in threshold_str else 1)


def _threshold_keys(entry: dict[str, Any]) -> list[str]:
    """The ``input_cost_per_token_above_*`` keys litellm's ``ModelInfoBase`` would carry."""
    keys: list[str] = list(_NAMED_THRESHOLD_KEYS)
    keys.extend(
        key
        for key in entry
        if key.startswith("input_cost_per_token_above_")
        and key not in keys
        and _ABOVE_THRESHOLD_KEY.search(key)
    )
    return [key for key in keys if not key.endswith(_SERVICE_TIER_SUFFIXES)]


def _tiered_rates(
    entry: dict[str, Any], tail: str, service_tier: str | None, rates: _Rates
) -> _Rates:
    """Each rate's ``*<tail>`` variant (tier-suffixed when the tier is known), else itself."""

    def tiered(base: str, current: float) -> float:
        return _rate(entry, _tier_key(f"{base}{tail}", service_tier), current)

    return _Rates(
        prompt=tiered("input_cost_per_token", rates.prompt),
        completion=tiered("output_cost_per_token", rates.completion),
        cache_creation=tiered("cache_creation_input_token_cost", rates.cache_creation),
        cache_creation_above_1hr=tiered(
            "cache_creation_input_token_cost_above_1hr", rates.cache_creation_above_1hr
        ),
        cache_read=tiered("cache_read_input_token_cost", rates.cache_read),
    )


def _above_threshold_rates(
    entry: dict[str, Any],
    prompt_tokens: int,
    service_tier: str | None,
    rates: _Rates,
) -> _Rates:
    """The ``## CHECK IF ABOVE THRESHOLD`` block of ``_get_token_base_cost``."""
    threshold_keys = _threshold_keys(entry)
    if not threshold_keys:
        return rates
    for key in sorted(threshold_keys, key=_parse_threshold, reverse=True):
        if _info_get(entry, key) is None:
            continue
        threshold_str = key.split("_above_")[1].split("_tokens")[0]
        if not prompt_tokens > _parse_threshold(key):
            continue
        return _tiered_rates(entry, f"_above_{threshold_str}_tokens", service_tier, rates)
    return rates


def _base_rates(entry: dict[str, Any], prompt_tokens: int, service_tier: str | None) -> _Rates:
    """``_get_token_base_cost`` for an entry without ``tiered_pricing``."""
    tiered_pricing = entry.get("tiered_pricing")
    if isinstance(tiered_pricing, list) and tiered_pricing:
        _decline("tiered_pricing table")
    prompt = _rate(entry, _tier_key("input_cost_per_token", service_tier))
    completion = _rate(entry, _tier_key("output_cost_per_token", service_tier))
    if completion == 0.0:
        image_rate = _cost_per_unit(entry, "output_cost_per_image_token", None)
        if image_rate is not None:
            completion = image_rate
    rates = _Rates(
        prompt=prompt,
        completion=completion,
        cache_creation=_rate(entry, _tier_key("cache_creation_input_token_cost", service_tier)),
        cache_creation_above_1hr=_rate(entry, "cache_creation_input_token_cost_above_1hr"),
        cache_read=_rate(entry, _tier_key("cache_read_input_token_cost", service_tier)),
    )
    return _above_threshold_rates(entry, prompt_tokens, service_tier, rates)


# --------------------------------------------------------------------------- public API


def fast_cost_per_token(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
    service_tier: str | None = None,
) -> tuple[float, float]:
    """``litellm.cost_per_token`` for the covered shapes, computed from the bundled table.

    Raises :class:`FastPathUnsupported` for a shape this module does not
    replicate and :class:`UnpriceableModelError` where litellm would raise.
    """
    table = _active_table()
    if table is None:
        _decline("bundled table unavailable or disabled")
    if not model or "/" in model:
        _decline("empty or provider-prefixed model")
    counts = (
        prompt_tokens,
        completion_tokens,
        cache_creation_input_tokens,
        cache_read_input_tokens,
    )
    if any(type(count) is not int for count in counts):
        _decline("non-int token counts")
    entry = _resolve(table, model)
    rates = _base_rates(entry, prompt_tokens, service_tier)

    # generic_cost_per_token: the cache counts arrive on prompt_tokens_details, text
    # tokens are unset, so both the double-counting branch and the clamp branch
    # leave text = max(prompt - cache_read - cache_creation, 0).
    text_tokens = max(prompt_tokens - cache_read_input_tokens - cache_creation_input_tokens, 0)
    prompt_cost = float(text_tokens) * rates.prompt
    prompt_cost += float(cache_read_input_tokens) * rates.cache_read
    if cache_creation_input_tokens:
        writing_cost = 0.0
        writing_cost += cache_creation_input_tokens * rates.cache_creation
        prompt_cost += writing_cost
    completion_cost = float(completion_tokens) * rates.completion
    return prompt_cost, completion_cost


def cost_per_token(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
    service_tier: str | None = None,
) -> tuple[float, float]:
    """``(prompt_cost_usd, completion_cost_usd)``: the fast path, else litellm itself.

    Raises whatever litellm raises (and ``ImportError`` when litellm is not
    installed) for a shape the fast path declines; the callers turn any
    exception into "no estimate", as harbor did.
    """
    try:
        return fast_cost_per_token(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            service_tier=service_tier,
        )
    except FastPathUnsupported as exc:
        logger.debug("Pricing fast path declined model '{}' ({}); using litellm", model, exc)
    litellm = _import_litellm()
    priced = litellm.cost_per_token(
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        service_tier=service_tier,
    )
    return cast("tuple[float, float]", priced)


def has_pricing_entry(key: str) -> bool:
    """``bool(litellm.model_cost.get(key))``, whether the table prices this exact key.

    Raises ``ImportError`` when neither the bundled table nor litellm is available.
    """
    table = _active_table()
    if table is None:
        litellm = _import_litellm()
        return bool(litellm.model_cost.get(key))
    return bool(table.entries.get(key))
