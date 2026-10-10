#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Prove a staged GitHub release carries exactly what a consumer needs to verify it.

    scripts/verify_release_assets.py <release-dir> <version>
    scripts/verify_release_assets.py <release-dir> <version> --attestations

`mise run release:assets` stages the release in one directory: the wheel and the sdist that go
to PyPI, a CycloneDX and an SPDX SBOM of the locked runtime closure, and a `SHA256SUMS`
manifest. `publish.yml` then signs Sigstore attestations over those files and adds them as
`atif_sql-<version>.intoto.jsonl`, one bundle per line. This is the release gate both steps
run before anything is uploaded, and its rule is that an asset nobody can verify fails the
release rather than shipping beside the ones that can be:

- every file is one the release lists by name, and every name carries the version
                                   (a stray file, or a version that disagrees with the tag)
- `SHA256SUMS` names every other asset, and every digest matches its file
- both SBOMs parse, and each names atif-sql at this version as its subject
- with `--attestations`: the `.intoto.jsonl` holds a SLSA provenance statement whose subjects
  are every asset's digest, and an SBOM statement whose subjects are the wheel and the sdist

The signatures themselves are checked by `gh attestation verify` in the workflow, against the
signing workflow's identity; this script checks what was signed. The release is short of
assets in two ways that must not read alike: a missing asset is a finding (exit 1), and an
empty or absent directory means the staging step produced nothing to judge (exit 2).

stdlib only, matching the other gate helpers: it runs on a runner that has no venv.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

#: One decoded JSON value, as in scripts/vex_to_osv_config.py.
type JsonValue = str | int | float | bool | list["JsonValue"] | dict[str, "JsonValue"] | None

DISTRIBUTION = "atif-sql"
STEM = "atif_sql"
SUMS = "SHA256SUMS"
SLSA_PROVENANCE = "https://slsa.dev/provenance/v1"
#: The predicate types actions/attest writes for an SBOM, by format.
SBOM_PREDICATES = frozenset({"https://cyclonedx.org/bom", "https://spdx.dev/Document/v2.3"})


def expected_names(version: str, *, attestations: bool) -> dict[str, str]:
    """Map each asset the release must carry to what it is."""
    names = {
        f"{STEM}-{version}-py3-none-any.whl": "wheel",
        f"{STEM}-{version}.tar.gz": "sdist",
        f"{STEM}-{version}.cdx.json": "cyclonedx",
        f"{STEM}-{version}.spdx.json": "spdx",
        SUMS: "sums",
    }
    if attestations:
        names[f"{STEM}-{version}.intoto.jsonl"] = "attestations"
    return names


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path, problems: list[str]) -> JsonValue:
    try:
        value: JsonValue = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        problems.append(f"UNPARSABLE {path.name}: {error}")
        return None
    return value


def _check_sums(directory: Path, names: dict[str, str], problems: list[str]) -> None:
    listed: dict[str, str] = {}
    for number, line in enumerate((directory / SUMS).read_text(encoding="utf-8").splitlines(), 1):
        digest, _, name = line.partition("  ")
        name = name.removeprefix("*")
        if len(digest) != 64 or not name:  # noqa: PLR2004 - a sha256 hex digest is 64 characters
            problems.append(f"SUMS {SUMS} line {number} is not '<sha256>  <name>': {line!r}")
            continue
        listed[name] = digest
    covered = {n for n, kind in names.items() if kind not in {"sums", "attestations"}}
    problems.extend(f"SUMS {SUMS} does not list {name}" for name in sorted(covered - listed.keys()))
    problems.extend(
        f"SUMS {SUMS} lists {name}, which is not a release asset"
        for name in sorted(listed.keys() - covered)
    )
    for name in sorted(covered & listed.keys()):
        actual = _sha256(directory / name)
        if listed[name] != actual:
            problems.append(f"DIGEST {name} is {actual}, {SUMS} says {listed[name]}")


def _check_cyclonedx(path: Path, version: str, problems: list[str]) -> None:
    document = _load_json(path, problems)
    if not isinstance(document, dict):
        if document is not None:
            problems.append(f"SBOM {path.name} is not a JSON object")
        return
    metadata = document.get("metadata")
    subject = metadata.get("component") if isinstance(metadata, dict) else None
    components = document.get("components")
    if document.get("bomFormat") != "CycloneDX":
        problems.append(f"SBOM {path.name} bomFormat is {document.get('bomFormat')!r}")
    if not isinstance(subject, dict) or (subject.get("name"), subject.get("version")) != (
        DISTRIBUTION,
        version,
    ):
        problems.append(f"SBOM {path.name} does not name {DISTRIBUTION} {version} as its subject")
    if not isinstance(components, list) or not components:
        problems.append(f"SBOM {path.name} lists no components")


