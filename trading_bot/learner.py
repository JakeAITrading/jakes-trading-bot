"""Self-learning take-profit.

For every coin the whale trader bought (and every coin it skipped, as if bought), record how high the
price went and where it ended within WINDOW after entry. From those samples, score each candidate
take-profit: a sample pays (tp - cost) if its high reached the target, otherwise its end-of-window
return minus cost. The bot uses the best-scoring target and re-checks every few minutes, so it raises
the target when coins keep running past it (money left on the table) and lowers it when they fade.

Starts from prior samples (past whale signals, state/learning_prior.json) and weights live samples
more, so it adapts to what is happening now.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time

from trading_bot.store import _path

log = logging.getLogger("learner")

CANDIDATES = [0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]
COST = 0.03          # fees + slippage per round trip
WINDOW = 2 * 3600    # how long after entry a sample is followed
LIVE_WEIGHT = 3.0    # live samples count 3x prior ones
MAX_LIVE = 80        # learn from the most recent live samples
SAMPLES = "learning.json"
PRIOR = "learning_prior.json"


def _load(name: str) -> list:
    p = _path(name)
    return json.loads(p.read_text()) if p.exists() else []


def _save(name: str, rows: list) -> None:
    p = _path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rows, indent=0) + "\n")


def sample_from_candles(candles: list[tuple], entry: float, t_entry: float) -> dict | None:
    """candles: sorted (t_seconds, high, close). Max gain and end return within WINDOW after entry."""
    after = [c for c in candles if t_entry < c[0] <= t_entry + WINDOW]
    if not after or entry <= 0:
        return None
    return {"max_pct": max(c[1] for c in after) / entry - 1, "end_pct": after[-1][2] / entry - 1}


def score(samples: list[dict], tp: float) -> float:
    total = weight = 0.0
    for s in samples:
        w = s.get("w", 1.0)
        r = tp - COST if s["max_pct"] >= tp else s["end_pct"] - COST
        total += w * r
        weight += w
    return total / weight if weight else 0.0


def best_tp(samples: list[dict]) -> tuple[float, dict]:
    scores = {tp: score(samples, tp) for tp in CANDIDATES}
    return max(scores, key=scores.get), scores


class Learner:
    def __init__(self, client):
        self.c = client
        self.live = _load(SAMPLES)
        self.prior = _load(PRIOR)

    def _done(self) -> set[str]:
        return {s["key"] for s in self.live}

    def collect(self, state: dict) -> int:
        """Turn matured trades/skips (WINDOW after entry) into samples. Returns how many were added."""
        added, done, now = 0, self._done(), time.time()
        cands = []
        for t in state.get("trades", []):
            if t.get("mode") == "LIVE" and t.get("entry_price") and t.get("bought_at") and not t.get("rebuy_of"):
                cands.append((f"buy:{t['token_address']}:{t['bought_at']}", t, float(t["entry_price"]), t["bought_at"]))
        for s in state.get("skipped", []):
            if s.get("price_when_seen") and s.get("skipped_at"):
                # As if bought with the live entry rule: limit at the price seen +5%.
                cands.append((f"skip:{s['token_address']}:{s['skipped_at']}", s, float(s["price_when_seen"]) * 1.05,
                              s["skipped_at"]))
        for key, rec, entry, when in cands:
            if key in done:
                continue
            t0 = dt.datetime.fromisoformat(when.replace("Z", "").split(".")[0]).replace(tzinfo=dt.timezone.utc).timestamp()
            if now < t0 + WINDOW:
                continue  # not matured yet
            try:
                time.sleep(1.0)
                k = self.c.call_tool("get_token_ohlcv", {"token_address": rec["token_address"], "interval": "1m",
                                                         "limit": 200, "time_from": int(t0), "time_to": int(t0 + WINDOW + 60)})
            except Exception as e:
                log.warning("learner candles failed for %s: %s", rec.get("symbol"), e)
                continue
            candles = sorted(zip([x // 1000 for x in k.get("t", [])], k.get("h", []), k.get("c", [])))
            s = sample_from_candles(candles, entry, t0)
            if s:
                self.live.append({"key": key, "symbol": rec.get("symbol"), **{k2: round(v, 4) for k2, v in s.items()}})
                added += 1
        if added:
            self.live = self.live[-MAX_LIVE:]
            _save(SAMPLES, self.live)
        return added

    def choose(self) -> tuple[float, dict]:
        samples = [{**p, "w": 1.0} for p in self.prior] + [{**s, "w": LIVE_WEIGHT} for s in self.live]
        return best_tp(samples)


def build_prior(signals_path: str, out_dir: str | None = None) -> list[dict]:
    """One-off: turn saved past whale signals (with 1m candles) into prior samples."""
    rows = []
    for x in json.load(open(signals_path)):
        o = x.get("ohlcv") or {}
        if not o.get("t"):
            continue
        t0 = dt.datetime.fromisoformat(x["time"]).replace(tzinfo=dt.timezone.utc).timestamp()
        candles = sorted(zip([t // 1000 for t in o["t"]], o["h"], o["c"]))
        k = next((c for c in candles if c[0] >= t0 - 59), None)
        if not k:
            continue
        s = sample_from_candles(candles, k[2] * 1.05, k[0])
        if s:
            rows.append({k2: round(v, 4) for k2, v in s.items()})
    return rows
