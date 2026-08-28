import { fileURLToPath } from "node:url"

import { satteri } from "@astrojs/markdown-satteri"
import starlight from "@astrojs/starlight"
import { defineConfig } from "astro/config"
/* A NAMED export, unlike every other Starlight plugin here: `starlight-base-path@0.2.1` publishes no
   default, so a default import is `undefined` and the config load fails with "not a function". */
import { starlightBasePath } from "starlight-base-path"
import starlightLinksValidator from "starlight-links-validator"
import starlightLlmsTxt from "starlight-llms-txt"
import starlightMdTxt from "starlight-md-txt"

import { agentNotePlugin } from "./src/lib/agent-note.js"
import { baseRawLinks } from "./src/lib/base-raw-links.js"
import { citationLinks } from "./src/lib/citation-links.js"
import { COLLECTION_ROOT, sidebar, syncedTreePaths } from "./src/lib/content-tree.js"
import mermaid, { beautifulMermaid } from "./src/lib/mermaid-integration.js"
import { PERMALINK_COMMIT, REPO_URL, SITE_BASE, SITE_ORIGIN } from "./src/lib/repo.js"

const projectRoot = fileURLToPath(new URL(".", import.meta.url))
/** The git repository the docs tree documents. `site/` is one directory inside it. */
const repoRoot = fileURLToPath(new URL("..", import.meta.url))
/** The ccu tree, read-only. `scripts/sync-ccu.mjs` copies it into the collection; nothing writes back. */
const treeRoot = fileURLToPath(new URL("../docs", import.meta.url))

/** The base with no trailing slash, which is the route prefix a citation link is built from. */
const routePrefix = SITE_BASE.replace(/\/$/, "")

const SITE_DESCRIPTION =
  "ATIF-native analytics over Claude Code agent trajectories: sessions converted to ATIF, " +
  "materialized as a corpus, queried through DuckDB views."

