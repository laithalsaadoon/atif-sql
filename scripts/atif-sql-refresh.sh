#!/usr/bin/env bash
# Keep atif-sql's materialized corpora + analytics caches fresh (cron; one lane per plane).
#
# Three lanes, one per plane:
#
#   materialize  */10  Zero-cost CPU: scan -> plan -> convert -> write through
#                      `atif-sql materialize`. Incremental by watermark +
#                      quiescence, so the steady-state tick converts only the
#                      sessions that moved. Embedding PIGGYBACKS here (decision
#                      record): after each materialize pass the lane runs
#                      `atif-sql embed --limit 500` per corpus — Cohere embed
#                      pricing makes 500 steps cost fractions of a cent, the
#                      anti-join means a steady-state tick embeds only what
#                      just materialized, and riding the same lane means no
#                      fourth crontab line / lock / failure mode. A dedicated
#                      */30 lane was considered and rejected: it buys nothing
#                      but drift between corpus and embeddings. The explicit
#                      --limit keeps any one tick bounded even after a bulk
#                      re-materialize; the backlog clears across ticks.
#   structural   :17   `atif-sql analyze --structural-only` — the zero-cost
#                      analytics stages (cluster/terms/community). Guarded:
#                      see below.
#   llm          10:20 `atif-sql analyze --no-dry-run --llm-only` — REAL SPEND
#                      (classify/trajectory/conflicts/friction on Bedrock).
#                      Deliberate and nightly. Guarded: see below.
#
# THE ANALYTICS GUARD. The CLI the resolution order below lands on can predate
# the `analyze` subcommand (an older `~/.local/bin/atif-sql` wins over the
# workspace venv). Both analyze lanes therefore probe `atif-sql --help` first
# and, when `analyze` is absent, log "analytics not yet installed, skipping"
# and exit 0 — an armed crontab line is a no-op against a CLI that cannot serve
# it, not an hourly error. The selftest
# (scripts/atif-sql-refresh-selftest.sh) pins this guard by actually running
# this script against a shimmed CLI with and without `analyze`.
#
# ONE LOCK PER MODE — load-bearing. The lock file name carries $MODE, so a slow
# structural run can never starve the nightly llm tick. Two runs of the SAME
# mode must not overlap (two writers to one corpus/watermark is corruption),
# which is what the per-mode lock preserves. flock is NONBLOCKING: a busy lane
# means SKIP this tick, never queue behind it.
#
# COST POSTURE (CONTRACT-V2 §Cost guards): `analyze` defaults dry_run=true;
# `--no-dry-run` appears exactly ONCE in this file's executable code, in the
# llm lane, and the selftest counts it.
#
# ONE OR MORE CORPORA PER TICK. The primary Claude config root is
# ${CLAUDE_CONFIG_DIR:-$HOME/.claude}. Extra roots come from
# ATIF_SQL_EXTRA_CONFIG_DIRS, a colon-separated list of config-dir paths: an
# agent fleet gets its own isolated config root so a run cannot mutate the
# operator's live session state, and each such root is its own corpus.
#
# atif-corpus resolves its source root from ATIF_SQL_SOURCE_ROOT
# (= <config-dir>/projects), BUT its default corpus-root slug derives from
# CLAUDE_CONFIG_DIR — setting only ATIF_SQL_SOURCE_ROOT points one corpus's
# source at another root's transcripts while the corpus root still slugs the
# primary default (verified live 2026-08-23: two corpora collapse onto one
# slug, projects-73b00a87). So each tick exports BOTH
# CLAUDE_CONFIG_DIR=<config-dir> and ATIF_SQL_SOURCE_ROOT=<config-dir>/projects,
# keeping source_root and the corpus slug coherent per corpus.
#
# CLI RESOLUTION (decided + documented): $ATIF_SQL_CLI override (the selftest's
# shim hook) -> ~/.local/bin/atif-sql (a `uv tool install`, when one is
# present) -> <this repo>/.venv/bin/atif-sql (the workspace venv, present by
# construction after `uv sync --all-packages`). The venv console script IS the
# "uv-run fallback from the repo" without `uv run`'s implicit-sync side effect,
# which an unattended cron must not trigger.
#
# Arm with ONE user-crontab line per lane (`atif-sql cron install` prints this
# block; check `crontab -l` first — no tool writes the crontab silently):
#   */10 * * * * <repo>/scripts/atif-sql-refresh.sh materialize >> <repo>/scripts/.run/atif-sql-refresh.cron.log 2>&1
#   17   * * * * <repo>/scripts/atif-sql-refresh.sh structural  >> <repo>/scripts/.run/atif-sql-refresh.cron.log 2>&1
#   20  10 * * * <repo>/scripts/atif-sql-refresh.sh llm         >> <repo>/scripts/.run/atif-sql-refresh.cron.log 2>&1
#
# scripts/atif-sql-refresh-selftest.sh asserts this file's flags against the
# installed CLI's --help; run it after every atif-sql upgrade.
set -uo pipefail

