import { docsLoader } from "@astrojs/starlight/loaders"
import { docsSchema } from "@astrojs/starlight/schema"
import { defineCollection } from "astro:content"

/**
 * The docs collection: Starlight's own file loader over `src/content/docs`.
 *
 * A file loader rather than a wrapped one, and that is the consequential choice. The ccu tree is
 * published by copying it into this directory and stamping frontmatter on the copy
 * (`scripts/sync-ccu.mjs`), so every page here is file-backed. That buys two things a loader-injected
 * tier does not have: `starlight-links-validator` records each page's headings during its own pass and
 * so can judge links into the tree, and every markdown visitor receives a real `ctx.fileURL` instead
 * of `undefined` — which is what makes a Mermaid fence render on a generated page as well as an
 * authored one.
 *
 * `docsSchema()` requires `title` with no H1 fallback, and ccu forbids frontmatter on its outputs and
 * strips any found there. So the title is stamped onto the copy and the tree is never mutated.
 */
export const collections = {
  docs: defineCollection({ loader: docsLoader(), schema: docsSchema() })
}
