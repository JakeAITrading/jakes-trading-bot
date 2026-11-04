"""Honest test: do the learners' settings make money on signals they have NOT seen?

Each learner picks settings from the older part of its samples (train) and is scored on the newer
part (test), several splits. "Take everything" is the baseline: trading every signal. A strategy is
worth real money only if its test results are positive and beat the baseline across splits.

  python -m trading_bot.oos_check
"""
from __future__ import annotations

import json

from trading_bot import learner as L
from trading_bot import main_learner as M
from trading_bot.store import _path


def _load(name):
    p = _path(name)
    return json.loads(p.read_text()) if p.exists() else []


def whale(splits=(0.4, 0.5, 0.6)) -> list[str]:
    live = sorted(_load(L.SAMPLES), key=lambda s: s["key"].split(":", 1)[1])
    prior = _load(L.PRIOR)
    lines = [f"Whale trader: {len(live)} live samples"]
    loose = {**L.DEFAULTS, "min_mcap": 20_000, "max_mcap": 10_000_000, "min_whale_usd": 100, "max_spike": None}
    for f in splits:
        k = int(len(live) * f)
        if k < 50 or len(live) - k < 50:
            continue
        train = [{**s, "w": 1.0} for s in prior] + [{**s, "w": L.LIVE_WEIGHT} for s in live[:k]]
        p, e = L.optimize(train, dict(L.DEFAULTS))
        t, base = L.evaluate(live[k:], p), L.evaluate(live[k:], loose)
        lines.append(f"  trained on {k}: looked {e['avg'] * 100:+.1f}% -> next {len(live) - k} signals "
                     f"{t['avg'] * 100:+.1f}% on {t['trades']:.0f} trades (take-everything {base['avg'] * 100:+.1f}%)")
    return lines


def main_strategy(splits=(0.4, 0.5, 0.6)) -> list[str]:
    s = sorted(_load(M.SAMPLES), key=lambda x: int(x["key"].split(":")[1]))
    lines = [f"Main strategy: {len(s)} samples"]
    start = M.start_params({})
    for f in splits:
        k = int(len(s) * f)
        if k < 40 or len(s) - k < 40:
            continue
        p, e = M.optimize(s[:k], start)
        t, base = M.evaluate(s[k:], p), M.evaluate(s[k:], {**start, **M.LOOSE})
        lines.append(f"  trained on {k}: looked {e['avg'] * 100:+.1f}% -> next {len(s) - k} "
                     f"{t['avg'] * 100:+.1f}% on {t['trades']} trades (take-everything {base['avg'] * 100:+.1f}%)")
    return lines


if __name__ == "__main__":
    print("\n".join(whale() + main_strategy()))