# Env hygiene: cron's env is minimal, but this script is ALSO run by hand
# during an incident from a shell carrying who-knows-what. Pin the identity
# trio ONCE, from the invoking environment, so every lane and every corpus in
# this tick resolves the same $HOME. ATIF_SQL_HOME / ATIF_SQL_USER override it
# for a cron context that must read a different account's transcripts than the
# one the crontab belongs to. PATH covers coreutils + util-linux flock only.
ATIF_SQL_HOME="${ATIF_SQL_HOME:-${HOME:-}}"
[ -n "$ATIF_SQL_HOME" ] || ATIF_SQL_HOME="$(getent passwd "$(id -u)" | cut -d: -f6)"
[ -n "$ATIF_SQL_HOME" ] || { echo "FATAL: cannot resolve a home directory — set ATIF_SQL_HOME" >&2; exit 1; }
export HOME="$ATIF_SQL_HOME"
export USER="${ATIF_SQL_USER:-$(id -un)}"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

# AWS_PROFILE must NOT reach a Bedrock call: an inherited profile name with no
# matching entry in ~/.aws/config makes botocore raise FileNotFoundError at
# credential resolution, killing the lane before it reaches Bedrock at all.
# Unattended runs authenticate from the bearer token below or from the default
# credential chain, never from a named profile.
unset AWS_PROFILE AWS_DEFAULT_PROFILE

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
RUN_DIR="${ATIF_SQL_REFRESH_RUN_DIR:-$SCRIPT_DIR/.run}"
mkdir -p "$RUN_DIR"
LOG="$RUN_DIR/atif-sql-refresh.log"
log() { echo "$(date -Is) $*" >> "$LOG"; }

# Normalize the mode BEFORE it names a lock file, so two spellings of one lane
# cannot take two different locks and run concurrently. The lane is a required
# argument — an unarmed default would let a typo'd crontab line silently run
# the wrong plane.
case "${1:-}" in
  materialize|mat)   MODE=materialize ;;
  structural|struct) MODE=structural ;;
  llm|--llm)         MODE=llm ;;
  *) log "FATAL: unknown mode '${1:-}' (expected: materialize | structural | llm)"; exit 64 ;;
esac

# The rotated Bedrock bearer token, read at RUN TIME from
# $ATIF_SQL_BEDROCK_TOKEN_FILE and never frozen: rotation rewrites the file's
# contents under a live crontab. Export only when non-empty — botocore treats
# "" as a real broken credential and skips the default chain entirely. Only the
# llm lane spends, so only it warrants the missing-token warning. With no token
# file, credentials resolve from the default chain (instance role, SSO cache).
TOKEN_FILE="${ATIF_SQL_BEDROCK_TOKEN_FILE:-$HOME/.cache/bedrock-keys/current.token}"
if [ -s "$TOKEN_FILE" ]; then
  AWS_BEARER_TOKEN_BEDROCK="$(cat "$TOKEN_FILE")"
  export AWS_BEARER_TOKEN_BEDROCK
elif [ "$MODE" = llm ]; then
  log "WARN: no Bedrock token at $TOKEN_FILE — the llm lane will fail"
fi

# CORPUS ROOTS, in tick order: the primary root first, then every
# ATIF_SQL_EXTRA_CONFIG_DIRS entry. Resolved once, before any lane runs — see
# the header for why each corpus exports both CLAUDE_CONFIG_DIR and
# ATIF_SQL_SOURCE_ROOT.
PRIMARY_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
CORPUS_NAMES=(primary)
CORPUS_DIRS=("$PRIMARY_CONFIG_DIR")
if [ -n "${ATIF_SQL_EXTRA_CONFIG_DIRS:-}" ]; then
  IFS=: read -r -a extra_config_dirs <<< "$ATIF_SQL_EXTRA_CONFIG_DIRS"
  for i in "${!extra_config_dirs[@]}"; do
    [ -n "${extra_config_dirs[$i]}" ] || continue
    CORPUS_NAMES+=("extra$((i + 1))")
    CORPUS_DIRS+=("${extra_config_dirs[$i]}")
  done
fi

# A config root can sit on a nofail mount; a cron run firing before it lands
# must not half-refresh against a missing tree.
[ -d "$PRIMARY_CONFIG_DIR" ] || { log "FATAL: $PRIMARY_CONFIG_DIR not present"; exit 1; }

