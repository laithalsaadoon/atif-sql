# SPDX-License-Identifier: Apache-2.0

"""`scripts/verify_release_assets.py` is the gate a GitHub release passes before upload.

publish.yml preparation stages the release with `mise run release:assets`, attests it, and uploads it only
when this script accepts the directory. A release asset nobody can verify must fail the release,
so each planted defect below turns the gate red, and a well-formed release is green: the
anti-vacuity proof every gate here carries.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import textwrap
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
    upload = text.index('gh release upload "$TAG" "$asset"')
    assert stage < verify_assets < upload
    assert verify_signatures < upload
    assert "--signer-workflow" in text


def test_pypi_consumes_verified_immutable_assets_without_rebuilding() -> None:
    text = PUBLISH.read_text(encoding="utf-8")
    verify = text.split("  verify:\n", 1)[1].split("  # WHAT A MAINTAINER", 1)[0]
    publish = text.split("  publish:\n", 1)[1]
    assert ".draft == false and .immutable == true" in verify
    assert "gh release verify" in verify
    assert verify.index("gh release download") < verify.index("gh attestation verify")
    assert verify.index("--attestations") < verify.index("cp ")
    assert "--source-ref" in verify and "--source-digest" in verify
    assert "--predicate-type https://cyclonedx.org/bom" in verify
    assert "uv build" not in verify + publish
    assert "gh release upload" not in verify + publish
    assert "needs: verify" in publish


def test_preparation_requires_draft_and_tag_context_before_build() -> None:
    text = PUBLISH.read_text(encoding="utf-8")
    draft = text.split("  draft:\n", 1)[1].split("  build:\n", 1)[0]
    build = text.split("  build:\n", 1)[1].split("  attest:\n", 1)[0]
    assert "if: inputs.prepare" in draft
    assert '[ "$REF" = "refs/tags/$TAG" ]' in draft
    assert ".draft == true" in draft
    assert "contents: write" in draft and "actions/checkout@" not in draft
    assert "if: inputs.prepare" in build
    assert "needs: draft" in build and "contents: read" in build
    assert "ref: ${{ github.sha }}" in build
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "actions: write" in release
    assert '--ref "v$VERSION"' in release and "-f prepare=true" in release


def _upload_run_block() -> str:
    """Execute the workflow's shell rather than a second implementation of its retry policy."""
    text = PUBLISH.read_text(encoding="utf-8")
    step = text.split("      - name: Upload the assets to the release\n", 1)[1]
    body = step.split("        run: |\n", 1)[1]
    lines: list[str] = []
    for line in body.splitlines():
        if line.strip() and not line.startswith("          "):
            break
        lines.append(line)
    script = textwrap.dedent("\n".join(lines))
    assert "gh release upload" in script and "final-release" in script
    return script


# This fake implements only the API and release commands the upload step uses. Every other
# invocation fails, and upload refuses replacement, so a policy regression cannot call GitHub
# or turn a mismatched asset into a successful retry.
FAKE_GH = """\
import json
import pathlib
import shutil
import sys

args = sys.argv[1:]
root = pathlib.Path.cwd()
remote = root / 'remote'
state = json.loads((root / 'state.json').read_text())
with (root / 'gh-calls.jsonl').open('a') as log:
    log.write(json.dumps(args) + '\\n')
if args[0] == 'api':
    query = args[args.index('--jq') + 1]
    if query == '.draft':
        print('true' if state['draft'] else 'false')
    elif query == '.sha':
        print(state['source_sha'])
    elif query == '.assets | length':
        print(len(list(remote.iterdir())))
    else:
        raise SystemExit('unexpected API query: ' + query)
elif args[:2] == ['release', 'download']:
    destination = root / args[args.index('--dir') + 1]
    destination.mkdir(exist_ok=True)
    if destination.name == 'final-release' and state['stray']:
        (remote / 'helper').write_bytes(b'unlisted asset')
    for asset in remote.iterdir():
        shutil.copyfile(asset, destination / asset.name)
elif args[:2] == ['release', 'upload']:
    asset = root / args[3]
    destination = remote / asset.name
    if destination.exists() or '--clobber' in args:
        raise SystemExit('asset replacement refused')
    shutil.copyfile(asset, destination)
else:
    raise SystemExit('unexpected gh command: ' + repr(args))
"""


