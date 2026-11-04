"""Self-learning for the whale trader: every setting is learned from outcomes.

Every whale buy the bot sees is logged (traded or not). Two hours later the learner looks up what the
coin's price did and turns it into a sample: the price when seen, the low in the next 3 minutes, the
high and the end price over 2 hours, plus the features the bot can see at signal time (market cap,
whale size and type, how far the coin had already run, how hard it was spiking that minute).

From those samples it searches for the settings that would have made the most TOTAL profit:
  take_profit    sell target
  limit_cushion  limit buy at price seen + cushion (a sample only trades if its 3-minute low reached it)
  max_runup      skip coins already this far above their 5-minute low (None = off)
  max_spike      skip coins up more than this in the current minute (None = off)
  min_mcap / max_mcap, min_whale_usd, whale_type
If no combination is expected to make money, the best "settings" are ones that trade almost nothing,
so the bot mostly stops buying. A share of signals that fail the rules is still traded (exploration)
so the learner keeps seeing the trades it would otherwise never learn about.

Prior samples (past whale signals) seed it; live samples weigh LIVE_WEIGHT times more.
"""
from __future__ import annotations

import datetime as dt
import itertools
import json
import logging
import time

from trading_bot.store import _path

log = logging.getLogger("learner")

COST = 0.03            # fees + slippage per round trip
WINDOW = 2 * 3600      # follow each sample this long after the signal
FILL_WINDOW = 180      # a limit buy must fill within ~3 minutes (2-minute order + 1-minute candle)
LIVE_WEIGHT = 3.0
MAX_LIVE = 600
MIN_TRADES = 12        # a rule set must keep at least this much (weighted) sample to be trusted
SAMPLES, PRIOR = "learning.json", "learning_prior.json"

GRID = {
    "take_profit": [0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50],
    "limit_cushion": [0.0, 0.02, 0.05, 0.10, 0.20],
    "max_runup": [None, 0.5, 1.0, 2.0, 4.0],
    "max_spike": [None, 0.10, 0.20, 0.40],
    "min_mcap": [20_000, 50_000, 100_000, 200_000],
    "max_mcap": [250_000, 500_000, 1_000_000, 10_000_000],
    "min_whale_usd": [100, 250, 500, 1000, 2500],
    "whale_type": ["any", "whale", "kol"],
    "require_known": [False, True],          # skip whales Moby has no track record for
    "min_wallet_win": [0.0, 0.3, 0.4, 0.5, 0.6],  # whale's 30-day win rate
    "max_wallets30": [None, 1, 2, 3],        # tracked wallets that bought the coin in the last 30 min
    "min_views": [0, 50, 100, 250, 500],     # Moby page views (attention)
}
DEFAULTS = {"take_profit": 0.10, "limit_cushion": 0.05, "max_runup": None, "max_spike": 0.20,
            "min_mcap": 20_000, "max_mcap": 1_000_000, "min_whale_usd": 250, "whale_type": "any",
            "require_known": False, "min_wallet_win": 0.0, "max_wallets30": None, "min_views": 0}


def _load(name: str):
    p = _path(name)
    return json.loads(p.read_text()) if p.exists() else []


def _save(name: str, rows) -> None:
    p = _path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rows, separators=(",", ":")) + "\n")


def _ts(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace("Z", "").split(".")[0]).replace(tzinfo=dt.timezone.utc).timestamp()


# ---------------------------------------------------------------- whale wallet records
_WALLETS: dict = {}


def wallet_stats(client, wallet: str | None, max_age: float = 6 * 3600) -> dict:
    """30-day record of the whale's wallet from Moby (cached). known=False when Moby has none."""
    if not wallet:
        return {}
    hit = _WALLETS.get(wallet)
    if hit and time.time() - hit["at"] < max_age:
        return hit
    try:
        r = client.call_tool("get_whale_wallet_track_record", {"wallet_address": wallet})
        d30 = ((r.get("profile") or {}).get("stats") or {}).get("d30") or {}
        out = {"known": bool(d30), "win": d30.get("win_rate"), "roi": d30.get("roi_pct"), "at": time.time()}
    except Exception as e:
        log.warning("wallet record failed: %s", e)
        return {}
    _WALLETS[wallet] = out
    return out