# CLI resolution — see the header for the decision record.
if [ -n "${ATIF_SQL_CLI:-}" ]; then
  ATIF_SQL="$ATIF_SQL_CLI"
elif [ -x "$HOME/.local/bin/atif-sql" ]; then
  ATIF_SQL="$HOME/.local/bin/atif-sql"
else
  ATIF_SQL="$ROOT/.venv/bin/atif-sql"
fi
[ -x "$ATIF_SQL" ] || { log "FATAL: $ATIF_SQL not executable (run \`uv sync --all-packages\` in $ROOT)"; exit 1; }

# SINGLE-FLIGHT PER MODE. flock with a nonblocking guard: a still-running run
# of THIS mode means SKIP this tick, not queue behind it. A different mode is
# free to proceed — see the header.
LOCK="$RUN_DIR/atif-sql-refresh-$MODE.lock"
PIDFILE="$RUN_DIR/atif-sql-refresh-$MODE.pid"
exec 9>"$LOCK"
if ! flock -n 9; then
  log "skip[$MODE]: a $MODE run is already going (pid $(cat "$PIDFILE" 2>/dev/null || echo '?'))"
  exit 0
fi
echo $$ > "$PIDFILE"

# THE LANE LOCK IS HELD BY THE OPEN DESCRIPTOR, NOT BY THIS PID, so every
# `atif-sql` call below closes fd 9 (`9>&-`). A descendant that inherits it
# keeps the lane locked for as long as the descendant lives — a lane that
# stops doing work while every log line still reads like a clean single-flight.
# The parent keeps its own fd 9, so single-flight is unaffected.

# THE ANALYTICS GUARD (see header). Probed once — the subcommand's existence
# does not vary per corpus.
if [ "$MODE" != materialize ]; then
  if ! "$ATIF_SQL" --help 2>/dev/null 9>&- | grep -qw analyze; then
    log "[$MODE] analytics not yet installed, skipping"
    exit 0
  fi
fi

# TERMINAL vs TRANSIENT embed failures. `atif-sql embed` exits 78 (EX_CONFIG)
# when the store or its config needs an OPERATOR — a state no retry can clear.
# Retrying those anyway is how the 2026-08-24 schema-stale condition burned
# 400+ identical 10-minute ticks over 3 days with zero escalation. On 78 this
# lane logs ONE loud TERMINAL line, drops a marker keyed on the store path +
# its mtime, and skips embed for that corpus until the store's directory
# mtime changes (an rm -rf, a rebuild, a restore — any operator touch clears
# it). Every other nonzero exit stays transient: next tick retries.

