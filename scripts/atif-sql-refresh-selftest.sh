#!/usr/bin/env bash
# Guard for scripts/atif-sql-refresh.sh: assert its lane invocations still match
# the atif-sql CLI, that the cheap paths really are cheap, and that the
# analytics-absent guard actually fires.
#
# What it asserts:
#
#   (a) every flag a lane passes is accepted by the CURRENT CLI's --help — or,
#       for a subcommand the resolved CLI may not carry (`analyze`, `embed`),
#       the documented absence guard covers the lane;
#   (b) `--no-dry-run` appears exactly once in comment-stripped executable
#       code (CONTRACT-V2 §Cost guards: only the llm lane spends);
#   (c) every corpus config dir this host is configured for resolves and stats
#       — RESOLVED the way the refresh script resolves it, never grepped as a
#       literal, because a typo'd path must fail here rather than become a
#       silent no-op tick;
#   (d) single-flight is nonblocking AND per lane (the lock name carries
#       $MODE — one shared lock lets a slow lane starve the paid one);
#   (e) the analytics-absent guard REALLY exits 0: run the refresh script
#       against a PATH-shim atif-sql whose --help lacks `analyze`, then again
#       against one that has it, and assert both behaviors. A guard that was
#       never seen firing is hope, not a guard;
#   (g) the Codex pass's own guards fire AND stand down: skipped against a CLI
#       with no `--agent`, run against one that has it, and refused when
#       ATIF_SQL_CORPUS_ROOT is pinned to the Claude corpus.
#
# Run by hand after an atif-sql upgrade, or wire into a nightly.
# Exits the FAILURE COUNT (0 = green).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
SCRIPT="$SCRIPT_DIR/atif-sql-refresh.sh"

# Same resolution order as the refresh script itself.
if [ -n "${ATIF_SQL_CLI:-}" ]; then
  ATIF_SQL="$ATIF_SQL_CLI"
elif [ -x "$HOME/.local/bin/atif-sql" ]; then
  ATIF_SQL="$HOME/.local/bin/atif-sql"
else
  ATIF_SQL="$ROOT/.venv/bin/atif-sql"
fi

