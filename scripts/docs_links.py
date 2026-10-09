#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Crawl the docs site and fail on any internal link that does not answer, built or deployed.

    scripts/docs_links.py dist [--dist DIR] [--content DIR] [--label LABEL]
    scripts/docs_links.py live [--root URL] [--min-pages N] [--retries N] [--label LABEL]

`dist` (`mise run docs:links`, inside `docs:gate`) serves `site/dist` the way GitHub Pages
serves the artifact `actions/upload-pages-artifact` makes of it, from disk and offline. `live`
(`mise run docs:links:live`, the `live-links` job in `.github/workflows/docs.yml`) fetches the
deployed site over HTTP. Both crawl the same way: start from every page, follow every internal
`href` and `src` an HTML document carries (the body, the chrome and the head) and every link
target a Markdown or text document carries (the raw `.md` twins and the llms.txt trio), resolve
each one as a browser does against the URL its document is served at, and fetch it once. A link
is internal when it lands on the site's origin, whether under the base or not.

Why this exists: on 2026-10-09 the deployed site at 36dc203 answered 404 on 76 internal
targets over its 20 pages (75 relative `.md` links that a directory route resolved one level too
deep, the root twin `<base>/.md`, and `<base>/favicon.svg` on every page) while the links
validator said "All internal links are valid" and lychee over the source Markdown was green.
Both read the inputs; this reads what a reader is served.

What fails the run, each a way the site can 404 or the check can pass on nothing:

- a broken internal target    any internal URL that does not answer 200: a missing file, a
                              route directory with no `index.html`, or a hidden path (a
                              segment starting with `.`), which the Pages artifact leaves
                              out (`tar --exclude=.[^/]*`)
- a missing fragment          `#id` on an internal HTML target that carries no such `id`
- no pages, no links          a crawl that found 0 pages or 0 internal links checked nothing
- fewer pages than published  `dist`: every page in the content collection
                              (`site/src/content/docs/**/*.md`, what the sync and the authored
                              pages wrote) must have its route `index.html` and its raw twin in
                              the build; `live`: the sitemap must list at least `--min-pages`

External links are not fetched: `.github/workflows/links.yml` runs lychee over them. The
"Open in" deep links (DEEP_LINK_HOSTS) are named on their own because their query carries the
page's own URL, and they answer 403 to a crawler while working in a browser.

stdlib only, 3.9-compatible, run with `python3` like the other gate helpers here, so it works
before `uv sync` and on a bare CI runner.

