#!/usr/bin/env bash
# One Moby bot cycle (by hand: bash scripts/cycle.sh). Writes last_run.txt and state/.
set -u
cd "$(dirname "$0")/.."
PY="${PYTHON:-python}"
$PY -m trading_bot.run
