# atif-cli

cyclopts CLI composing the atif-sql workspace — the composition root, and the
only package allowed to import atif-converter, atif-corpus, atif-duck,
atif-embed, and atif-analytics together (import-linter independence
contract).

Commands (`docs/CONTRACT.md` §CLI plus the wave-2 additions in
`docs/CONTRACT-V2.md`):

```
atif-sql convert <session.jsonl>       # one-shot convert+audit (+edges.jsonl
                                       #   next to --trajectory-out)
atif-sql materialize [--force] [--quiesce-seconds N]
                     [--source-root P] [--corpus-root P]
                     [--sessions id,id]   # contract-compatible extension:
                                          #   plan only the named sessions
atif-sql status                        # corpus freshness, read-only
atif-sql query 'SQL' [--format auto|json|csv]
atif-sql schema                        # static catalog, no duckdb bind, <50ms
atif-sql examples                      # tested example queries per view/macro
                                       #   (alias: atif-sql query --examples)
atif-sql analyze                       # v2 analytics pipelines; dry-run by
                                       #   default, spends only with --no-dry-run
atif-sql embed --all --no-dry-run      # embed corpus steps into LanceDB
                                       #   (a real run needs --all or --limit N)
atif-sql search '<text>'               # embed the text, then semantic kNN
atif-sql cron install | status         # print the refresh crontab block, or
                                       #   report lane locks + last runs
```

Conventions: `--format auto` resolves to a table on a TTY and JSON on a pipe;
DuckDB errors classify to stable exit codes (64 parse / 65 catalog / 70
runtime) with JSON error envelopes on non-TTY; heavy imports (duckdb, harbor,
boto3) are deferred into command bodies (pinned by the lean-import test).