Exit 0 when every internal target answered, 1 when one did not or the crawl checked nothing,
2 on a usage error.
"""

from __future__ import annotations

import re
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, Protocol
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

ROOT = Path(__file__).resolve().parent.parent
REPO_TS = ROOT / "site" / "src" / "lib" / "repo.ts"
DIST = ROOT / "site" / "dist"
CONTENT = ROOT / "site" / "src" / "content" / "docs"

#: The "Open in ChatGPT / Claude / Claude Code / Cursor" targets (`site/src/lib/agent-surface.ts`).
#: Never fetched: each answers 403 to a crawler and works in a browser, so a check over them
#: reports the vendor's bot policy, not this site.
DEEP_LINK_HOSTS = frozenset({"chatgpt.com", "claude.ai", "cursor.com"})

#: Build output that holds assets rather than pages: no `index.html` under these is a page.
ASSET_DIRS = frozenset({"_astro", "pagefind"})

#: The attributes that name a URL, per tag. `srcset` is left out: Starlight emits none.
URL_ATTRIBUTES: dict[str, tuple[str, ...]] = {
    "a": ("href",),
    "area": ("href",),
    "link": ("href",),
    "img": ("src",),
    "script": ("src",),
    "source": ("src",),
    "iframe": ("src",),
    "video": ("src", "poster"),
    "audio": ("src",),
}

#: `](target)` and `]: target`, the two forms Markdown writes a link target in.
_INLINE = re.compile(r"\]\(<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\)")
_DEFINITION = re.compile(r"^ {0,3}\[[^\]]+\]:[ \t]+<?(\S+?)>?(?:\s|$)")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_SITE_ORIGIN = re.compile(r'export const SITE_ORIGIN = "([^"]+)"')
_SITE_BASE = re.compile(r'export const SITE_BASE = "([^"]+)"')
_SITEMAP_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")

#: The one status a target may answer with.
HTTP_OK = 200

_USAGE = (
    "usage: scripts/docs_links.py dist [--dist DIR] [--content DIR] [--label LABEL]\n"
    "       scripts/docs_links.py live [--root URL] [--min-pages N] [--retries N] [--label LABEL]"
)


def _die(label: str, reason: str, code: int = 2) -> NoReturn:
    sys.stderr.write(f"{label}: {reason}\n")
    sys.exit(code)


def site_url(repo_ts: Path = REPO_TS) -> str:
    """The deployed site, `SITE_ORIGIN` plus `SITE_BASE` from `repo.ts`, the one place both live."""
    text = repo_ts.read_text(encoding="utf-8")
    origin = _SITE_ORIGIN.search(text)
    base = _SITE_BASE.search(text)
    if origin is None or base is None:
        msg = f"{repo_ts} names no SITE_ORIGIN or SITE_BASE"
        raise ValueError(msg)
    return origin.group(1).rstrip("/") + "/" + base.group(1).strip("/") + "/"


@dataclass
class Response:
    """What one fetch answered: the HTTP status, and the body when it answered 200."""

    status: int
    body: bytes = b""
    content_type: str = ""


class Fetcher(Protocol):
    """Answers a GET for an absolute URL on the site."""

    def get(self, url: str) -> Response:
        """Fetch url."""
        ...


def _content_type(path: str) -> str:
    if path.endswith((".html", "/")):
        return "text/html"
    if path.endswith(".md"):
        return "text/markdown"
    if path.endswith(".txt"):
        return "text/plain"
    if path.endswith(".xml"):
        return "application/xml"
    return "application/octet-stream"


@dataclass
class DistFetcher:
    """`site/dist` served the way GitHub Pages serves the artifact made of it."""

    dist: Path
    site: str

    def file_for(self, url: str) -> Path | None:
        """The file Pages would answer url with, or None for a 404."""
        parts = urlsplit(url)
        base = urlsplit(self.site)
        if (parts.scheme, parts.netloc) != (base.scheme, base.netloc):
            return None
        if not parts.path.startswith(base.path):
            return None
        relative = unquote(parts.path[len(base.path) :])
        segments = [s for s in relative.split("/") if s]
        # The artifact leaves out every hidden file and directory (`--exclude=.[^/]*`).
        if any(s.startswith(".") or s == ".." for s in segments):
            return None
        target = self.dist.joinpath(*segments)
        if relative == "" or relative.endswith("/"):
            target /= "index.html"
        elif target.is_dir():
            # Pages answers `/x` with a redirect to `/x/` when `x/index.html` exists.
            target /= "index.html"
        return target if target.is_file() else None

    def get(self, url: str) -> Response:
        """Read the file Pages would serve for url."""
        path = self.file_for(url)
        if path is None:
            return Response(404)
        return Response(200, path.read_bytes(), _content_type(path.name))


@dataclass
class LiveFetcher:
    """The deployed site over HTTP, retrying a non-200 to ride out a CDN that is still catching up."""

    retries: int = 3
    timeout: float = 30.0
    pause: float = 2.0

    def get(self, url: str) -> Response:
        """GET url, following redirects, retrying anything but a 200."""
        last = Response(0)
        for attempt in range(max(1, self.retries)):
            if attempt:
                time.sleep(self.pause * attempt)
            request = urllib.request.Request(  # noqa: S310 - the caller passes the site's own https URL
                url, headers={"User-Agent": "atif-sql-docs-links/1 (+scripts/docs_links.py)"}
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as answer:  # noqa: S310
                    body = answer.read()
                    kind = answer.headers.get_content_type()
                    return Response(int(answer.status), body, kind)
            except urllib.error.HTTPError as error:
                last = Response(int(error.code))
            except (urllib.error.URLError, TimeoutError, OSError):
                last = Response(0)
        return last


class _HtmlLinks(HTMLParser):
    """The URLs and the ids of one HTML document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value for name, value in attrs if value is not None}
        if "id" in values:
            self.ids.add(values["id"])
        if tag == "a" and "name" in values:
            self.ids.add(values["name"])
        for attribute in URL_ATTRIBUTES.get(tag, ()):
            value = values.get(attribute, "").strip()
            if value:
                self.urls.append(value)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def html_links(text: str) -> tuple[list[str], set[str]]:
    """Every URL attribute of an HTML document, in order, and the ids it carries."""
    parser = _HtmlLinks()
    parser.feed(text)
    parser.close()
    return parser.urls, parser.ids


