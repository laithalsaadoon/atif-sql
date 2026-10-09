# SPDX-License-Identifier: Apache-2.0

"""`scripts/docs_links.py` (`mise run docs:links`) fails on planted 404s and on nothing checked.

The built-site link gate carries the same anti-vacuity proof every gate here does. A synthetic
build shaped like `site/dist` (directory routes, raw twins, an llms index, a favicon) passes;
each defect that reached the deployed site on 2026-10-09 turns it red on its own: a relative
`.md` link a directory route resolves one level too deep, a removed favicon, the root twin left
as the dotfile `.md` the Pages artifact drops, plus a route directory deleted from the build, a
fragment naming no heading, a build with fewer pages than the content collection, and an empty
build. The live mode is proved against the same tree served over HTTP under `/atif-sql/`.

These run under `mise run test` and so `mise run check`; the gate itself runs over the real
build in `mise run docs:gate`, which needs node and a site build that `check` does not.
"""

from __future__ import annotations

import functools
import http.server
import importlib.util
import subprocess
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, override

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Repository root: `packages/atif-cli/tests/` -> three parents up.
ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "docs_links.py"
SITE = "https://docs.example/atif-sql/"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("docs_links", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["docs_links"] = module
    spec.loader.exec_module(module)
    return module


gate = _load()

_HEAD = (
    '<link rel="shortcut icon" href="/atif-sql/favicon.svg">'
    '<link rel="alternate" type="text/markdown" href="{twin}">'
)


def _page(twin: str, body: str) -> str:
    return (
        f"<!doctype html><html><head>{_HEAD.format(twin=twin)}</head>"
        f'<body><div id="_top"></div><h2 id="see-also">See also</h2>{body}'
        '<a href="https://chatgpt.com/?q=x">Open in ChatGPT</a>'
        '<a href="https://github.com/o/r/blob/abc/docs/CONTRACT.md">contract</a>'
        "</body></html>"
    )


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path]:
    """A clean build and its content collection: three pages, their twins, llms.txt, a favicon."""
    dist = tmp_path / "dist"
    content = tmp_path / "content"
    for page in ("index.md", "agents.md", "architecture/module-map.md"):
        _write(content, page, "---\ntitle: x\n---\n")
    _write(content, ".ccu-sync.json", "{}")
    _write(
        dist,
        "index.html",
        _page("/atif-sql/index.md", '<a href="/atif-sql/agents/">agents</a>'),
    )
    _write(
        dist,
        "agents/index.html",
        _page(
            "/atif-sql/agents.md",
            '<a href="/atif-sql/architecture/module-map/#see-also">map</a><a href="#_top">top</a>',
        ),
    )
    _write(
        dist,
        "architecture/module-map/index.html",
        _page("/atif-sql/architecture/module-map.md", '<a href="../../agents/">agents</a>'),
    )
    _write(dist, "index.md", "# home\n\n[agents](/atif-sql/agents.md)\n")
    _write(
        dist,
        "agents.md",
        "# agents\n\n[map](architecture/module-map.md)\n\n```\n[x](gone.md)\n```\n",
    )
    _write(dist, "architecture/module-map.md", "# map\n\n[agents](../agents.md) [top](#see-also)\n")
    _write(dist, "llms.txt", "- [Agents](https://docs.example/atif-sql/agents.md)\n")
    _write(dist, "favicon.svg", "<svg/>")
    _write(dist, "404.html", '<link rel="canonical" href="https://docs.example/atif-sql/404/">')
    _write(dist, "_astro/page.js", "")
    return dist, content


def _check(tree: tuple[Path, Path]) -> Any:
    dist, content = tree
    return gate.check_dist(dist, content, SITE)


def test_clean_build_passes(tree: tuple[Path, Path]) -> None:
    report = _check(tree)
    assert report.broken == []
    assert report.shortfalls == []
    assert report.ok
    assert len(report.pages) == 3
    # Every page's chrome, body and head, every twin and llms.txt: never a vacuous pass.
    assert report.internal_links == 14
    assert report.deep_links == 3
    assert report.external == 3


