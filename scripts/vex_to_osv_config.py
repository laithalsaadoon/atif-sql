#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Render `osv-scanner.toml` from the OpenVEX ledger, and fail when the two disagree.

    scripts/vex_to_osv_config.py <ledger.json> <output.toml> [--lock uv.lock]
    scripts/vex_to_osv_config.py <ledger.json> <output.toml> [--lock uv.lock] --check

`security/atif-sql.openvex.json` is the ONE record of why a vulnerability finding is
suppressed. Every scanner's suppression surface is derived from it, because three scanners
disagree about how to read a VEX document and a hand-kept ignore list per scanner is how a
suppression outlives the reasoning that justified it.

WHY OSV-SCANNER NEEDS THIS RENDERER AT ALL
Probed 2026-08-28 against osv-scanner 2.5.0 (the version mise.lock pins): it has no `--vex`
flag, on `scan source` or anywhere else. grype 0.111.1 and trivy 0.70.0 both do, so they read
the ledger directly and are not this script's problem. osv-scanner reads `osv-scanner.toml`,
so that is what gets generated.

THE VERSIONED PROPERTY, AND WHERE IT IS ACTUALLY ENFORCED
A VEX statement scopes to product versions through PURLs, so a suppression names the exact
version whose reachability was argued about and stops applying when the dependency moves. A
blanket ignore does not — it keeps suppressing after an upgrade changes the argument out from
under it, silently.

osv-scanner 2.5.0 cannot express that scoping. Read from its own source at v2.5.0
(`internal/config/config.go`): `IgnoreEntry` is `{id, ignoreUntil, reason}` and
`Config.ShouldIgnore(vulnID)` matches on the id ALONE, version-blind; the other surface,
`PackageOverrideEntry`, does match on `{name, version, ecosystem}` but carries no vulnerability
id, so `vulnerability.ignore = true` there suppresses EVERY finding against that package
version including advisories nobody has read. Neither is faithful, so this renderer emits the
vulnerability-scoped one — the axis a VEX statement is *about* — and reconstructs the version
scoping as an ASSERTION instead:

    every `pkg:pypi/<name>@<version>` in the ledger must equal the version uv.lock resolves
    for that name, or this script fails.

That is stronger than what a version-scoped config entry would have given, because a config
entry that stops matching is invisible — the finding quietly reappears, or quietly does not,
depending on whether the new version is affected. A failed assertion is loud: the moment a
bump moves the package, `mise run check` goes red and a human has to re-establish the
reachability argument before the suppression can follow the dependency forward.

WHAT THIS ALSO VALIDATES, AND WHY IT HAS TO
grype does not validate the document. Probed 2026-08-28: grype 0.111.1 exits 0 on a ledger
carrying `"status": "bogus_status"`, so a typo'd status silently suppresses nothing. It DOES
reject a malformed or missing document (exit 1, `failed to create VEX processor`), and so does
trivy — so the ledger's existence and its JSON are covered by the scanners, but its MEANING is
covered only here.

THE EMPTY LEDGER IS A LEGITIMATE STATE
The published OpenVEX JSON schema sets `minItems: 1` on `statements`, so an empty ledger is not
schema-valid. It is shipped empty anyway, and deliberately: no scanner finding in this
repository currently needs suppressing, and adding a placeholder statement to satisfy `minItems`
would put a fact in the record that is not true. Probed 2026-08-28: grype 0.111.1 and trivy
0.70.0 both accept `"statements": []` and exit 0, so nothing downstream breaks. The worked
example in `security/examples/` is what carries the shape a real statement must have.

stdlib only, matching scripts/verify_sarif.py: this runs inside `mise run check`, which must not
depend on a network or on anything a broken environment removes.

Exit 0 when the render (or the check) succeeds, 1 with the failing property on stderr when it
does not, 2 on a usage error.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

# Mirrors the published schema's `additionalProperties: false` at document level. A key outside
# this set is a typo or a private extension, and either way grype and trivy will ignore it while
# a reader assumes it did something.
_DOCUMENT_KEYS = frozenset(
    {
        "@context",
        "@id",
        "author",
        "role",
        "timestamp",
        "last_updated",
        "version",
        "tooling",
        "statements",
    }
)

_STATEMENT_KEYS = frozenset(
    {
        "@id",
        "version",
        "vulnerability",
        "timestamp",
        "last_updated",
        "products",
        "status",
        "supplier",
        "status_notes",
        "justification",
        "impact_statement",
        "action_statement",
        "action_statement_timestamp",
    }
)