def markdown_links(text: str) -> list[str]:
    """Every link target of a Markdown or text document, outside fenced code.

    A bare fragment is left out: in a bundle that concatenates pages (llms-full.txt) it names a
    heading of a page the bundle no longer separates, so it resolves against nothing.
    """
    found: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        marker = _FENCE.match(line)
        if fence is not None:
            if marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
                fence = None
            continue
        if marker:
            fence = marker.group(1)
            continue
        found.extend(m.group(1) for m in _INLINE.finditer(line))
        definition = _DEFINITION.match(line)
        if definition:
            found.append(definition.group(1))
    return [url for url in found if not url.startswith("#")]


@dataclass
class Report:
    """What one crawl found."""

    pages: list[str] = field(default_factory=list)
    documents: int = 0
    internal_links: int = 0
    targets: int = 0
    external: int = 0
    deep_links: int = 0
    broken: list[str] = field(default_factory=list)
    shortfalls: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether the crawl checked something and found nothing broken."""
        return (
            bool(self.pages) and self.internal_links > 0 and not self.broken and not self.shortfalls
        )


def _is_parsed(url: str, response: Response) -> str | None:
    path = urlsplit(url).path
    kind = response.content_type
    if kind == "text/html" or path.endswith(("/", ".html")):
        return "html"
    if kind in {"text/markdown", "text/plain"} or path.endswith((".md", ".txt")):
        return "markdown"
    return None


def crawl(seeds: Iterable[str], site: str, fetcher: Fetcher, alias: str | None = None) -> Report:
    """Fetch every seed and every internal target they link to, transitively; report what broke.

    site is where the crawl runs. alias, when given, is the canonical site the pages name in their
    absolute URLs (canonical, alternate, sitemap); a URL under it is read as the same path under
    site, so a crawl of a local server holds the absolute links to account too.
    """

    def local(url: str) -> str:
        return site + url[len(alias) :] if alias and url.startswith(alias) else url

    report = Report(pages=list(dict.fromkeys(local(seed) for seed in seeds)))
    origin = urlsplit(site)
    base_path = origin.path
    responses: dict[str, Response] = {}
    ids: dict[str, set[str]] = {}
    links_from: dict[str, list[str]] = {}
    queue: deque[str] = deque(report.pages)
    seen: set[str] = set(report.pages)

    def fetch(url: str) -> Response:
        if url not in responses:
            responses[url] = fetcher.get(url)
        return responses[url]

    while queue:
        document = queue.popleft()
        response = fetch(document)
        if response.status != HTTP_OK:
            continue
        kind = _is_parsed(document, response)
        if kind is None:
            continue
        report.documents += 1
        text = response.body.decode("utf-8", errors="replace")
        if kind == "html":
            urls, found_ids = html_links(text)
            ids[document] = found_ids
        else:
            urls = markdown_links(text)
        resolved: list[str] = []
        for raw in urls:
            absolute = local(urljoin(document, raw))
            parts = urlsplit(absolute)
            if parts.scheme not in {"http", "https"}:
                continue
            if parts.hostname in DEEP_LINK_HOSTS:
                report.deep_links += 1
                continue
            if (parts.scheme, parts.netloc) != (origin.scheme, origin.netloc):
                report.external += 1
                continue
            report.internal_links += 1
            resolved.append(absolute)
            target, _fragment = urldefrag(absolute)
            on_base = urlsplit(target).path.startswith(base_path)
            if target not in seen and on_base:
                seen.add(target)
                queue.append(target)
        links_from[document] = resolved

    distinct: set[str] = set()
    for document, urls in links_from.items():
        for absolute in urls:
            target, fragment = urldefrag(absolute)
            distinct.add(absolute)
            response = fetch(target)
            if response.status != HTTP_OK:
                report.broken.append(f"{document} -> {absolute} ({response.status})")
                continue
            if fragment and target in ids and unquote(fragment) not in ids[target]:
                report.broken.append(f"{document} -> {absolute} (no id {unquote(fragment)!r})")
    for page in report.pages:
        status = fetch(page).status
        if status != HTTP_OK:
            report.broken.append(f"page {page} ({status})")
    report.targets = len(distinct)
    report.broken = sorted(set(report.broken))
    return report


def _slug(collection_path: str) -> str | None:
    """A page's route slug the way Astro's glob loader derives it, or None off its character set."""
    segments = collection_path[: -len(".md")].split("/")
    if not all(_SEGMENT.match(s) for s in segments):
        return None
    slug = "/".join(s.lower() for s in segments)
    return "" if slug == "index" else re.sub(r"/index$", "", slug)


def content_pages(content: Path) -> list[str]:
    """Every published page in the content collection, collection-relative, hidden paths left out."""
    if not content.is_dir():
        return []
    return sorted(
        path.relative_to(content).as_posix()
        for path in content.rglob("*.md")
        if not any(part.startswith(".") for part in path.relative_to(content).parts)
    )


def dist_seeds(dist: Path, site: str) -> list[str]:
    """Every page, twin and bundle the build wrote, as the URLs Pages would serve them at."""
    seeds: list[str] = []
    for path in sorted(dist.rglob("*")):
        relative = path.relative_to(dist)
        parts = relative.parts
        if not path.is_file() or parts[0] in ASSET_DIRS:
            continue
        if any(part.startswith(".") for part in parts):
            continue
        posix = relative.as_posix()
        if path.name == "index.html":
            seeds.append(site + posix[: -len("index.html")])
        elif path.suffix in {".md", ".txt"}:
            # `404.html` is no seed. Pages serves it at whatever URL was missing, so it has no
            # address of its own to resolve against, and Starlight gives it a canonical of
            # `<base>/404/`, a route nobody links to and the sitemap leaves out.
            seeds.append(site + posix)
    return seeds


def check_dist(dist: Path, content: Path, site: str) -> Report:
    """Crawl the build as Pages would serve it, and hold it to the content collection's page count."""
    fetcher = DistFetcher(dist, site)
    seeds = dist_seeds(dist, site) if dist.is_dir() else []
    pages = [s for s in seeds if s.endswith("/")]
    report = crawl(seeds, site, fetcher)
    report.pages = pages
    published = content_pages(content)
    if not published:
        report.shortfalls.append(f"{content} holds no pages: the content sync did not run")
    for page in published:
        slug = _slug(page)
        if slug is None:
            report.shortfalls.append(f"{page} has no route slug the gate can derive")
            continue
        route = site + (slug + "/" if slug else "")
        twin = site + (slug or "index") + ".md"
        for url, what in ((route, "page"), (twin, "raw twin")):
            if fetcher.file_for(url) is None:
                report.shortfalls.append(f"{page}: its {what} {url} is not in the build")
    if len(pages) < len(published):
        report.shortfalls.append(
            f"the build holds {len(pages)} pages, fewer than the {len(published)} the content "
            "collection publishes"
        )
    return report