# ---------------------------------------------------------------- samples
def sample_from_candles(candles: list[tuple], t0: float) -> dict | None:
    """candles: sorted (t_s, open, high, low, close) from ~6 min before the signal to 2h after.
    Everything is relative to the price when seen (close of the signal's minute)."""
    cur = next((c for c in candles if c[0] <= t0 < c[0] + 60), None) or next((c for c in candles if c[0] >= t0), None)
    if not cur:
        return None
    seen = cur[4]
    pre = [c for c in candles if t0 - 300 <= c[0] <= cur[0]]
    after = [c for c in candles if cur[0] < c[0] <= t0 + WINDOW]
    soon = [c for c in after if c[0] <= t0 + FILL_WINDOW]
    if seen <= 0 or not after:
        return None
    low5 = min(c[3] for c in pre) if pre else seen
    return {"runup": seen / low5 - 1 if low5 else 0.0, "spike": seen / cur[1] - 1 if cur[1] else 0.0,
            "low3": min([c[3] for c in soon] + [seen]) / seen,
            "high": max(c[2] for c in after) / seen, "end": after[-1][4] / seen}


def trade_return(s: dict, p: dict) -> float | None:
    """Return of one sample under settings p, or None if the limit buy wouldn't fill."""
    entry = 1 + p["limit_cushion"]
    if s["low3"] > entry:
        return None
    if s["high"] >= entry * (1 + p["take_profit"]):
        return p["take_profit"] - COST
    return s["end"] / entry - 1 - COST


def passes(s: dict, p: dict) -> bool:
    if p["max_runup"] is not None and s.get("runup") is not None and s["runup"] > p["max_runup"]:
        return False
    if p["max_spike"] is not None and s.get("spike") is not None and s["spike"] > p["max_spike"]:
        return False
    m = s.get("mcap")
    if m is not None and not (p["min_mcap"] <= m <= p["max_mcap"]):
        return False
    w = s.get("whale_usd")
    if w is not None and w < p["min_whale_usd"]:
        return False
    if p["whale_type"] != "any" and s.get("whale_type") not in (None, p["whale_type"]):
        return False
    known = s.get("wallet_known")            # None = sample from before wallet records were logged
    if p.get("require_known") and known is False:
        return False
    win = s.get("wallet_win")
    if p.get("min_wallet_win") and known is not None and (win is None or win < p["min_wallet_win"]):
        return False
    n = s.get("n_wallets30")
    if p.get("max_wallets30") is not None and n is not None and n > p["max_wallets30"]:
        return False
    v = s.get("views")
    if p.get("min_views") and v is not None and v < p["min_views"]:
        return False
    return True


def evaluate(samples: list[dict], p: dict) -> dict:
    total = n = 0.0
    for s in samples:
        if not passes(s, p):
            continue
        r = trade_return(s, p)
        if r is None:
            continue
        w = s.get("w", 1.0)
        total += w * r
        n += w
    return {"total": total, "trades": n, "avg": total / n if n else 0.0}