# OpenVEX v0.2.0, read from https://github.com/openvex/spec/openvex_json_schema_0.2.0.json.
_STATUSES = frozenset({"not_affected", "affected", "fixed", "under_investigation"})
_JUSTIFICATIONS = frozenset(
    {
        "component_not_present",
        "vulnerable_code_not_present",
        "vulnerable_code_not_in_execute_path",
        "vulnerable_code_cannot_be_controlled_by_adversary",
        "inline_mitigations_already_exist",
    }
)

# Only `not_affected` and `fixed` remove a finding; grype and trivy both treat the other two as
# informational. Rendering an ignore for `affected` would suppress a finding the ledger says is
# real, which is the one thing this pipeline must never do.
_SUPPRESSING_STATUSES = frozenset({"not_affected", "fixed"})

_HEADER = """\
# GENERATED by scripts/vex_to_osv_config.py from {source} — do not edit.
#
# Edit the ledger, then run `mise run security:vex` and commit both files.
# `mise run security:vex:check` (inside `mise run check`) fails if this file drifts from it.
#
# WHY THIS FILE IS GENERATED
# Probed 2026-08-28: osv-scanner 2.5.0 has no `--vex` flag, so the OpenVEX ledger is rendered
# into the one suppression surface it does have. grype 0.111.1 and trivy 0.70.0 both accept
# `--vex` and read the ledger directly, so they have no generated counterpart.
#
# WHY THE VERSION IS NOT IN THE ENTRIES BELOW
# A VEX statement scopes to a product VERSION through its PURL, so a suppression stops applying
# when the dependency moves. osv-scanner 2.5.0 cannot express that: read from its source at
# v2.5.0, `Config.ShouldIgnore(vulnID)` matches `[[IgnoredVulns]]` on the id alone, and the
# version-aware `[[PackageOverrides]]` carries no vulnerability id, so using it would suppress
# every future advisory against the same pinned version too. So the id-scoped entry is what is
# emitted, and the version scoping is enforced by the generator instead: it asserts every PURL
# version in the ledger against uv.lock and FAILS when a bump moves the package, which turns a
# silent stale suppression into a red gate.
{body}"""

#: One decoded JSON value. `json.loads` is typed `Any`, so without a declared shape every
#: read below is checked against nothing; naming the shape here makes each `isinstance`
#: narrowing land on a real type and keeps the reads type-checked. Recursive by design —
#: the documents these scripts read nest objects inside arrays inside objects.
type JsonValue = str | int | float | bool | list["JsonValue"] | dict[str, "JsonValue"] | None

_EMPTY_BODY = """\
#
# The ledger carries no statements. That is the intended state: nothing this repository ships
# currently needs a suppression, and every scanner reported zero on the run that produced this
# file. The pipeline exists and is exercised anyway — see security/examples/ for the shape a
# real statement takes.
"""


def _fail(reason: str) -> NoReturn:
    """Reject the ledger or the render. The reason names the property, which is the value."""
    sys.stderr.write(f"vex-to-osv-config: {reason}\n")
    sys.exit(1)


def _get(value: JsonValue, key: str) -> JsonValue:
    """Read `key` out of `value` when it is a JSON object, else None."""
    return value.get(key) if isinstance(value, dict) else None


def _locked_versions(lock_path: Path) -> dict[str, str]:
    """Map every package name in uv.lock to its resolved version."""
    try:
        raw = lock_path.read_bytes()
    except OSError as error:
        _fail(f"cannot read {lock_path}: {error.strerror or type(error).__name__}")
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        _fail(f"{lock_path} is not parseable TOML: {error}")
    packages: JsonValue = document.get("package")
    if not isinstance(packages, list) or not packages:
        _fail(f"{lock_path} carries no [[package]] entries")
    resolved: dict[str, str] = {}
    for entry in packages:
        name = _get(entry, "name")
        version = _get(entry, "version")
        if isinstance(name, str) and isinstance(version, str):
            resolved[name] = version
    return resolved


def _split_purl(purl: str) -> tuple[str, str, str]:
    """Split `pkg:<ecosystem>/<name>@<version>` into its three parts.

    A PURL with no `@version` is rejected here rather than downstream: an unversioned product
    is the blanket suppression this whole pipeline exists to refuse.
    """
    if not purl.startswith("pkg:"):
        _fail(f"product identifier {purl!r} is not a PURL (must start with 'pkg:')")
    body = purl[len("pkg:") :]
    if "/" not in body:
        _fail(f"PURL {purl!r} names no package")
    ecosystem, remainder = body.split("/", 1)
    if "@" not in remainder:
        _fail(
            f"PURL {purl!r} carries no @version — a VEX statement must name the exact version "
            f"whose reachability was argued, or the suppression outlives the argument"
        )
    name, version = remainder.rsplit("@", 1)
    if not name or not version:
        _fail(f"PURL {purl!r} has an empty name or version")
    return ecosystem, name, version