def sitemap_pages(root: str, fetcher: Fetcher, alias: str | None = None) -> list[str]:
    """Every page the deployed sitemap lists, through `sitemap-index.xml`."""
    index = fetcher.get(urljoin(root, "sitemap-index.xml"))
    if index.status != HTTP_OK:
        return []
    pages: list[str] = []
    for listed in _SITEMAP_LOC.findall(index.body.decode("utf-8", errors="replace")):
        sitemap = root + listed[len(alias) :] if alias and listed.startswith(alias) else listed
        answer = fetcher.get(sitemap)
        if answer.status == HTTP_OK:
            pages.extend(_SITEMAP_LOC.findall(answer.body.decode("utf-8", errors="replace")))
    return list(dict.fromkeys(pages))


def check_live(root: str, fetcher: Fetcher, min_pages: int, site: str | None = None) -> Report:
    """Crawl the deployed site from its sitemap; site is the canonical URL when root is a copy."""
    alias = site if site is not None and site != root else None
    pages = sitemap_pages(root, fetcher, alias)
    report = crawl(pages, root, fetcher, alias)
    if len(pages) < min_pages:
        report.shortfalls.append(
            f"the sitemap lists {len(pages)} pages, below the floor of {min_pages}"
        )
    return report


def _print(label: str, report: Report) -> None:
    out = sys.stdout
    for line in report.broken:
        out.write(f"broken  {line}\n")
    for line in report.shortfalls:
        out.write(f"short   {line}\n")
    if not report.pages:
        out.write("short   found 0 pages: the crawl checked nothing\n")
    elif report.internal_links == 0:
        out.write("short   found 0 internal links: the crawl checked nothing\n")
    out.write(
        f"{label}: {len(report.pages)} pages, {report.documents} documents read, "
        f"{report.internal_links} internal links to {report.targets} distinct targets, "
        f"{len(report.broken)} broken; {report.external} external and {report.deep_links} "
        "deep links not fetched\n"
    )