fails=0
note() { printf '%s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; fails=$((fails + 1)); }
ok()   { printf 'ok    %s\n' "$*"; }

[ -f "$SCRIPT" ] || { fail "refresh script missing at $SCRIPT"; exit 1; }
[ -x "$ATIF_SQL" ] || { note "SKIP: $ATIF_SQL not executable (run \`uv sync --all-packages\`)"; exit 0; }

top_help="$("$ATIF_SQL" --help 2>&1)"

# ---------------------------------------------------------------------------
# (a) Every lane invocation's flags must be accepted by the current CLI.
#     Lane lines have the shape: "$ATIF_SQL" <subcommand> --flag ...
#     A renamed flag would otherwise make cyclopts reject the whole invocation
#     on every tick, which shows up only as a growing log nobody reads.
# ---------------------------------------------------------------------------
while IFS=' ' read -r subcommand flags; do
  if printf '%s' "$top_help" | grep -qw -- "$subcommand"; then
    sub_help="$("$ATIF_SQL" "$subcommand" --help 2>&1)"
    for flag in $flags; do
      if printf '%s' "$sub_help" | grep -qE -- "$flag\b"; then
        ok "$subcommand accepts $flag"
      elif [ "$flag" = --agent ] && grep -q 'codex not yet supported' "$SCRIPT"; then
        # --agent is allowed to be absent — but ONLY because the script probes
        # for it and skips the Codex pass when it is missing.
        ok "$subcommand lacks --agent but the refresh script carries the codex absence guard"
      else
        fail "$subcommand no longer accepts $flag (passed by the refresh script)"
      fi
    done
    [ -n "$flags" ] || ok "$subcommand exists (no flags passed)"
  elif [ "$subcommand" = analyze ]; then
    # analyze is allowed to be absent — but ONLY because the script guards it.
    if grep -q 'analytics not yet installed, skipping' "$SCRIPT"; then
      ok "analyze absent from CLI but the refresh script carries the absence guard"
    else
      fail "analyze absent from CLI and the refresh script has NO absence guard"
    fi
  elif [ "$subcommand" = embed ]; then
    # embed is allowed to be absent — but ONLY because the script guards it.
    if grep -q 'embed not yet installed, skipping' "$SCRIPT"; then
      ok "embed absent from CLI but the refresh script carries the absence guard"
    else
      fail "embed absent from CLI and the refresh script has NO absence guard"
    fi
  else
    fail "subcommand '$subcommand' is invoked by the refresh script but missing from the CLI"
  fi
done < <(
  # shellcheck disable=SC2016  # deliberate: grepping the SCRIPT for the literal text `"$ATIF_SQL"`, not expanding it here
  grep -oE '"\$ATIF_SQL" [a-z][a-z-]*( --[a-z-]+( [a-z0-9.]+)?)*' "$SCRIPT" \
    | sed 's/^"\$ATIF_SQL" //' \
    | awk '{ line=$1; for (i=2; i<=NF; i++) if ($i ~ /^--/ && $i != "--help") line=line " " $i; print line }' \
    | sort -u
)

# ---------------------------------------------------------------------------
# (b) --no-dry-run exactly once in EXECUTABLE code (comments stripped first —
#     the header EXPLAINS the rule and must not trip the count).
# ---------------------------------------------------------------------------
count="$(grep -v '^[[:space:]]*#' "$SCRIPT" | grep -c -- '--no-dry-run' || true)"
if [ "$count" = "1" ]; then
  ok "--no-dry-run appears exactly once in executable code (the llm lane)"
else
  fail "--no-dry-run appears $count times in executable code — only the llm lane may spend"
fi

# ---------------------------------------------------------------------------
# (b2) The llm lane must carry BOTH budget-ceiling flags on the same line as
#      --no-dry-run: an unattended spend without a visible cap in the crontab
#      chain is the exact failure mode the ceilings exist to prevent.
# ---------------------------------------------------------------------------
llm_line="$(grep -v '^[[:space:]]*#' "$SCRIPT" | grep -- '--no-dry-run' || true)"
for budget_flag in --max-sessions --max-cost-usd; do
  if printf '%s' "$llm_line" | grep -q -- "$budget_flag"; then
    ok "llm lane carries $budget_flag (budget ceiling visible in the cron chain)"
  else
    fail "llm lane is missing $budget_flag — the nightly spend has no visible ceiling"
  fi
done

# ---------------------------------------------------------------------------
# (b3) The embed piggyback must be BOUNDED: every executable `embed`
#      invocation carries --limit (a bare embed is a full backfill — the CLI
#      refuses it, and this pins the cron from ever asking).
# ---------------------------------------------------------------------------
# shellcheck disable=SC2016  # deliberate: grepping the SCRIPT for the literal text `"$ATIF_SQL" embed`
embed_lines="$(grep -v '^[[:space:]]*#' "$SCRIPT" | grep -- '"\$ATIF_SQL" embed' || true)"
if [ -z "$embed_lines" ]; then
  fail "no embed invocation in the refresh script — the VSS store never refreshes"
elif printf '%s\n' "$embed_lines" | grep -qv -- '--limit'; then
  fail "an embed invocation lacks --limit — an unbounded backfill in an unattended lane"
else
  ok "every embed invocation is bounded with --limit"
fi

# ---------------------------------------------------------------------------
# (c) Every corpus this host is configured for must be tickable: each path is
#     RESOLVED exactly as the refresh script resolves it — the primary root
#     plus every ATIF_SQL_EXTRA_CONFIG_DIRS entry — never grepped as a literal,
#     so a typo'd path fails here instead of becoming a silent skip.
# ---------------------------------------------------------------------------
config_dirs=("${CLAUDE_CONFIG_DIR:-$HOME/.claude}")
if [ -n "${ATIF_SQL_EXTRA_CONFIG_DIRS:-}" ]; then
  IFS=: read -r -a extra_config_dirs <<< "$ATIF_SQL_EXTRA_CONFIG_DIRS"
  for dir in "${extra_config_dirs[@]}"; do
    [ -n "$dir" ] && config_dirs+=("$dir")
  done
fi
if [ "${#config_dirs[@]}" -lt 1 ]; then
  fail "no corpus config dir resolved — the refresh script would have nothing to tick"
else
  for dir in "${config_dirs[@]}"; do
    if [ -d "$dir/projects" ]; then
      ok "corpus source root exists: $dir/projects"
    else
      fail "corpus source root does NOT exist (the script would silently skip it): $dir/projects"
    fi
  done
fi

# ---------------------------------------------------------------------------
# (d) Single-flight: nonblocking flock, and the lock is PER LANE.
# ---------------------------------------------------------------------------
if grep -q 'flock -n' "$SCRIPT"; then
  ok "single-flight lock present (nonblocking)"
else
  fail "no nonblocking flock — a busy lane would queue ticks instead of skipping"
fi
# shellcheck disable=SC2016  # deliberate: asserting the literal text `-$MODE.lock` exists in the SCRIPT
if grep -q -- '-\$MODE\.lock' "$SCRIPT"; then
  ok "lock file name carries \$MODE (one lock per lane)"
else
  fail "lock name does not carry \$MODE — one shared lock lets a slow lane starve the llm lane"
fi

# ---------------------------------------------------------------------------
# (e) THE GUARD MUST FIRE. Simulate an atif-sql WITHOUT `analyze` via a PATH
#     shim and run the real refresh script against it: the structural lane
#     must exit 0 and log the documented skip line. Then simulate one WITH
#     `analyze` and assert the lane actually invokes it — proving the guard
#     can both fire and stand down.
# ---------------------------------------------------------------------------
shim_root="$(mktemp -d)"
trap 'rm -rf "$shim_root"' EXIT

make_shim() {
  # $1 = shim dir, $2 = "with" | "without" (analyze in --help)
  local dir="$1" mode="$2"
  mkdir -p "$dir"
  cat > "$dir/atif-sql" <<SHIM
#!/usr/bin/env bash
case "\${1:-}" in
  --help)
    echo "Usage: atif-sql COMMAND"
    echo "  materialize  status  query  schema$([ "$mode" = with ] && echo '  analyze')"
    ;;
  analyze) echo "analyze \$*" >> "$dir/calls.log" ;;
  *) exit 0 ;;