def test_relative_md_link_under_a_directory_route_fails(tree: tuple[Path, Path]) -> None:
    dist, _ = tree
    page = dist / "agents" / "index.html"
    page.write_text(
        page.read_text().replace(
            "/atif-sql/architecture/module-map/#see-also", "architecture/module-map.md"
        )
    )
    report = _check(tree)
    assert not report.ok
    assert report.broken == [
        (
            "https://docs.example/atif-sql/agents/ -> "
            "https://docs.example/atif-sql/agents/architecture/module-map.md (404)"
        )
    ]


def test_missing_favicon_fails_on_every_page(tree: tuple[Path, Path]) -> None:
    dist, _ = tree
    (dist / "favicon.svg").unlink()
    report = _check(tree)
    assert not report.ok
    assert len(report.broken) == 3
    assert all(line.endswith("/atif-sql/favicon.svg (404)") for line in report.broken)


def test_root_twin_as_a_dotfile_fails(tree: tuple[Path, Path]) -> None:
    # The Pages artifact excludes hidden files, so `<base>/.md` is a 404 however it was built.
    dist, _ = tree
    (dist / "index.md").rename(dist / ".md")
    index = dist / "index.html"
    index.write_text(index.read_text().replace("/atif-sql/index.md", "/atif-sql/.md"))
    report = _check(tree)
    assert not report.ok
    assert "https://docs.example/atif-sql/ -> https://docs.example/atif-sql/.md (404)" in (
        report.broken
    )
    assert any(
        "its raw twin https://docs.example/atif-sql/index.md" in s for s in report.shortfalls
    )


def test_deleted_route_directory_fails(tree: tuple[Path, Path]) -> None:
    dist, _ = tree
    for path in sorted((dist / "architecture" / "module-map").iterdir()):
        path.unlink()
    (dist / "architecture" / "module-map").rmdir()
    report = _check(tree)
    assert not report.ok
    assert any(
        line.endswith("-> https://docs.example/atif-sql/architecture/module-map/#see-also (404)")
        for line in report.broken
    )
    assert "the build holds 2 pages, fewer than the 3 the content collection publishes" in (
        report.shortfalls
    )


def test_fragment_naming_no_id_fails(tree: tuple[Path, Path]) -> None:
    dist, _ = tree
    page = dist / "agents" / "index.html"
    page.write_text(page.read_text().replace("#see-also", "#no-such-heading"))
    report = _check(tree)
    assert report.broken == [
        (
            "https://docs.example/atif-sql/agents/ -> "
            "https://docs.example/atif-sql/architecture/module-map/#no-such-heading "
            "(no id 'no-such-heading')"
        )
    ]


def test_broken_twin_link_fails(tree: tuple[Path, Path]) -> None:
    dist, _ = tree
    _write(dist, "agents.md", "# agents\n\n[contract](CONTRACT.md)\n")
    report = _check(tree)
    assert report.broken == [
        "https://docs.example/atif-sql/agents.md -> https://docs.example/atif-sql/CONTRACT.md (404)"
    ]


def test_link_outside_the_base_on_the_site_origin_fails(tree: tuple[Path, Path]) -> None:
    # A root-relative link that lost the base names the origin's root, a different site.
    dist, _ = tree
    _write(dist, "llms.txt", "- [Agents](/agents.md)\n")
    report = _check(tree)
    assert report.broken == [
        "https://docs.example/atif-sql/llms.txt -> https://docs.example/agents.md (404)"
    ]


def test_empty_build_fails(tmp_path: Path, tree: tuple[Path, Path]) -> None:
    _, content = tree
    empty = tmp_path / "empty"
    empty.mkdir()
    report = gate.check_dist(empty, content, SITE)
    assert not report.ok
    assert report.pages == []
    assert "the build holds 0 pages, fewer than the 3 the content collection publishes" in (
        report.shortfalls
    )


