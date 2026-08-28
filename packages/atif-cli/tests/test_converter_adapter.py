# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the ConverterPort adapter mapping (ConversionResult -> ConversionOutput)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from atif_cli.converter_adapter import RealConverter
from atif_converter.domain.errors import TrajectoryValidationError
from atif_converter.domain.fidelity import FidelityGap, LossReport, RecordType
from atif_converter.infrastructure.harbor_adapter import ConversionResult
from atif_corpus.domain.ports import ConversionOutput, ConverterPort


def _fake_convert_and_audit(
    trajectory: dict[str, Any],
    *,
    validation_errors: tuple[str, ...] = (),
    edges_lines: tuple[str, ...] = ('{"uuid":"u-1"}',),
) -> tuple[ConversionResult, LossReport]:
    result = ConversionResult(
        trajectory=trajectory,
        validation_errors=validation_errors,
        edges_lines=edges_lines,
    )
    report = LossReport(
        record_counts={RecordType.USER: 2, RecordType.ASSISTANT: 2, RecordType.SUMMARY: 1},
        records_converted=4,
        records_dropped=1,
        gaps_observed=frozenset({FidelityGap.NON_MESSAGE_RECORDS_DROPPED}),
        subagent_files_found=1,
        subagent_files_convertible=1,
        workflow_subagent_files_found=0,
    )
    return result, report


class TestAdapterMapping:
    def test_maps_result_fields_onto_conversion_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        trajectory: dict[str, Any] = {
            "schema_version": "ATIF-v1.7",
            "session_id": "s-1",
            "steps": [],
        }

        def _stub(path: Path, *, include_subagents: bool) -> tuple[ConversionResult, LossReport]:
            del path, include_subagents
            return _fake_convert_and_audit(trajectory)

        monkeypatch.setattr("atif_cli.converter_adapter.convert_and_audit", _stub)
        output = RealConverter().convert(Path("s-1.jsonl"))

        assert isinstance(output, ConversionOutput)
        assert output.trajectory_dict is trajectory
        assert output.edges_lines == ['{"uuid":"u-1"}']
        # loss_report_dict is the CONTRACT loss_report.json shape: enum
        # members flattened, records_total materialized as a plain key.
        assert output.loss_report_dict["records_total"] == 5
        assert output.loss_report_dict["records_converted"] == 4
        assert output.loss_report_dict["records_dropped"] == 1
        assert output.loss_report_dict["record_counts"] == {
            "assistant": 2,
            "summary": 1,
            "user": 2,
        }
        assert output.loss_report_dict["gaps_observed"] == ["non_message_records_dropped"]
        # Fully JSON-serializable — the corpus writer json.dumps this verbatim.
        json.dumps(output.loss_report_dict)

    def test_validation_errors_raise_instead_of_materializing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _stub(path: Path, *, include_subagents: bool) -> tuple[ConversionResult, LossReport]:
            del path, include_subagents
            return _fake_convert_and_audit({"steps": []}, validation_errors=("step 3: bad shape",))

        monkeypatch.setattr("atif_cli.converter_adapter.convert_and_audit", _stub)
        with pytest.raises(TrajectoryValidationError):
            RealConverter().convert(Path("s-1.jsonl"))

    def test_real_converter_satisfies_the_port(self) -> None:
        # Structural check: assigning to the Protocol type is what ty
        # verifies; at runtime we assert the one required method exists.
        converter: ConverterPort = RealConverter()
        assert callable(converter.convert)

    def test_adapter_output_round_trips_through_real_conversion(
        self, synthetic_session: Path
    ) -> None:
        """End-to-end through the REAL convert_and_audit (harbor) once."""
        output = RealConverter().convert(synthetic_session)
        assert output.trajectory_dict["schema_version"].startswith("ATIF")
        assert output.trajectory_dict["steps"]
        assert output.loss_report_dict["records_total"] >= 4
        for line in output.edges_lines:
            edge = json.loads(line)
            assert "uuid" in edge