def _run_upload(
    tmp_path: Path,
    *,
    existing: tuple[str, ...] = (),
    mismatch: bool = False,
    draft: bool = True,
    stray: bool = False,
) -> subprocess.CompletedProcess[str]:
    directory = _stage(tmp_path)
    remote = tmp_path / "remote"
    remote.mkdir()
    for name in existing:
        shutil.copyfile(directory / name, remote / name)
    if mismatch:
        (remote / WHEEL).write_bytes(b"different wheel bytes")
    source_sha = "a" * 40
    state = {"draft": draft, "source_sha": source_sha, "stray": stray}
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f"#!{sys.executable}\n{FAKE_GH}", encoding="utf-8")
    gh.chmod(0o755)
    (bin_dir / "python3").symlink_to(sys.executable)
    shutil.copyfile(SCRIPT, tmp_path / "verify_release_assets.py")
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GH_TOKEN": "test",
        "TAG": f"v{VERSION}",
        "VERSION": VERSION,
        "REPO": "example/atif-sql",
        "SOURCE_SHA": source_sha,
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
    }
    return subprocess.run(  # noqa: S603 - fixed bash executes the local workflow with a fake gh
        ["bash", "-c", _upload_run_block()],  # noqa: S607 - bash is the repository's shell
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env=environment,
    )


def _uploaded_names(tmp_path: Path) -> list[str]:
    calls = [json.loads(line) for line in (tmp_path / "gh-calls.jsonl").read_text().splitlines()]
    return [Path(args[3]).name for args in calls if args[:2] == ["release", "upload"]]


def test_draft_upload_retry_reuses_identical_existing_assets(tmp_path: Path) -> None:
    names = (WHEEL, SDIST, CDX, SPDX, INTOTO, "SHA256SUMS")
    result = _run_upload(tmp_path, existing=names)
    assert result.returncode == 0, result.stderr
    assert _uploaded_names(tmp_path) == []
    assert "6 assets, attestations covered, 0 findings" in result.stdout
    assert (tmp_path / "summary.md").is_file()


def test_draft_upload_retry_uploads_only_missing_assets(tmp_path: Path) -> None:
    result = _run_upload(tmp_path, existing=(WHEEL,))
    assert result.returncode == 0, result.stderr
    assert set(_uploaded_names(tmp_path)) == {SDIST, CDX, SPDX, INTOTO, "SHA256SUMS"}
    for asset in (tmp_path / "release").iterdir():
        assert (tmp_path / "remote" / asset.name).read_bytes() == asset.read_bytes()


def test_draft_upload_retry_refuses_mismatched_asset_without_replacement(tmp_path: Path) -> None:
    names = (WHEEL, SDIST, CDX, SPDX, INTOTO, "SHA256SUMS")
    result = _run_upload(tmp_path, existing=names, mismatch=True)
    assert result.returncode != 0
    assert "differ" in result.stdout
    assert _uploaded_names(tmp_path) == []
    assert (tmp_path / "remote" / WHEEL).read_bytes() == b"different wheel bytes"
    assert not (tmp_path / "summary.md").exists()


def test_draft_upload_rejects_stray_asset_in_final_download(tmp_path: Path) -> None:
    result = _run_upload(tmp_path, stray=True)
    assert result.returncode != 0
    assert "UNLISTED helper" in result.stderr
    assert not (tmp_path / "summary.md").exists()


def test_draft_upload_refuses_release_published_during_build(tmp_path: Path) -> None:
    result = _run_upload(tmp_path, draft=False)
    assert result.returncode != 0
    assert _uploaded_names(tmp_path) == []
    assert list((tmp_path / "remote").iterdir()) == []
    assert not (tmp_path / "summary.md").exists()