embed_store_dir() {
  # Resolve the corpus's Lance store directory the same way atif-embed does:
  # the ATIF_SQL_LANCE_URI override, else <corpus_root>/embeddings_lance.
  # `atif-sql status` is read-only and fast, and it answers under the SAME
  # CLAUDE_CONFIG_DIR/ATIF_SQL_SOURCE_ROOT exports this tick runs under.
  if [ -n "${ATIF_SQL_LANCE_URI:-}" ]; then
    printf '%s\n' "$ATIF_SQL_LANCE_URI"
    return
  fi
  local corpus_root
  corpus_root="$("$ATIF_SQL" status --format json 2>/dev/null 9>&- \
    | sed -n 's/.*"corpus_root": *"\([^"]*\)".*/\1/p' | head -n 1)"
  [ -n "$corpus_root" ] && printf '%s/embeddings_lance\n' "$corpus_root"
}

store_mtime() {
  # Epoch mtime of the store dir, or "absent" — which is a VALID state that
  # differs from any recorded number, so deleting the store clears the marker.
  stat -c %Y "$1" 2>/dev/null || echo absent
}

run_embed_piggyback() {
  # Bounded --limit 500 per tick, anti-joined against the Lance store so
  # steady state embeds only what just landed. An embed failure never fails
  # the lane — the corpus write already succeeded.
  local name="$1"
  local marker="$RUN_DIR/atif-sql-embed-terminal-$name.marker"
  local store_dir
  store_dir="$(embed_store_dir)"

  if [ -f "$marker" ]; then
    local recorded_mtime current_mtime
    recorded_mtime="$(sed -n '2p' "$marker")"
    current_mtime="$(store_mtime "$store_dir")"
    if [ "$current_mtime" = "$recorded_mtime" ]; then
      log "$name: embed suppressed (terminal condition persists; clear by fixing the store at $store_dir)"
      return 0
    fi
    log "$name: terminal marker cleared (store mtime changed at $store_dir) — resuming embed"
    rm -f "$marker"
  fi

  local embed_err="$RUN_DIR/atif-sql-embed-$name.stderr"
  local rc=0
  "$ATIF_SQL" embed --limit 500 --format json >> "$LOG" 2>"$embed_err" 9>&- || rc=$?
  cat "$embed_err" >> "$LOG" 2>/dev/null
  if [ "$rc" -eq 0 ]; then
    log "$name: embed ok"
  elif [ "$rc" -eq 78 ]; then
    local reason
    reason="$(sed -n 's/.*"message": *"\([^"]*\)".*/\1/p' "$embed_err" | head -n 1 | cut -c1-200)"
    [ -n "$reason" ] || reason="exit 78 with no parseable error envelope"
    printf '%s\n%s\n%s\n' "$store_dir" "$(store_mtime "$store_dir")" "$reason" > "$marker"
    log "TERMINAL: embed for $name requires operator action ($reason) — suppressing retries until store mtime changes"
  else
    log "$name: embed FAILED (exit $rc, non-fatal; next tick retries)"
  fi
}

run_materialize() {
  # Incremental + zero-cost: watermark and quiescence bound the work, so the
  # */10 cadence converts only what moved since the last tick.
  local name="$1"
  if ! "$ATIF_SQL" materialize --format json >> "$LOG" 2>&1 9>&-; then
    log "$name: materialize FAILED (see above)"
    return 1
  fi
  log "$name: materialize ok"

  # Embed piggyback (see header). Guarded like analyze: an atif-sql without
  # `embed` skips quietly. Terminal-exit handling lives in run_embed_piggyback.
  if "$ATIF_SQL" --help 2>/dev/null 9>&- | grep -qw embed; then
    run_embed_piggyback "$name"
  else
    log "$name: embed not yet installed, skipping"
  fi
}

run_structural() {
  # The zero-cost analytics stages only. `--structural-only` is the opt-IN
  # spelling (rather than an opt-out skip list) so a new upstream stage can
  # never join this unattended lane silently.
  local name="$1"
  if ! "$ATIF_SQL" analyze --structural-only >> "$LOG" 2>&1 9>&-; then
    log "$name: structural refresh FAILED (see above)"
    return 1
  fi
  log "$name: structural refresh ok"
}

run_llm() {
  # `--no-dry-run` is what makes analyze actually spend, so it appears exactly
  # here, in the lane a human scheduled deliberately, and nowhere else in this
  # file's executable code (the selftest counts it).
  #
  # THE BUDGET CEILING IS VISIBLE IN THIS LINE on purpose: --max-sessions and
  # --max-cost-usd are hard per-run caps enforced inside run_analyze (session
  # cap per pipeline newest-first; dollar cap against running actual usage).
  # An unattended nightly tick can never spend more than this, no matter how
  # big the backlog is. The selftest asserts both flags are present.
  local name="$1"
  if ! "$ATIF_SQL" analyze --no-dry-run --llm-only --max-sessions 50 --max-cost-usd 25.0 >> "$LOG" 2>&1 9>&-; then
    log "$name: LLM refresh FAILED (see above)"
    return 1
  fi
  log "$name: LLM refresh ok (spent)"
}

overall=0
for i in "${!CORPUS_NAMES[@]}"; do
  name="${CORPUS_NAMES[$i]}"
  config_dir="${CORPUS_DIRS[$i]}"
  if [ ! -d "$config_dir/projects" ]; then
    log "$name: $config_dir/projects absent — skip"
    continue
  fi
  export CLAUDE_CONFIG_DIR="$config_dir"
  export ATIF_SQL_SOURCE_ROOT="$config_dir/projects"

  case "$MODE" in
    materialize) run_materialize "$name" || overall=1 ;;
    structural)  run_structural "$name" || overall=1 ;;
    llm)         run_llm "$name" || overall=1 ;;
  esac
done

# Report what the corpora now look like, so the log answers "is it fresh?"
# without a second command. `atif-sql status` is read-only and fast (a scan +
# a pure re-plan, no conversion), so it is cheap enough to dump per corpus.
# Only the non-materialize lanes bother — the */10 lane would drown the log.
if [ "$MODE" != materialize ]; then
  for i in "${!CORPUS_NAMES[@]}"; do
    name="${CORPUS_NAMES[$i]}"
    config_dir="${CORPUS_DIRS[$i]}"
    [ -d "$config_dir/projects" ] || continue
    log "--- corpus status: $name ---"
    CLAUDE_CONFIG_DIR="$config_dir" ATIF_SQL_SOURCE_ROOT="$config_dir/projects" \
      "$ATIF_SQL" status --format json >> "$LOG" 2>&1 9>&- || true
  done
fi

log "refresh complete (mode=$MODE, exit=$overall)"
exit "$overall"
