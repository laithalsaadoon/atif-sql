# SPDX-License-Identifier: Apache-2.0

"""`scripts/verify_release_assets.py` is the gate a GitHub release passes before upload.

publish.yml stages the release with `mise run release:assets`, attests it, and uploads it only
when this script accepts the directory. A release asset nobody can verify must fail the release,
so each planted defect below turns the gate red, and a well-formed release is green: the
anti-vacuity proof every gate here carries.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

#: Repository root: `packages/atif-cli/tests/` -> three parents up.
ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "verify_release_assets.py"
PUBLISH = ROOT / ".github" / "workflows" / "publish.yml"
VERSION = "0.2.0"
WHEEL = f"atif_sql-{VERSION}-py3-none-any.whl"
SDIST = f"atif_sql-{VERSION}.tar.gz"
CDX = f"atif_sql-{VERSION}.cdx.json"
SPDX = f"atif_sql-{VERSION}.spdx.json"
INTOTO = f"atif_sql-{VERSION}.intoto.jsonl"
SLSA = "https://slsa.dev/provenance/v1"
CYCLONEDX = "https://cyclonedx.org/bom"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bundle(predicate_type: str, files: list[Path]) -> str:
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": f.name, "digest": {"sha256": _sha(f)}} for f in files],
        "predicateType": predicate_type,
        "predicate": {},
    }
    payload = base64.b64encode(json.dumps(statement).encode()).decode()
    envelope = {"payloadType": "application/vnd.in-toto+json", "payload": payload}
    return json.dumps({"mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
                       "dsseEnvelope": envelope})  # fmt: skip


def _write_sums(directory: Path) -> None:
    lines = [f"{_sha(directory / name)}  {name}" for name in sorted([WHEEL, SDIST, CDX, SPDX])]
    (directory / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _stage(tmp_path: Path, *, attest: bool = True) -> Path:
    directory = tmp_path / "release"
    directory.mkdir()
    (directory / WHEEL).write_bytes(b"wheel bytes")
    (directory / SDIST).write_bytes(b"sdist bytes")
    cdx = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "metadata": {"component": {"name": "atif-sql", "version": VERSION}},
        "components": [{"name": "duckdb", "version": "1.5.6", "purl": "pkg:pypi/duckdb@1.5.6"}],
    }
    (directory / CDX).write_text(json.dumps(cdx), encoding="utf-8")
    spdx = {
        "spdxVersion": "SPDX-2.3",
        "packages": [
            {"name": "atif-sql", "versionInfo": VERSION},
            {"name": "duckdb", "versionInfo": "1.5.6"},
        ],
    }
    (directory / SPDX).write_text(json.dumps(spdx), encoding="utf-8")
    _write_sums(directory)
    if attest:
        _attest(directory)
    return directory


def _attest(directory: Path, *, skip: str = "", sbom: bool = True) -> None:
    files = [directory / n for n in sorted([WHEEL, SDIST, CDX, SPDX, "SHA256SUMS"]) if n != skip]
    lines = [_bundle(SLSA, files)]
    if sbom:
        lines.append(_bundle(CYCLONEDX, [directory / WHEEL, directory / SDIST]))
    (directory / INTOTO).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run(directory: Path, *, attestations: bool = True) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, str(SCRIPT), str(directory), VERSION]
    if attestations:
        argv.append("--attestations")
    return subprocess.run(  # noqa: S603 - fixed interpreter and repository script
        argv, capture_output=True, text=True, check=False, cwd=ROOT
    )


def test_staged_release_without_attestations_is_green(tmp_path: Path) -> None:
    result = _run(_stage(tmp_path, attest=False), attestations=False)
    assert result.returncode == 0, result.stderr
    assert f"release {VERSION}: 5 assets, 0 findings" in result.stdout


def test_attested_release_is_green(tmp_path: Path) -> None:
    result = _run(_stage(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "6 assets, attestations covered, 0 findings" in result.stdout


def test_empty_directory_is_vacuous(tmp_path: Path) -> None:
    (tmp_path / "release").mkdir()
    result = _run(tmp_path / "release")
    assert result.returncode == 2
    assert "EMPTY" in result.stderr


def test_stray_file_is_red(tmp_path: Path) -> None:
    directory = _stage(tmp_path)
    (directory / "helper").write_bytes(b"\x7fELF")
    result = _run(directory)
    assert result.returncode == 1
    assert "UNLISTED helper" in result.stderr


def test_missing_sbom_is_red(tmp_path: Path) -> None:
    directory = _stage(tmp_path)
    (directory / SPDX).unlink()
    result = _run(directory)
    assert result.returncode == 1
    assert f"MISSING {SPDX}" in result.stderr


def test_version_that_disagrees_with_the_names_is_red(tmp_path: Path) -> None:
    directory = _stage(tmp_path)
    argv = [sys.executable, str(SCRIPT), str(directory), "0.2.1", "--attestations"]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
    assert result.returncode == 1
    assert "MISSING atif_sql-0.2.1-py3-none-any.whl" in result.stderr
    assert f"UNLISTED {WHEEL}" in result.stderr


def test_one_byte_appended_to_the_wheel_is_red(tmp_path: Path) -> None:
    directory = _stage(tmp_path)
    with (directory / WHEEL).open("ab") as wheel:
        wheel.write(b"x")
    result = _run(directory)
    assert result.returncode == 1
    assert f"DIGEST {WHEEL}" in result.stderr
    assert f"NO-PROVENANCE {WHEEL} is not a subject of the provenance" in result.stderr


def test_sums_that_omit_an_asset_is_red(tmp_path: Path) -> None:
    directory = _stage(tmp_path)
    sums = directory / "SHA256SUMS"
    kept = [line for line in sums.read_text().splitlines() if not line.endswith(SDIST)]
    sums.write_text("\n".join(kept) + "\n")
    _attest(directory)
    result = _run(directory)
    assert result.returncode == 1
    assert f"SHA256SUMS does not list {SDIST}" in result.stderr


def test_sbom_for_another_version_is_red(tmp_path: Path) -> None:
    directory = _stage(tmp_path, attest=False)
    document = json.loads((directory / CDX).read_text())
    document["metadata"]["component"]["version"] = "0.1.0"
    (directory / CDX).write_text(json.dumps(document))
    _write_sums(directory)
    _attest(directory)
    result = _run(directory)
    assert result.returncode == 1
    assert f"SBOM {CDX} does not name atif-sql {VERSION}" in result.stderr


def test_asset_outside_the_provenance_is_red(tmp_path: Path) -> None:
    directory = _stage(tmp_path)
    _attest(directory, skip=SPDX)
    result = _run(directory)
    assert result.returncode == 1
    assert f"NO-PROVENANCE {SPDX} is not a subject of the provenance" in result.stderr


def test_missing_sbom_attestation_is_red(tmp_path: Path) -> None:
    directory = _stage(tmp_path)
    _attest(directory, sbom=False)
    result = _run(directory)
    assert result.returncode == 1
    assert "NO-SBOM-ATTESTATION" in result.stderr


def test_undecodable_attestation_line_is_red(tmp_path: Path) -> None:
    directory = _stage(tmp_path)
    (directory / INTOTO).write_text('{"dsseEnvelope": {"payload": "not base64!"}}\n')
    result = _run(directory)
    assert result.returncode == 1
    assert "does not decode" in result.stderr
    assert "NO-PROVENANCE" in result.stderr


def test_publish_workflow_gates_the_upload_on_this_script() -> None:
    # The gate only protects a release if the workflow that uploads runs it, after the signatures
    # are checked against the signing workflow, and before `gh release upload`.
    text = PUBLISH.read_text(encoding="utf-8")
    stage = text.index('run: RELEASE_VERSION="${RELEASE_TAG#v}" mise run release:assets')
    verify_signatures = text.index('gh attestation verify "$asset" --bundle')
    verify_assets = text.index('verify_release_assets.py release "$VERSION" --attestations')
    upload = text.index("run: gh release upload")
    assert stage < verify_assets < upload
    assert verify_signatures < upload
    assert "--signer-workflow" in text