def test_empty_content_collection_fails(tmp_path: Path, tree: tuple[Path, Path]) -> None:
    dist, _ = tree
    report = gate.check_dist(dist, tmp_path / "no-content", SITE)
    assert not report.ok
    assert report.shortfalls == [
        f"{tmp_path / 'no-content'} holds no pages: the content sync did not run"
    ]


def test_pages_without_internal_links_fail(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    content = tmp_path / "content"
    _write(content, "index.md", "")
    _write(dist, "index.html", "<html><body>no links</body></html>")
    _write(dist, "index.md", "# home\n")
    report = gate.check_dist(dist, content, SITE)
    assert report.broken == []
    assert report.internal_links == 0
    assert not report.ok


def test_markdown_links_skip_fences_and_bare_fragments() -> None:
    text = "[a](x.md) [b](#frag)\n```\n[c](y.md)\n```\n[d]: z.md\n~~~~\n[e](w.md)\n~~~~\n"
    assert gate.markdown_links(text) == ["x.md", "z.md"]


def test_cli_exits_one_on_a_broken_build_and_zero_on_a_clean_one(tree: tuple[Path, Path]) -> None:
    dist, content = tree

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed interpreter and repository script
            [
                sys.executable,
                str(SCRIPT),
                "dist",
                *("--dist", str(dist), "--content", str(content), "--site", SITE),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    clean = run()
    assert clean.returncode == 0, clean.stdout + clean.stderr
    assert "docs:links: 3 pages," in clean.stdout
    assert ", 0 broken;" in clean.stdout
    (dist / "favicon.svg").unlink()
    broken = run()
    assert broken.returncode == 1
    assert ", 3 broken;" in broken.stdout


def test_cli_rejects_an_unknown_mode() -> None:
    result = subprocess.run(  # noqa: S603 - fixed interpreter and repository script
        [sys.executable, str(SCRIPT), "crawl"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2


def test_site_url_is_read_from_repo_ts() -> None:
    assert gate.site_url() == "https://laithalsaadoon.github.io/atif-sql/"


@pytest.fixture
def served(tmp_path: Path, tree: tuple[Path, Path]) -> Iterator[str]:
    """The synthetic build served over HTTP under `/atif-sql/`, as Pages serves the artifact."""
    dist, _ = tree
    (tmp_path / "www").mkdir()
    (tmp_path / "www" / "atif-sql").symlink_to(dist, target_is_directory=True)
    sitemap = (
        "<urlset>"
        + "".join(
            f"<url><loc>{SITE}{path}</loc></url>"
            for path in ("", "agents/", "architecture/module-map/")
        )
        + "</urlset>"
    )
    _write(dist, "sitemap-0.xml", sitemap)
    _write(
        dist,
        "sitemap-index.xml",
        f"<sitemapindex><sitemap><loc>{SITE}sitemap-0.xml</loc></sitemap></sitemapindex>",
    )

    class Quiet(http.server.SimpleHTTPRequestHandler):
        @override
        def log_message(self, format: str, *args: object) -> None:
            return

    handler = functools.partial(Quiet, directory=str(tmp_path / "www"))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/atif-sql/"
    finally:
        server.shutdown()
        server.server_close()


def test_live_crawl_passes_then_fails_on_a_missing_favicon(
    served: str, tree: tuple[Path, Path]
) -> None:
    fetcher = gate.LiveFetcher(retries=1, timeout=5.0, pause=0.0)
    clean = gate.check_live(served, fetcher, 3, SITE)
    assert clean.broken == []
    assert clean.ok
    assert len(clean.pages) == 3
    dist, _ = tree
    (dist / "favicon.svg").unlink()
    broken = gate.check_live(served, fetcher, 3, SITE)
    assert not broken.ok
    assert len(broken.broken) == 3


def test_live_crawl_below_its_page_floor_fails(served: str) -> None:
    fetcher = gate.LiveFetcher(retries=1, timeout=5.0, pause=0.0)
    report = gate.check_live(served, fetcher, 4, SITE)
    assert report.shortfalls == ["the sitemap lists 3 pages, below the floor of 4"]
    assert not report.ok