esac
SHIM
  chmod +x "$dir/atif-sql"
}

# Without analyze: the guard must fire (exit 0 + skip line), and the shim must
# never see an `analyze` call.
make_shim "$shim_root/without" without
run_dir="$shim_root/without-run"
PATH="$shim_root/without:$PATH" \
  ATIF_SQL_CLI="$shim_root/without/atif-sql" \
  ATIF_SQL_REFRESH_RUN_DIR="$run_dir" \
  bash "$SCRIPT" structural
rc=$?
if [ "$rc" = 0 ] && grep -q 'analytics not yet installed, skipping' "$run_dir/atif-sql-refresh.log" 2>/dev/null; then
  ok "absence guard fires: structural lane exits 0 and logs the skip when analyze is missing"
else
  fail "absence guard did NOT fire cleanly (exit=$rc; expected 0 + logged skip line)"
fi
if [ -e "$shim_root/without/calls.log" ]; then
  fail "structural lane invoked analyze despite the CLI not advertising it"
else
  ok "structural lane never invoked the missing analyze subcommand"
fi

# With analyze: the guard must stand down and the lane must do real work.
make_shim "$shim_root/with" with
run_dir="$shim_root/with-run"
PATH="$shim_root/with:$PATH" \
  ATIF_SQL_CLI="$shim_root/with/atif-sql" \
  ATIF_SQL_REFRESH_RUN_DIR="$run_dir" \
  bash "$SCRIPT" structural
rc=$?
if [ "$rc" = 0 ] && grep -q -- '--structural-only' "$shim_root/with/calls.log" 2>/dev/null; then
  ok "guard stands down: structural lane invokes analyze --structural-only when present"
else
  fail "structural lane did not invoke analyze when the CLI advertises it (exit=$rc)"
fi

