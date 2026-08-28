# SPDX-License-Identifier: Apache-2.0

"""Pydantic → OpenAI strict-mode JSON Schema transform (CONTRACT-V2 §Client contract).

OpenAI's strict structured outputs (``response_format.json_schema.strict``)
accept a JSON Schema subset with two hard rules this module enforces:

* ``additionalProperties: false`` on EVERY object level.
* EVERY property listed in ``required`` — optionality is expressed by
  making the field's type nullable (``["string", "null"]`` / an ``anyOf``
  null branch), never by omitting it from ``required``.

``$defs``/``$ref`` are KEPT: OpenAI strict accepts them, including a
``$defs``-referenced enum, on all three gpt-5.6 sizes. Ref inlining is a
per-provider wire-format concern, so it belongs in whichever adapter needs
it rather than here.

``default`` is dropped (it contradicts required-everywhere) and ``title``
is dropped as noise. Pydantic ``model_validate`` on the response is the
second gate, so ge/le style constraints still apply even if the model
ignores them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pydantic import BaseModel

#: Keys whose value is a mapping of NAME -> subschema (names are user data,
#: never schema keywords — a field literally named "default" must survive).
_MAP_OF_SCHEMAS = ("$defs", "definitions", "properties", "patternProperties")
#: Keys whose value is a list of subschemas.
_LIST_OF_SCHEMAS = ("anyOf", "oneOf", "allOf", "prefixItems")
#: Keys whose value is a single subschema.
_SINGLE_SCHEMA = ("items", "contains", "propertyNames", "not")


def to_openai_strict(schema: type[BaseModel]) -> dict[str, Any]:
    """Return ``schema.model_json_schema()`` transformed to OpenAI strict mode.

    Raises ``ValueError`` for shapes strict mode cannot represent (an
    object with a schema-valued ``additionalProperties``, i.e. a
    ``dict[str, X]`` field) — better a loud failure at body-build time
    than a Bedrock 400 mid-pipeline.
    """
    return _strictify(schema.model_json_schema())


def _strictify(node: dict[str, Any]) -> dict[str, Any]:
    """Recursively apply the strict-mode rules to one schema node."""
    out: dict[str, Any] = {}
    for key, val in node.items():
        if key in ("title", "default"):
            continue
        if key in _MAP_OF_SCHEMAS and isinstance(val, dict):
            out[key] = {name: _strictify(sub) for name, sub in val.items()}
        elif key in _LIST_OF_SCHEMAS and isinstance(val, list):
            out[key] = [_strictify(sub) if isinstance(sub, dict) else sub for sub in val]
        elif key in _SINGLE_SCHEMA and isinstance(val, dict):
            out[key] = _strictify(val)
        elif key == "additionalProperties" and isinstance(val, dict):
            msg = (
                "OpenAI strict mode cannot represent open mappings "
                "(schema-valued additionalProperties, e.g. a dict[str, X] field); "
                "model the keys explicitly or use a list of key/value pairs"
            )
            raise ValueError(msg)
        else:
            out[key] = val
    if out.get("type") == "object" or "properties" in out:
        props: dict[str, Any] = out.get("properties") or {}
        already_required = set(out.get("required") or [])
        for name, sub in props.items():
            if name not in already_required:
                props[name] = _nullable(sub)
        if props:
            out["required"] = list(props)
        out["additionalProperties"] = False
    return out


def _nullable(sub: dict[str, Any]) -> dict[str, Any]:
    """Rewrite one property subschema so ``null`` is an accepted value."""
    if "$ref" in sub:
        rest = {k: v for k, v in sub.items() if k != "$ref"}
        return {**rest, "anyOf": [{"$ref": sub["$ref"]}, {"type": "null"}]}
    if "anyOf" in sub:
        branches: list[Any] = sub["anyOf"]
        if any(isinstance(b, dict) and b.get("type") == "null" for b in branches):
            return sub
        return {**sub, "anyOf": [*branches, {"type": "null"}]}
    type_val = sub.get("type")
    if isinstance(type_val, str):
        new = {**sub, "type": [type_val, "null"]}
    elif isinstance(type_val, list):
        new = sub if "null" in type_val else {**sub, "type": [*type_val, "null"]}
    else:
        # No type and no ref/anyOf (e.g. a bare enum or empty schema):
        # wrap rather than guess.
        return {"anyOf": [sub, {"type": "null"}]}
    enum_val = new.get("enum")
    if isinstance(enum_val, list) and None not in enum_val:
        new = {**new, "enum": [*enum_val, None]}
    return new


__all__ = ["to_openai_strict"]
