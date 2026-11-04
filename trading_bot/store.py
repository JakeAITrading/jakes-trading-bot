"""Bot memory and trade log, kept as plain files in state/ and committed to GitHub.

  state/state.json      - what the bot remembers between runs (cooldowns, daily budget, practice positions)
  state/trades.jsonl    - every buy/sell/exit order it sent or simulated, one per line
  state/balances.jsonl  - wallet value after each cycle (the profit-and-loss curve)
No secrets are ever written here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from trading_bot.settings import ROOT

STATE_DIR = ROOT / "state"


def _path(name: str) -> Path:
    return Path(os.environ.get("BOT_STATE_DIR", STATE_DIR)) / name


def load_state() -> dict:
    p = _path("state.json")
    return json.loads(p.read_text()) if p.exists() else {}


def save_state(state: dict) -> None:
    p = _path("state.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True, default=str) + "\n")
    tmp.replace(p)


def append(name: str, row: dict) -> None:
    p = _path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as fh:
        fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def read_lines(name: str) -> list[dict]:
    p = _path(name)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def no_buy() -> bool:
    """True while a NO_BUY file pauses new buys (repo root; tests point BOT_STATE_DIR elsewhere)."""
    import os
    from pathlib import Path
    d = os.environ.get("BOT_STATE_DIR")
    root = Path(d) if d else Path(__file__).resolve().parent.parent
    return (root / "NO_BUY").exists()
