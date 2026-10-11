## v0.2.0 (2026-10-11)

### BREAKING CHANGE

- the message_trajectory, message_clusters, cluster_terms,
session_communities and community_profile views and the autonomy_trend,
success_rate_by_work, sentiment_arc, cluster_top_terms and
community_top_topics macros are gone; session_classifications drops the
autonomy and success_outcome aliases.
- the six sibling distributions are no longer published, and
`packages/atif-cli` is named `atif-cli` again — `atif-sql` is the root's name.
Nothing was ever released under either shape, so no installed version changes.

### Feat

- **release**: sign, attest and attach the release assets with SBOMs
- **embed**: EmbeddingGemma 2 text-only local provider
- **gates**: retire the spaced em dash in the docs and make Google.EmDash an error
- **converter**: emit Codex exec scripts' nested MCP calls and commands as tool calls
- **gates**: crawl the built and the deployed docs site for internal 404s
- **gates**: ratchet the Vale warnings per rule against .vale-baseline.json
- **gates**: give the Vale prose gate a file floor per glob and gate CONTRIBUTING.md
- **gates**: add the Vale prose gate over the published docs
- **corpus**: store session JSON compressed, drop per-session parquet, add corpus slim
- analyze reads session data from the lake
- **lake**: an explicit memory budget for compact, used by the nightly lane
- **cron**: a nightly compact lane for the lake
- **embed**: discover from the lake, prune orphans, search the lake
- **duck**: query every corpus through one DuckLake
- **converter**: harbor 0.23.0 and litellm v1.102.0; price every Claude Code step
- human-authored turns only, deterministic session outcomes, cut the trajectory and structural pipelines (#18)
- images by reference, typed tool outcomes, subagent links (#16)
- session_events artifact, NULL-not-$0 pricing, reported cost, honest loss gaps (#15)
- **corpus**: keep every session, archive raw sources, re-convert on schema change (#17)
- Codex CLI transcript support end to end (#5)
- **hooks**: refuse any push of a local-only/* ref
- atif-sql — ATIF-native analytics over Claude Code agent trajectories

### Fix

- **converter**: guard every string-set membership test on transcript fields
- **converter**: reject non-string uuids and payload types instead of crashing
- **security**: render the OpenVEX suppressions beside the docs site lockfile too
- **docs**: resolve relative .md links, publish the favicon and serve the root twin at index.md
- **converter**: store the bytes of every Codex tool output image the port names
- **converter**: hold parity with harbor 0.24.0's converters (#41)
- **analyze**: re-read sessions that moved, read ahead only lake rows, survive a rebuild
- **query**: count reclaimable page cache as cgroup headroom
- **converter**: keep agent.extra lists in first-seen order
- **duck**: create the lake writer lock owner-only
- **duck**: merge and rewrite lake files on one thread
- **converter**: is_error and exit_code for Codex exec scripts
- **converter**: archive a parsed sidecar through the verifying read only
- **refresh**: hard per-lane memory cap for the cron lanes (+ two test time bombs) (#14)
- **query**: size registration to the host, private spill dir, no root, no extension installs at query time (#10)
- **sql**: bind paths as parameters, validate session ids at the boundary, prove constant-only SQL text (#8)
- **ci**: stop caching the scanner toolchain
- **ci**: lock the node toolchain so a --locked install can resolve it

### Refactor

- **converter**: vendor the ATIF models and price data; harbor and litellm become dev-only (#19)
- **converter**: own the ATIF conversion; harbor becomes a public-API dependency (#6)
- publish one bundled distribution instead of seven

### Perf

- **converter**: read each session once for the converter and the audit (#9)
- 4.0x on the five-task panel via a certified frontier climb (#7)
