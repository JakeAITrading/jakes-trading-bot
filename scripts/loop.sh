#!/usr/bin/env bash
# Run scripts/cycle.sh every INTERVAL seconds, COUNT times, then exit; saves results to GitHub after
# each cycle and takes code changes (and a REMOTE_STOP file) from GitHub before each one.
#   COUNT=4 INTERVAL=900 bash scripts/loop.sh      # every 15 minutes for an hour
# Only one loop runs at a time. Never run it in two places (double trading).
set -u
# Wrapped in a function: bash reads all of it before running, so editing this file mid-run is safe.
main() {
  cd "$(dirname "$0")/.."
  INTERVAL="${INTERVAL:-900}"
  COUNT="${COUNT:-4}"
  BRANCH="${BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
  export PYTHON="${PYTHON:-.venv/bin/python}"
  mkdir -p .state
  if command -v flock >/dev/null; then
    exec 9>.state/loop.lock
    flock -w 600 9 || { echo "another loop is still running"; exit 0; }
  fi

  save() {
    git add state/ last_run.txt 2>/dev/null
    git diff --cached --quiet || git commit -q -m "Moby bot cycle $(date -u +%Y-%m-%dT%H:%MZ)"
    git pull -q --no-rebase origin "$BRANCH" || echo "pull failed; running the code already here"
    git push -q origin "HEAD:$BRANCH" 2>/dev/null || echo "push failed; will retry next cycle"
  }

  for i in $(seq 1 "$COUNT"); do
    save
    if ! cmp -s requirements.txt .venv/.installed-requirements 2>/dev/null; then
      $PYTHON -m pip install -q -r requirements.txt && cp requirements.txt .venv/.installed-requirements
    fi
    bash scripts/cycle.sh
    save
    [ "$i" -lt "$COUNT" ] && sleep "$INTERVAL"
  done
}
main "$@"