def _check_spdx(path: Path, version: str, problems: list[str]) -> None:
    document = _load_json(path, problems)
    if not isinstance(document, dict):
        if document is not None:
            problems.append(f"SBOM {path.name} is not a JSON object")
        return
    spdx_version = document.get("spdxVersion")
    packages = document.get("packages")
    if not isinstance(spdx_version, str) or not spdx_version.startswith("SPDX-2."):
        problems.append(f"SBOM {path.name} spdxVersion is {spdx_version!r}")
    if not isinstance(packages, list) or not any(
        isinstance(p, dict) and (p.get("name"), p.get("versionInfo")) == (DISTRIBUTION, version)
        for p in packages
    ):
        problems.append(f"SBOM {path.name} has no package {DISTRIBUTION} {version}")
    elif len(packages) < 2:  # noqa: PLR2004 - the subject plus at least one dependency
        problems.append(f"SBOM {path.name} lists no dependency of {DISTRIBUTION}")


def _statements(path: Path, problems: list[str]) -> list[dict[str, JsonValue]]:
    """Decode the in-toto statement inside every Sigstore bundle line."""
    statements: list[dict[str, JsonValue]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            bundle: JsonValue = json.loads(line)
            envelope = bundle.get("dsseEnvelope") if isinstance(bundle, dict) else None
            payload = envelope.get("payload") if isinstance(envelope, dict) else None
            if not isinstance(payload, str):
                problems.append(f"ATTESTATION {path.name} line {number} has no DSSE payload")
                continue
            statement: JsonValue = json.loads(base64.b64decode(payload, validate=True))
        except (json.JSONDecodeError, binascii.Error, UnicodeDecodeError) as error:
            problems.append(f"ATTESTATION {path.name} line {number} does not decode: {error}")
            continue
        if isinstance(statement, dict):
            statements.append(statement)
    return statements


def _subject_digests(statement: dict[str, JsonValue]) -> set[str]:
    subjects = statement.get("subject")
    digests: set[str] = set()
    if isinstance(subjects, list):
        for subject in subjects:
            digest = subject.get("digest") if isinstance(subject, dict) else None
            value = digest.get("sha256") if isinstance(digest, dict) else None
            if isinstance(value, str):
                digests.add(value)
    return digests


def _check_attestations(
    directory: Path, names: dict[str, str], version: str, problems: list[str]
) -> None:
    path = directory / f"{STEM}-{version}.intoto.jsonl"
    statements = _statements(path, problems)
    provenance = [s for s in statements if s.get("predicateType") == SLSA_PROVENANCE]
    sboms = [s for s in statements if s.get("predicateType") in SBOM_PREDICATES]
    if not provenance:
        problems.append(f"NO-PROVENANCE {path.name} holds no {SLSA_PROVENANCE} statement")
    if not sboms:
        problems.append(f"NO-SBOM-ATTESTATION {path.name} holds no SBOM statement")
    attested = set().union(*(_subject_digests(s) for s in provenance))
    for name, kind in sorted(names.items()):
        if kind != "attestations" and _sha256(directory / name) not in attested:
            problems.append(f"NO-PROVENANCE {name} is not a subject of the provenance")
    sbom_subjects = set().union(*(_subject_digests(s) for s in sboms))
    for name, kind in sorted(names.items()):
        if kind in {"wheel", "sdist"} and _sha256(directory / name) not in sbom_subjects:
            problems.append(f"NO-SBOM-ATTESTATION {name} is not a subject of an SBOM statement")


def verify(directory: Path, version: str, *, attestations: bool) -> tuple[int, list[str]]:
    """Return the exit code and every finding for the release staged in `directory`."""
    if not directory.is_dir() or not any(directory.iterdir()):
        return 2, [f"EMPTY {directory} holds no release assets: the staging step produced nothing"]
    names = expected_names(version, attestations=attestations)
    present = {p.name for p in directory.iterdir() if p.is_file()}
    problems = [
        f"UNLISTED {name} is not a release asset" for name in sorted(present - names.keys())
    ]
    problems += [f"MISSING {name}" for name in sorted(names.keys() - present)]
    if problems:
        return 1, problems
    _check_sums(directory, names, problems)
    _check_cyclonedx(directory / f"{STEM}-{version}.cdx.json", version, problems)
    _check_spdx(directory / f"{STEM}-{version}.spdx.json", version, problems)
    if attestations:
        _check_attestations(directory, names, version, problems)
    return (1 if problems else 0), problems


def main(argv: Sequence[str]) -> int:
    """Entry point. Returns the process exit code; 2 also means the ARGUMENTS were wrong."""
    parser = argparse.ArgumentParser(
        prog="scripts/verify_release_assets.py",
        description="Check a staged release directory before it is uploaded.",
    )
    parser.add_argument("directory", help="the staged release directory")
    parser.add_argument("version", help="the release version, without the leading v")
    parser.add_argument(
        "--attestations",
        action="store_true",
        help="also require the .intoto.jsonl and check what its statements cover",
    )
    args = parser.parse_args(argv)
    version = str(args.version).removeprefix("v")
    directory = Path(args.directory)
    code, problems = verify(directory, version, attestations=bool(args.attestations))
    for problem in problems:
        sys.stderr.write(f"release {version}: {problem}\n")
    if code == 0:
        count = len(expected_names(version, attestations=bool(args.attestations)))
        checked = "assets, attestations covered" if args.attestations else "assets"
        sys.stdout.write(f"release {version}: {count} {checked}, 0 findings\n")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
