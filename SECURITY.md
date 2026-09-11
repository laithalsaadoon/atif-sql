# Security policy

## Supported version

`0.1.0` — every workspace member is at `0.1.0` and the project is pre-1.0.
Fixes land on `main`; there are no backports and no patch branches.

## Reporting a vulnerability

Report privately through GitHub, not in a public issue:
**<https://github.com/laithalsaadoon/atif-sql/security/advisories/new>**
(the repository's Security tab → "Report a vulnerability").

Include the version or commit, the platform, and the smallest reproduction you
have. This is a personal open-source project with no on-call rotation, so no
response time is promised. Expect an acknowledgement and a fix or an explicit
"won't fix" when someone gets to it — and please keep the report private until
a fix ships or you hear that one is not coming.

## What the security-relevant surface actually is

atif-sql is a local analytics tool. It reads the operator's own Claude Code
transcripts, writes derived artifacts to the operator's own disk, and exposes
DuckDB over them. It listens on no port, has no users other than the person
running it, and grants no privilege that person did not already have. Two
properties are still worth stating plainly.

**1. Transcripts are sensitive, and so is everything derived from them.**
Sources default to `${CLAUDE_CONFIG_DIR:-~/.claude}/projects` and materialized
artifacts to `~/.atif-sql/corpus/<slug>`. Agent transcripts routinely contain
whatever passed through a session: pasted credentials, tokens in command output,
environment dumps, private source, customer data. atif-sql copies that content
into the corpus, the DuckDB views, and (with `embed`) the vector store. It does
not scan for or redact secrets. Treat the corpus root, the Lance store, and any
query output you export as exactly as sensitive as the raw transcripts — same
disk encryption, same backup policy, same care before pasting a query result
into a chat or an issue.

**2. `analyze` and `embed` send transcript text to Amazon Bedrock.**
`atif-sql analyze` sends session content to the configured Bedrock models for
the LLM pipelines; `atif-sql embed` sends step text to Cohere Embed v4 on
Bedrock. Both bill your AWS account, and both are guarded so that the network
call is a deliberate act: `analyze` is dry-run unless you pass `--no-dry-run`,
and `embed` exits 64 on an unscoped real run (no `--limit`, no `--all`).
`atif-sql search` also reaches Bedrock, but sends only the query string you
type — it embeds that one string, then runs the kNN locally.

Every other command — `convert`, `materialize`, `status`, `query`, `examples`,
`schema`, `cron` — is local-only.

If your transcripts may not be sent to a third-party model provider, do not run
`analyze` or `embed`. The rest of the tool remains fully usable without them.

**Not a privilege boundary.** `atif-sql query` executes the SQL you hand it, and
DuckDB can read and write local files. The tool assumes the caller already owns
the shell and the data; do not treat it as a sandbox, and do not wire it behind
an interface that lets an untrusted party choose the SQL, the corpus root, or
the environment.