export default defineConfig({
  site: SITE_ORIGIN,
  /*
   * A GitHub Pages PROJECT site is served from a path segment, so `base` is load-bearing on every
   * surface. `Astro.site` EXCLUDES this value and `import.meta.env.BASE_URL` includes it; the URL
   * helpers in `src/lib/agent-surface.ts` take both rather than deriving one from the other.
   */
  base: SITE_BASE,
  markdown: {
    /*
     * `satteri()` is already Astro 7's default processor
     * (`node_modules/astro/dist/core/config/schemas/base.js:202`); it is named here so the two mdast
     * plugins below travel in the config rather than in an integration. Setting it at config load is
     * safe where replacing it later is not: every integration that registers a transform — Starlight's
     * asides, `starlight-base-path`, the mermaid integration — appends to THIS object afterwards.
     *
     * Both plugins claim a node at mdast, which is strictly before any hast pass, so nothing
     * downstream can replace the node they just returned.
     */
    processor: satteri({
      features: {
        /*
         * The mermaid integration injects a rendered SVG as HTML at mdast. Left as an opaque `raw`
         * node — the default — the whole document's rendered output comes out EMPTY: measured
         * 2026-08-28 on astro@7.2.9 with satteri@0.10.5, where the page carrying a diagram built with
         * `<div class="sl-markdown-content"></div>` and every other page was intact. `rawHtml` reparses
         * injected HTML into real hast element nodes, which the passes Starlight and Astro append after
         * this one can walk.
         */
        rawHtml: true
      },
      /*
       * FACTORIES, not instances. Sätteri resolves a factory once per compile, so plugin state is per
       * document. It has to be: the mermaid visitor is async, which makes the whole mdast pass async,
       * and Astro's glob loader renders up to ten entries concurrently
       * (`astro/dist/content/loaders/glob.js`, `pLimit(10)`). A shared instance's closure state — the
       * citation antecedent, a diagram counter — would then be reset and read across interleaved
       * documents, and a citation would resolve against another page's last path: a link that is
       * plausible, wrong, and reported by nothing.
       */
      mdastPlugins: [
        () => agentNotePlugin(),
        () =>
          citationLinks({
            commit: PERMALINK_COMMIT,
            repoUrl: REPO_URL,
            repoRoot,
            treeRoot,
            siteBase: routePrefix,
            /*
             * Publication comes from the sync manifest, not from the filesystem: the tree holds
             * hand-authored siblings the site does not publish, and a citation into one would
             * otherwise become an intra-site link to a route the build never wrote.
             */
            publishedTreePaths: syncedTreePaths(projectRoot)
          })
      ]
    })
  },
  integrations: [
    /*
     * FIRST, and it runs at `astro:build:done`. Under a non-root base the rendered tree and the raw
     * `.md` twins are link-rewritten from different inputs — the tree from the compiled page, each
     * twin from the page's Markdown source — so a link is correct on whichever surface its producer
     * touched. `starlight-base-path` fixes the tree; this fixes the twins. It is idempotent.
     */
    baseRawLinks(SITE_BASE),
    /*
     * Diagrams render at BUILD time. A client-rendered diagram is absent from the raw twin, from all
     * three llms bundles, and from any fetch that runs no JavaScript — which withholds the densest
     * thing on the page from the only audience that cannot re-derive it. The renderer throws on a
     * diagram it does not implement, and the plugin never passes the fence through as a code block.
     *
     * `bg` and `fg` are the page's own theme tokens, so ONE build-time asset tracks light and dark.
     * That only works because the SVG is inlined: a `var()` inside an `<img src="…svg">` resolves in
     * the image's document, where these properties do not exist.
     */
    mermaid({
      renderer: beautifulMermaid({
        bg: "var(--sl-color-bg)",
        fg: "var(--sl-color-text)"
      })
    }),
    starlight({
      title: "atif-sql",
      description: SITE_DESCRIPTION,
      /*
       * Derived from what the last sync actually wrote. Starlight throws on an `autogenerate`
       * directory with no files, so a hardcoded category list fails the site's build over a gap in a
       * tree the site does not own.
       */
      sidebar: sidebar(projectRoot),
      /*
       * Both overrides DELEGATE to Starlight's default and add around it. Replacing `Head` drops the
       * canonical link and the sitemap reference, and both losses are silent.
       */
      components: {
        Head: "./src/components/Head.astro",
        PageTitle: "./src/components/PageTitle.astro"
      },
      customCss: ["./src/styles/page-actions.css"],
      social: [{ icon: "github", label: "GitHub", href: REPO_URL }],
      /*
       * Only the site's own authored pages have a file to edit. Every synced page carries
       * `editUrl: false` from the stamp, because its file is a copy the next sync overwrites.
       */
      editLink: { baseUrl: `${REPO_URL}/edit/main/site/` },
      plugins: [
        /*
         * ROUTE OWNERSHIP. Exactly one dependency may own a route pattern; two producers for one path
         * is not a build error, it silently downgrades a surface an agent reads and a human does not.
         *
         *   /[...slug].md                                     starlight-md-txt
         *   /llms.txt, /llms-full.txt, /llms-small.txt         starlight-llms-txt
         *   /_llms-txt/[slug].txt                              starlight-llms-txt
         *   /sitemap-index.xml, /sitemap-0.xml                 Starlight's own sitemap integration
         *
         * A seventh plugin gets read before it gets installed: list its `injectRoute` patterns and
         * its `components` keys, and check them against this table.
         */
        starlightMdTxt(),
        starlightLlmsTxt({
          projectName: "atif-sql",
          description: SITE_DESCRIPTION,
          /*
           * `details` is the ONLY slot whose content lands before every H2: the index is an ordered
           * array of segments and both bundle links plus `optionalLinks` render after it. So the
           * entry that has to be read first goes here and nowhere else. llmstxt.org sanctions
           * exactly this content in this slot — "markdown sections of any type except headings".
           */
          details: [
            "Start here:",
            "",
            `- [For agents](${SITE_ORIGIN}${SITE_BASE}agents.md): which surface to fetch for which question, and what to assume about this repository.`,
            `- [Repository](${REPO_URL}): the source every citation on these pages links into, pinned to a commit.`,
            "",
            "Every page is also served as Markdown at its own path with `.md` appended.",
            "Fetch that instead of the HTML: it is the same text without the navigation chrome."
          ].join("\n"),
          // The agent page leads the full bundle for the same reason it leads the index.
          promote: ["agents", "index*"]
        }),
        /*
         * Lets content author a root-relative `/x/` under a path-prefixed site: it appends an mdast
         * pass that prefixes the base onto the RENDERED tree and the llms bundles. It does not reach
         * the raw twins, which never enter this processor — `baseRawLinks` above is that half.
         */
        starlightBasePath(),
        /*
         * The only agent-surface gate that runs during the build. Every page here is file-backed —
         * the sync writes real files — so the validator records headings for all of them and needs
         * no exclusion. A broken ccu cross-link therefore fails the build, which is the intended
         * loud failure: the fix belongs to the tree, and a 404 on the Markdown surface is invisible
         * to everyone except the agent that follows it.
         */
        starlightLinksValidator({
          errorOnRelativeLinks: false,
          errorOnInvalidHashes: true
        })
      ]
    })
  ],
  /*
   * Vitest reads `dist/`, and the gates enumerate built pages by walking for `index.html`. The
   * directory build format is Astro's default and is what produces one directory per route.
   */
  build: { format: "directory" },
  vite: {
    /* The content directory is generated; a watcher on it would fight the sync rather than help it. */
    server: { watch: { ignored: [`**/${COLLECTION_ROOT}/.ccu-sync.json`] } }
  }
})
