import { defineConfig } from "vitest/config"

/**
 * The gates read `dist/`, so they are node-environment tests with no browser and no Astro runtime.
 *
 * `dist/` has to exist before they run: `pnpm gate` builds first, and every `readDist` throws with
 * that instruction rather than skipping. A gate that silently no-ops without a build is worse than an
 * absent one, because the run is green.
 */
export default defineConfig({
  test: {
    environment: "node",
    include: ["tests/**/*.test.ts"],
    /* Each gate walks `dist/` independently; a shared pool buys nothing and hides which file failed. */
    passWithNoTests: false
  }
})