def _statement_purls(statement: JsonValue, offset: int) -> list[str]:
    """Read the PURL of every product a statement applies to."""
    products = _get(statement, "products")
    if not isinstance(products, list) or not products:
        _fail(f"statements[{offset}] names no products[] — a statement must scope to something")
    purls: list[str] = []
    for product in products:
        purl = _get(_get(product, "identifiers"), "purl") or _get(product, "@id")
        if not isinstance(purl, str) or not purl:
            _fail(f"statements[{offset}] has a product with no identifiers.purl and no @id")
        purls.append(purl)
    return purls


def _vulnerability_ids(statement: JsonValue, offset: int) -> list[str]:
    """Read the advisory id plus every alias.

    Both are emitted, because the scanners disagree about which id they report: osv-scanner
    names a GHSA where pip-audit names the CVE for the same advisory, and an ignore keyed on one
    does not match the other.
    """
    vulnerability = _get(statement, "vulnerability")
    name = _get(vulnerability, "name")
    if not isinstance(name, str) or not name:
        _fail(f"statements[{offset}] carries no vulnerability.name")
    ids = [name]
    aliases = _get(vulnerability, "aliases")
    if isinstance(aliases, list):
        for alias in aliases:
            if isinstance(alias, str) and alias and alias not in ids:
                ids.append(alias)
    return ids


def _validate_document(document: JsonValue, source: str) -> list[JsonValue]:
    """Check the ledger against the OpenVEX rules that change behaviour, return statements."""
    if not isinstance(document, dict):
        _fail(f"{source} is not a JSON object")

    unknown = sorted(set(document) - _DOCUMENT_KEYS)
    if unknown:
        _fail(
            f"{source} carries key(s) the OpenVEX schema forbids "
            f"(additionalProperties: false): {', '.join(unknown)}"
        )
    missing = sorted(
        {"@context", "@id", "author", "timestamp", "version", "statements"} - set(document)
    )
    if missing:
        _fail(f"{source} is missing required OpenVEX field(s): {', '.join(missing)}")

    context = document["@context"]
    if not isinstance(context, str) or "openvex.dev/ns/" not in context:
        _fail(f"{source} @context {context!r} does not name an openvex.dev namespace")
    version = document["version"]
    # `version` is the DOCUMENT revision, an integer counting up from 1. It is not a product
    # version and it is not a semver string; bump it whenever a statement changes.
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        _fail(f"{source} version must be an integer >= 1, got {version!r}")

    statements = document["statements"]
    if not isinstance(statements, list):
        _fail(f"{source} statements is not an array")
    # Copied into a DECLARED `list[object]` rather than returned directly: narrowing a value read
    # out of `json.loads` gives a list whose element type is unknown, which ty rejects as an
    # unsound return, and every caller here treats a statement as an opaque object it re-narrows
    # anyway.
    collected: list[JsonValue] = list(statements)
    return collected


def _validate_statement(statement: JsonValue, offset: int, locked: dict[str, str]) -> None:
    """Check one statement, including its PURL versions against uv.lock."""
    if not isinstance(statement, dict):
        _fail(f"statements[{offset}] is not a JSON object")
    unknown = sorted(set(statement) - _STATEMENT_KEYS)
    if unknown:
        _fail(f"statements[{offset}] carries forbidden key(s): {', '.join(unknown)}")

    status = _get(statement, "status")
    if status not in _STATUSES:
        _fail(
            f"statements[{offset}] status {status!r} is not one of "
            f"{', '.join(sorted(_STATUSES))} — grype accepts a bogus status silently and "
            f"suppresses nothing, so it is checked here"
        )
    if status == "not_affected":
        justification = _get(statement, "justification")
        if justification not in _JUSTIFICATIONS:
            _fail(
                f"statements[{offset}] is not_affected without a valid justification "
                f"(got {justification!r}); one of {', '.join(sorted(_JUSTIFICATIONS))}"
            )
        impact = _get(statement, "impact_statement")
        if not isinstance(impact, str) or not impact.strip():
            _fail(
                f"statements[{offset}] is not_affected with no impact_statement — the "
                f"reachability argument IS the suppression; without it there is no record"
            )
        if "'''" in impact:
            _fail(
                f"statements[{offset}] impact_statement contains ''' , which would terminate "
                f"the TOML literal string this renders into"
            )

    for purl in _statement_purls(statement, offset):
        ecosystem, name, product_version = _split_purl(purl)
        if ecosystem != "pypi":
            # uv.lock resolves PyPI only. A statement about another ecosystem (the docs site's
            # npm tree, say) still has to name a version, but there is no lockfile here to
            # check it against, so it passes unasserted rather than being silently trusted.
            continue
        resolved = locked.get(name)
        if resolved is None:
            _fail(
                f"statements[{offset}] names {purl} but uv.lock resolves no package {name!r} — "
                f"the dependency is gone, so delete the statement"
            )
        if resolved != product_version:
            _fail(
                f"statements[{offset}] names {purl} but uv.lock resolves {name}=={resolved}. "
                f"The suppression was reasoned about a version this project no longer ships: "
                f"re-establish the reachability argument against {resolved}, update the PURL "
                f"and the impact_statement, bump the document version, then regenerate"
            )