# ---------------------------------------------------------------------------
# (f) TERMINAL EXIT SUPPRESSION MUST FIRE. Shim an atif-sql whose `embed`
#     exits 78 (terminal_state: operator action required) and run the
#     materialize lane three times:
#       tick 1: embed is attempted, the lane logs the TERMINAL line ONCE per
#               corpus and drops a marker;
#       tick 2: embed is NOT re-attempted (suppressed) — no new TERMINAL line;
#       tick 3 (after the store dir's mtime changes): the marker clears and
#               embed is attempted again.
#     A suppression that was never seen firing — or never seen CLEARING — is
#     hope, not a guard.
# ---------------------------------------------------------------------------
term_dir="$shim_root/terminal"
mkdir -p "$term_dir"
cat > "$term_dir/atif-sql" <<SHIM
#!/usr/bin/env bash
case "\${1:-}" in
  --help)
    echo "Usage: atif-sql COMMAND"
    echo "  materialize  status  query  schema  embed"
    echo "  --limit --format"
    ;;
  materialize) echo '{"materialize": "ok"}' ;;
  status) echo "{\"corpus_root\": \"$term_dir/corpus\"}" ;;
  embed)
    echo "embed \$*" >> "$term_dir/calls.log"
    echo '{"error": {"kind": "terminal_state", "message": "store requires operator action", "hint": null}}' >&2
    exit 78
    ;;
  *) exit 0 ;;
esac
SHIM
chmod +x "$term_dir/atif-sql"

term_run_dir="$shim_root/terminal-run"
run_materialize_tick() {
  PATH="$term_dir:$PATH" \
    ATIF_SQL_CLI="$term_dir/atif-sql" \
    ATIF_SQL_REFRESH_RUN_DIR="$term_run_dir" \
    bash "$SCRIPT" materialize
}

# Tick 1: terminal exit → exactly one TERMINAL log line per attempted corpus,
# and one embed call per attempted corpus.
run_materialize_tick
calls_after_1="$(grep -c '^embed' "$term_dir/calls.log" 2>/dev/null)" || calls_after_1=0
terminal_lines="$(grep -c 'TERMINAL: embed for' "$term_run_dir/atif-sql-refresh.log" 2>/dev/null)" || terminal_lines=0
if [ "$calls_after_1" -ge 1 ] && [ "$terminal_lines" = "$calls_after_1" ]; then
  ok "terminal exit 78 logs TERMINAL exactly once per embed attempt ($terminal_lines)"
else
  fail "expected one TERMINAL line per embed attempt, got $terminal_lines lines for $calls_after_1 calls"
fi

# Tick 2: suppression — no new embed calls, no new TERMINAL lines, a
# suppressed line instead.
run_materialize_tick
calls_after_2="$(grep -c '^embed' "$term_dir/calls.log" 2>/dev/null)" || calls_after_2=0
terminal_lines_2="$(grep -c 'TERMINAL: embed for' "$term_run_dir/atif-sql-refresh.log" 2>/dev/null)" || terminal_lines_2=0
if [ "$calls_after_2" = "$calls_after_1" ] && [ "$terminal_lines_2" = "$terminal_lines" ] \
   && grep -q 'embed suppressed' "$term_run_dir/atif-sql-refresh.log"; then
  ok "suppression holds: no embed retry, no repeat TERMINAL line while the condition persists"
else
  fail "suppression did not hold (calls $calls_after_1 -> $calls_after_2, TERMINAL $terminal_lines -> $terminal_lines_2)"
fi

# Tick 3: clearance — the store dir's mtime changes (the operator acted), so
# the marker clears and embed is attempted again.
mkdir -p "$term_dir/corpus/embeddings_lance"
run_materialize_tick
calls_after_3="$(grep -c '^embed' "$term_dir/calls.log" 2>/dev/null)" || calls_after_3=0
if [ "$calls_after_3" -gt "$calls_after_2" ] \
   && grep -q 'terminal marker cleared' "$term_run_dir/atif-sql-refresh.log"; then
  ok "clearance works: store mtime change lifts the suppression and embed retries"
else
  fail "store mtime change did not lift the suppression (calls $calls_after_2 -> $calls_after_3)"
fi

# ---------------------------------------------------------------------------
# (g) THE CODEX GUARD MUST FIRE, AND STAND DOWN. The materialize lane runs one
#     extra pass with `--agent codex`, and the resolved CLI can predate that
#     flag. Shim an atif-sql whose `materialize --help` lacks `--agent` and
#     assert the lane logs the documented skip and never passes the flag; then
#     shim one that advertises it and assert the pass actually runs.
# ---------------------------------------------------------------------------
codex_home="$shim_root/codex-home"
mkdir -p "$codex_home/sessions"

