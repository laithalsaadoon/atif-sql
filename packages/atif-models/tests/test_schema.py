# SPDX-License-Identifier: Apache-2.0

"""Strict-schema transform: nullable+required optionals, recursion, enums."""

from __future__ import annotations

from enum import StrEnum

import pytest
from pydantic import BaseModel

from atif_models.domain.schema import to_openai_strict


class Color(StrEnum):
    RED = "red"
    BLUE = "blue"


class Inner(BaseModel):
    name: str
    color: Color


class WithOptional(BaseModel):
    required_field: str
    optional_str: str | None = None
    optional_int: int = 7


class Nested(BaseModel):
    inner: Inner
    inners: list[Inner]
    note: str | None = None


class TestRequiredAndNullable:
    def test_all_properties_required(self):
        out = to_openai_strict(WithOptional)
        assert set(out["required"]) == {"required_field", "optional_str", "optional_int"}

    def test_optional_union_field_keeps_null_branch(self):
        out = to_openai_strict(WithOptional)
        prop = out["properties"]["optional_str"]
        assert {"type": "null"} in prop["anyOf"]

    def test_defaulted_plain_field_becomes_nullable_type(self):
        out = to_openai_strict(WithOptional)
        prop = out["properties"]["optional_int"]
        assert prop["type"] == ["integer", "null"]

    def test_required_field_stays_non_nullable(self):
        out = to_openai_strict(WithOptional)
        assert out["properties"]["required_field"]["type"] == "string"

    def test_default_values_are_dropped(self):
        out = to_openai_strict(WithOptional)
        assert "default" not in out["properties"]["optional_int"]


class TestAdditionalPropertiesRecursion:
    def test_root_object_closed(self):
        assert to_openai_strict(Nested)["additionalProperties"] is False

    def test_nested_defs_object_closed(self):
        out = to_openai_strict(Nested)
        inner = out["$defs"]["Inner"]
        assert inner["additionalProperties"] is False
        assert set(inner["required"]) == {"name", "color"}

    def test_open_mapping_is_rejected_loudly(self):
        class OpenMap(BaseModel):
            extras: dict[str, int]

        with pytest.raises(ValueError, match="strict mode cannot represent"):
            to_openai_strict(OpenMap)


class TestEnumsAndRefs:
    def test_enum_values_preserved(self):
        out = to_openai_strict(Nested)
        assert out["$defs"]["Color"]["enum"] == ["red", "blue"]

    def test_refs_are_kept_not_inlined(self):
        # OpenAI strict accepts $defs, so inlining them here would be work
        # for nothing; ref flattening belongs to whichever provider rejects
        # them, not to this transform.
        out = to_openai_strict(Nested)
        assert out["properties"]["inners"]["items"] == {"$ref": "#/$defs/Inner"}

    def test_optional_ref_field_gets_anyof_null(self):
        class OptionalRef(BaseModel):
            maybe_inner: Inner | None = None

        prop = to_openai_strict(OptionalRef)["properties"]["maybe_inner"]
        assert {"type": "null"} in prop["anyOf"]

    def test_optional_enum_field_admits_null_value(self):
        class OptionalEnum(BaseModel):
            color: Color = Color.RED

        prop = to_openai_strict(OptionalEnum)["properties"]["color"]
        # The ref branch is preserved and null is admitted alongside it.
        assert any(b == {"type": "null"} for b in prop["anyOf"])

    def test_titles_are_dropped(self):
        out = to_openai_strict(Nested)
        assert "title" not in out
        assert "title" not in out["properties"]["inner"]