def _parse(argv: Sequence[str]) -> tuple[str, dict[str, str]]:
    if not argv or argv[0] not in {"dist", "live"}:
        _die("docs:links", _USAGE)
    mode = argv[0]
    allowed = {
        "dist": {"--dist", "--content", "--label", "--site"},
        "live": {"--root", "--min-pages", "--retries", "--label", "--site"},
    }[mode]
    options: dict[str, str] = {}
    it = iter(argv[1:])
    for arg in it:
        if arg not in allowed:
            _die("docs:links", _USAGE)
        value = next(it, None)
        if value is None:
            _die("docs:links", f"{arg} needs a value\n{_USAGE}")
        options[arg] = value
    return mode, options


def main(argv: Sequence[str]) -> int:
    """Run one crawl; return the exit status the module docstring defines."""
    mode, options = _parse(argv)
    label = options.get("--label", "docs:links" if mode == "dist" else "docs:links:live")
    try:
        site = options.get("--site") or site_url()
    except (OSError, ValueError) as error:
        _die(label, str(error))
    if not site.endswith("/"):
        site += "/"
    if mode == "dist":
        report = check_dist(
            Path(options.get("--dist", str(DIST))),
            Path(options.get("--content", str(CONTENT))),
            site,
        )
    else:
        root = options.get("--root", site)
        if not root.endswith("/"):
            root += "/"
        try:
            min_pages = int(options.get("--min-pages", "1"))
            retries = int(options.get("--retries", "3"))
        except ValueError:
            _die(label, _USAGE)
        report = check_live(root, LiveFetcher(retries=retries), max(1, min_pages), site)
    _print(label, report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