def _render_entries(statements: Sequence[JsonValue], source: str) -> Iterator[str]:
    """Emit one `[[IgnoredVulns]]` block per advisory id per suppressing statement."""
    for offset, statement in enumerate(statements):
        status = _get(statement, "status")
        if status not in _SUPPRESSING_STATUSES:
            continue
        justification = _get(statement, "justification")
        impact = _get(statement, "impact_statement")
        heading = f"{status}: {justification}" if isinstance(justification, str) else str(status)
        products = ", ".join(_statement_purls(statement, offset))
        argument = impact.strip() if isinstance(impact, str) else "(no impact_statement)"
        for identifier in _vulnerability_ids(statement, offset):
            yield (
                "\n[[IgnoredVulns]]\n"
                f'id = "{identifier}"\n'
                "reason = '''\n"
                f"{heading}\n"
                "\n"
                f"Products: {products}\n"
                "\n"
                f"{argument}\n"
                "\n"
                f"Recorded in {source} (OpenVEX statements[{offset}]). Do not edit this file "
                "directly; edit the ledger and run `mise run security:vex`.\n"
                "'''\n"
            )


def render(ledger_path: Path, lock_path: Path) -> str:
    """Validate the ledger and return the `osv-scanner.toml` text it renders to."""
    source = ledger_path.as_posix()
    try:
        raw = ledger_path.read_text(encoding="utf-8")
    except OSError as error:
        _fail(f"cannot read {source}: {error.strerror or type(error).__name__}")
    try:
        document: JsonValue = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        _fail(f"{source} is not parseable JSON: {error}")

    statements = _validate_document(document, source)
    locked = _locked_versions(lock_path)
    for offset, statement in enumerate(statements):
        _validate_statement(statement, offset, locked)

    entries = list(_render_entries(statements, source))
    body = "".join(entries) if entries else _EMPTY_BODY
    return _HEADER.format(source=source, body=body)


def main(argv: Sequence[str]) -> int:
    """Entry point. Returns the process exit code; 2 means the ARGUMENTS were wrong."""
    parser = argparse.ArgumentParser(
        prog="scripts/vex_to_osv_config.py",
        description="Render osv-scanner.toml from an OpenVEX ledger.",
    )
    parser.add_argument("ledger", help="path to the OpenVEX ledger JSON")
    parser.add_argument("output", help="path to the osv-scanner.toml to write or check")
    parser.add_argument(
        "--lock", default="uv.lock", help="lockfile the PURL versions are checked against"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if the committed output differs from the render",
    )
    args = parser.parse_args(argv)

    ledger_path = Path(args.ledger)
    output_path = Path(args.output)
    rendered = render(ledger_path, Path(args.lock))

    if not args.check:
        output_path.write_text(rendered, encoding="utf-8")
        sys.stdout.write(f"vex-to-osv-config: wrote {output_path} from {ledger_path}\n")
        return 0

    try:
        committed = output_path.read_text(encoding="utf-8")
    except OSError:
        _fail(
            f"{output_path} does not exist but {ledger_path} does — run "
            f"`mise run security:vex` and commit the result"
        )
    if committed != rendered:
        diff = difflib.unified_diff(
            committed.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile=f"{output_path} (committed)",
            tofile=f"{output_path} (rendered from {ledger_path})",
        )
        sys.stderr.writelines(diff)
        _fail(
            f"{output_path} has DRIFTED from {ledger_path}. A generated suppression surface "
            f"nothing verifies is a suppression surface that outlives its reason: run "
            f"`mise run security:vex` and commit both files"
        )
    sys.stdout.write(
        f"vex-to-osv-config: {output_path} matches {ledger_path} "
        f"({len(rendered.splitlines())} lines)\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
