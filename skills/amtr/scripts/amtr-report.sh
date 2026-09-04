#!/bin/sh
# amtr-report.sh — headless amtr session report for the Claude Code skill.
#
#   amtr-report.sh [--json] [--session PATH|ID | --project DIR] [--budget N] [--watch]
#
# With no --session/--project the newest session under the current working
# directory is reported (the session the agent is in). If that directory has
# no sessions, the newest session anywhere is reported instead.
#
# The engine (amtr_engine.py, stdlib-only python) is resolved in this order,
# so the skill works on every install channel and from a bare checkout:
#   1. $AMTR_ENGINE
#   2. `amtr-report` on PATH               (Homebrew installs ship it)
#   3. beside the `amtr` binary            (curl installer bundle)
#   4. the cargo install's extracted copy  (~/.cache/amtr/engine-<version>/)
#   5. the repository root, when the skill runs from a checkout
#   6. the copy bundled with this skill    (synced by packaging/sync-engine.sh)
set -e
HERE=$(cd "$(dirname "$0")" && pwd)

realpath_py() { python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$1"; }

engine=""
if [ -n "$AMTR_ENGINE" ] && [ -f "$AMTR_ENGINE" ]; then
  engine="$AMTR_ENGINE"
elif command -v amtr-report >/dev/null 2>&1; then
  engine="amtr-report"
else
  if command -v amtr >/dev/null 2>&1; then
    bindir=$(dirname "$(realpath_py "$(command -v amtr)")")
    for cand in "$bindir/amtr_engine.py" "$bindir/../libexec/amtr_engine.py" "$bindir/../lib/amtr/amtr_engine.py"; do
      [ -f "$cand" ] && { engine="$cand"; break; }
    done
  fi
  if [ -z "$engine" ]; then
    cache="${XDG_CACHE_HOME:-$HOME/.cache}/amtr"
    if [ -d "$cache" ]; then
      cand=$(ls -d "$cache"/engine-*/amtr_engine.py 2>/dev/null | sort -V | tail -1)
      [ -n "$cand" ] && engine="$cand"
    fi
  fi
  [ -z "$engine" ] && [ -f "$HERE/../../../amtr_engine.py" ] && engine="$HERE/../../../amtr_engine.py"
  [ -z "$engine" ] && [ -f "$HERE/amtr_engine.py" ] && engine="$HERE/amtr_engine.py"
fi
[ -n "$engine" ] || { echo "amtr-report.sh: no amtr engine found (set AMTR_ENGINE or install amtr)" >&2; exit 2; }

targeted=0
for a in "$@"; do
  case "$a" in --session|--session=*|--project|--project=*) targeted=1;; esac
done

run() {
  if [ "$engine" = "amtr-report" ]; then amtr-report "$@"; else python3 "$engine" --report "$@"; fi
}

if [ "$targeted" = 1 ]; then
  run "$@"
else
  # newest session for this directory; fall back to newest anywhere
  run --project "$PWD" "$@" 2>/dev/null || run "$@"
fi