make_codex_shim() {
  # $1 = shim dir, $2 = "with" | "without" (--agent in `materialize --help`)
  local dir="$1" mode="$2"
  mkdir -p "$dir"
  cat > "$dir/atif-sql" <<SHIM
#!/usr/bin/env bash
case "\${1:-}" in
  --help)
    echo "Usage: atif-sql COMMAND"
    echo "  materialize  status  query  schema  analyze"
    ;;
  materialize)
    if [ "\${2:-}" = --help ]; then
      echo "Usage: atif-sql materialize [OPTIONS]"
      echo "  --force --quiesce-seconds --source-root --corpus-root --sessions --format$([ "$mode" = with ] && echo ' --agent')"
    else
      echo "materialize \$*" >> "$dir/calls.log"
      echo '{"materialize": "ok"}'
    fi
    ;;
  status)
    if [ "\${2:-}" = --help ]; then
      echo "Usage: atif-sql status [OPTIONS]"
      echo "  --source-root --corpus-root --quiesce-seconds --format$([ "$mode" = with ] && echo ' --agent')"
    else
      echo "status \$*" >> "$dir/calls.log"
      echo '{"status": "ok"}'
    fi
    ;;
  analyze) echo "analyze \$*" >> "$dir/calls.log" ;;
  *) exit 0 ;;
esac
SHIM
  chmod +x "$dir/atif-sql"
}

run_codex_tick() {
  # $1 = shim dir
  PATH="$1:$PATH" \
    ATIF_SQL_CLI="$1/atif-sql" \
    ATIF_SQL_REFRESH_RUN_DIR="$1-run" \
    CODEX_HOME="$codex_home" \
    bash "$SCRIPT" materialize
}

make_codex_shim "$shim_root/codex-without" without
run_codex_tick "$shim_root/codex-without"
rc=$?
if [ "$rc" = 0 ] \
   && grep -q 'codex not yet supported' "$shim_root/codex-without-run/atif-sql-refresh.log" 2>/dev/null \
   && ! grep -q -- '--agent' "$shim_root/codex-without/calls.log" 2>/dev/null; then
  ok "codex guard fires: the pass is skipped and --agent is never passed to a CLI without it"
else
  fail "codex guard did NOT fire cleanly (exit=$rc; expected 0, a logged skip, and no --agent call)"
fi

make_codex_shim "$shim_root/codex-with" with
run_codex_tick "$shim_root/codex-with"
rc=$?
if [ "$rc" = 0 ] && grep -q -- 'materialize --agent codex' "$shim_root/codex-with/calls.log" 2>/dev/null; then
  ok "codex guard stands down: the lane runs materialize --agent codex when the CLI carries the flag"
else
  fail "the codex pass did not run against a CLI advertising --agent (exit=$rc)"
fi

# The freshness dump must cover the Codex corpus too, or the log answers "is it
# fresh?" for every corpus except the one with no lane of its own.
PATH="$shim_root/codex-with:$PATH" \
  ATIF_SQL_CLI="$shim_root/codex-with/atif-sql" \
  ATIF_SQL_REFRESH_RUN_DIR="$shim_root/codex-status-run" \
  CODEX_HOME="$codex_home" \
  bash "$SCRIPT" structural
if grep -q 'corpus status: codex' "$shim_root/codex-status-run/atif-sql-refresh.log" 2>/dev/null \
   && grep -q -- 'status --agent codex' "$shim_root/codex-with/calls.log" 2>/dev/null; then
  ok "the freshness dump reports the Codex corpus"
else
  fail "no Codex corpus status in the freshness dump — its staleness would go unreported"
fi

# A pinned ATIF_SQL_CORPUS_ROOT belongs to the Claude corpus, and one corpus
# holds one agent — the pass must refuse rather than mix agents in it.
ATIF_SQL_CORPUS_ROOT="$shim_root/pinned-corpus" run_codex_tick "$shim_root/codex-with" >/dev/null
if grep -q 'set ATIF_SQL_CODEX_CORPUS_ROOT' "$shim_root/codex-with-run/atif-sql-refresh.log" 2>/dev/null; then
  ok "codex pass refuses a corpus root pinned to the Claude corpus"
else
  fail "codex pass did not refuse a pinned ATIF_SQL_CORPUS_ROOT — two agents could land in one corpus"
fi

printf '\n%s\n' "selftest: $fails failure(s)"
exit "$fails"