def optimize(samples: list[dict], start: dict | None = None, rounds: int = 3) -> tuple[dict, dict]:
    """Coordinate search for the settings with the highest total return. take_profit and limit_cushion
    are searched together; then each filter in turn. Rule sets keeping fewer than MIN_TRADES are ignored
    unless nothing else is possible."""
    p = dict(start or DEFAULTS)

    def ok(r):
        return r["trades"] >= MIN_TRADES

    best = evaluate(samples, p)
    for _ in range(rounds):
        changed = False
        for tp, cu in itertools.product(GRID["take_profit"], GRID["limit_cushion"]):
            q = {**p, "take_profit": tp, "limit_cushion": cu}
            r = evaluate(samples, q)
            if ok(r) and r["total"] > best["total"] + 1e-9:
                p, best, changed = q, r, True
        for key in ("max_runup", "max_spike", "min_mcap", "max_mcap", "min_whale_usd", "whale_type",
                    "require_known", "min_wallet_win", "max_wallets30", "min_views"):
            for v in GRID[key]:
                q = {**p, key: v}
                if q["min_mcap"] >= q["max_mcap"]:
                    continue
                r = evaluate(samples, q)
                if ok(r) and r["total"] > best["total"] + 1e-9:
                    p, best, changed = q, r, True
        if not changed:
            break
    return p, best


# ---------------------------------------------------------------- learner
class Learner:
    def __init__(self, client):
        self.c = client
        self.live = _load(SAMPLES)
        self.prior = _load(PRIOR)
        self.params = dict(DEFAULTS)
        self.expected: dict = {}

    def collect(self, state: dict, max_new: int = 40) -> int:
        """Turn matured observations (whale buys seen, traded or not) into samples."""
        done = {s["key"] for s in self.live}
        now, added = time.time(), 0
        for o in state.get("observed", []):
            key = f"{o['token_address']}:{o['ts']}"
            if key in done or now < o["t"] + WINDOW + 60:
                continue
            try:
                time.sleep(1.0)
                k = self.c.call_tool("get_token_ohlcv", {"token_address": o["token_address"], "interval": "1m",
                                                         "limit": 200, "time_from": int(o["t"] - 400),
                                                         "time_to": int(o["t"] + WINDOW + 60)})
            except Exception as e:
                log.warning("learner candles failed for %s: %s", o.get("symbol"), e)
                continue
            candles = sorted(zip([x // 1000 for x in k.get("t", [])], k.get("o", []), k.get("h", []),
                                 k.get("l", []), k.get("c", [])))
            s = sample_from_candles(candles, o["t"])
            done.add(key)
            if s:
                ws = wallet_stats(self.c, o.get("wallet")) if o.get("wallet") else {}
                self.live.append({"key": key, "symbol": o.get("symbol"), "mcap": o.get("mcap"),
                                  "whale_usd": o.get("whale_usd"), "whale_type": o.get("whale_type"),
                                  "wallet_known": ws.get("known"), "wallet_win": ws.get("win"),
                                  "n_wallets30": o.get("n_wallets30"), "views": o.get("views"),
                                  **{k2: round(v, 4) for k2, v in s.items()}})
                added += 1
            if added >= max_new:
                break
        if added:
            self.live = self.live[-MAX_LIVE:]
            _save(SAMPLES, self.live)
        return added

    def choose(self) -> tuple[dict, dict]:
        samples = [{**s, "w": 1.0} for s in self.prior] + [{**s, "w": LIVE_WEIGHT} for s in self.live]
        self.params, self.expected = optimize(samples, self.params)
        return self.params, self.expected


def build_prior(signals_path: str) -> list[dict]:
    """One-off: past whale signals (saved with 1-minute candles) -> prior samples."""
    rows = []
    for x in json.load(open(signals_path)):
        o = x.get("ohlcv") or {}
        if not o.get("t"):
            continue
        t0 = dt.datetime.fromisoformat(x["time"]).replace(tzinfo=dt.timezone.utc).timestamp()
        candles = sorted(zip([t // 1000 for t in o["t"]], o["o"], o["h"], o["l"], o["c"]))
        s = sample_from_candles(candles, t0)
        if s:
            kinds = x.get("kinds") or []
            rows.append({"mcap": x.get("mcap"), "whale_usd": None,
                         "whale_type": kinds[0] if len(kinds) == 1 else None,
                         **{k: round(v, 4) for k, v in s.items()}})
    return rows
