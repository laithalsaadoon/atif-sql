# SPDX-License-Identifier: Apache-2.0

"""The README's "What you can do" matrix names every command the CLI registers, and no other.

The matrix is the one place a reader meets the whole command surface, so it cannot drift from
the cyclopts app. The test walks the registered command tree (the top level and the `lake`,
`corpus` and `cron` groups), parses the README table, and checks both directions: a command
with no row, and a row naming a command the app lacks, each fail by name. Each row's link must
also land on the heading of that command in `docs/reference/cli.md`.

Anti-vacuity: a README with no matrix, and a matrix with no rows, fail rather than pass on
nothing, and the comparison itself is proved on planted drift in both directions.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import NamedTuple

import cyclopts
import pytest

from atif_cli.app import app

#: Repository root: `packages/atif-cli/tests/` -> three parents up.
ROOT = Path(__file__).resolve().parents[3]
README = ROOT / "README.md"
CLI_REFERENCE = ROOT / "docs" / "reference" / "cli.md"
SECTION = "## What you can do"
PROGRAM = "atif-sql"
LINK = re.compile(r"\[`([^`]+)`\]\(([^)]+)\)")


class Row(NamedTuple):
    """One matrix row: the command path the Command cell names, and where it links."""

    path: tuple[str, ...]
    target: str
    line: int


def subcommands(node: cyclopts.App) -> list[str]:
    """The names registered under `node`, without cyclopts' own `--help` and `--version`."""
    return [name for name in node if not name.startswith("-")]


def registered_commands(node: cyclopts.App, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    """Every runnable command path under `node`: the leaves of the command tree."""
    children = subcommands(node)
    if not children:
        return {prefix}
    found: set[tuple[str, ...]] = set()
    for name in children:
        found |= registered_commands(node[name], (*prefix, name))
    return found


def command_path(command: str, line: int) -> tuple[str, ...]:
    """The registered words a Command cell starts with, resolved against the real app.

    Words after the command (flags, placeholders, SQL) are arguments. A word that is not a
    registered name where the app expects a subcommand is a command the app lacks, and so is
    a group named without its subcommand.
    """
    words = shlex.split(command)
    if words[:1] != [PROGRAM]:
        msg = f"README line {line}: `{command}` does not start with `{PROGRAM}`"
        raise AssertionError(msg)
    node = app
    path: list[str] = []
    for word in words[1:]:
        children = subcommands(node)
        if not children:
            break
        if word not in children:
            msg = f"README line {line}: `{command}` names `{word}`, which the app lacks"
            raise AssertionError(msg)
        node = node[word]
        path.append(word)
    if subcommands(node) or not path:
        msg = f"README line {line}: `{command}` names a group or nothing, not a command"
        raise AssertionError(msg)
    return tuple(path)


def parse_matrix(text: str) -> list[Row]:
    """The matrix rows under `## What you can do`; fails when the section or its rows are absent."""
    lines = text.splitlines()
    if SECTION not in lines:
        msg = f"README has no `{SECTION}` section"
        raise AssertionError(msg)
    start = lines.index(SECTION) + 1
    rows: list[Row] = []
    header_seen = False
    for number, line in enumerate(lines[start:], start=start + 1):
        if line.startswith("## "):
            break
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split(" | ")]
        if cells[0] == "Task":
            if cells != ["Task", "Command", "Outcome"]:
                msg = f"README line {number}: the matrix columns are {cells}"
                raise AssertionError(msg)
            header_seen = True
            continue
        if set(line) <= set("|- "):
            continue
        if not header_seen or len(cells) != 3:
            msg = f"README line {number}: not a Task | Command | Outcome row"
            raise AssertionError(msg)
        link = LINK.fullmatch(cells[1])
        if link is None:
            msg = f"README line {number}: the Command cell is not one linked code span"
            raise AssertionError(msg)
        rows.append(Row(command_path(link.group(1), number), link.group(2), number))
    if not rows:
        msg = "the matrix parsed zero rows"
        raise AssertionError(msg)
    return rows


def drift(rows: list[Row], commands: set[tuple[str, ...]]) -> list[str]:
    """What differs between the matrix and the command set, one line each."""
    named = {row.path for row in rows}
    problems = [f"no matrix row for `{PROGRAM} {' '.join(p)}`" for p in sorted(commands - named)]
    problems += [
        f"row names `{PROGRAM} {' '.join(p)}`, which the app lacks"
        for p in sorted(named - commands)
    ]
    return problems


def headings(text: str) -> set[str]:
    """The anchors of the Markdown headings in `text` (GitHub slugs: lowercase, spaces to dashes)."""
    slugs: set[str] = set()
    for line in text.splitlines():
        match = re.fullmatch(r"#{1,6} (.+)", line)
        if match:
            slugs.add(
                re.sub(r"[^a-z0-9 -]", "", match.group(1).lower().replace("`", "")).replace(
                    " ", "-"
                )
            )
    return slugs


def test_the_app_registers_the_groups_and_commands_the_matrix_walks() -> None:
    commands = registered_commands(app)
    assert len(commands) >= 15
    assert ("lake", "compact") in commands
    assert ("corpus", "slim") in commands
    assert ("cron", "install") in commands
    assert ("search",) in commands


def test_the_matrix_names_every_registered_command_and_no_other() -> None:
    rows = parse_matrix(README.read_text(encoding="utf-8"))
    assert drift(rows, registered_commands(app)) == []


def test_each_row_links_to_its_command_heading_in_the_cli_reference() -> None:
    anchors = headings(CLI_REFERENCE.read_text(encoding="utf-8"))
    for row in parse_matrix(README.read_text(encoding="utf-8")):
        page, _, fragment = row.target.partition("#")
        assert page == "docs/reference/cli.md", f"README line {row.line}: links to {row.target}"
        assert fragment == "-".join(row.path), (
            f"README line {row.line}: `{fragment}` is not the anchor of {' '.join(row.path)}"
        )
        assert fragment in anchors, (
            f"README line {row.line}: docs/reference/cli.md has no #{fragment}"
        )


class TestTheComparisonIsNotVacuous:
    def test_no_section_fails(self) -> None:
        with pytest.raises(AssertionError, match="no `## What you can do` section"):
            parse_matrix("# atif-sql\n\nno matrix here\n")

    def test_zero_rows_fails(self) -> None:
        with pytest.raises(AssertionError, match="zero rows"):
            parse_matrix(
                f"{SECTION}\n\n| Task | Command | Outcome |\n| --- | --- | --- |\n\n## Next\n"
            )

    def test_a_missing_row_is_named(self) -> None:
        text = README.read_text(encoding="utf-8")
        kept = [line for line in text.splitlines() if "atif-sql lake compact`" not in line]
        problems = drift(parse_matrix("\n".join(kept)), registered_commands(app))
        assert problems == ["no matrix row for `atif-sql lake compact`"]

    def test_a_row_for_a_missing_command_is_named(self) -> None:
        text = README.read_text(encoding="utf-8")
        planted = "| Plant | [`atif-sql lake bogus`](docs/reference/cli.md#lake-bogus) | x |"
        header = "| Task | Command | Outcome |\n| --- | --- | --- |"
        with pytest.raises(AssertionError, match="`bogus`, which the app lacks"):
            parse_matrix(text.replace(SECTION, f"{SECTION}\n\n{header}\n{planted}\n", 1))

    def test_a_group_without_its_subcommand_fails(self) -> None:
        with pytest.raises(AssertionError, match="names a group or nothing"):
            command_path("atif-sql lake", 1)
