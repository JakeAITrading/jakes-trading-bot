"""Self-learning for the main (leaderboard) strategy.

Every cycle the main bot logs the leaderboard rows it sees (traded or not) with the features it screens
on. Four hours later each observation becomes a sample: the price path in 5-minute steps relative to
the price when seen, so stop-loss vs take-profit order is known. Every 30 minutes the learner searches
for the screening thresholds, stop-loss and take-profit with the best expected total profit and writes
them to state/main_params.json, which run.py applies on top of config.yaml.
"""
from __future__ import annotations

import json
import logging
import time

from trading_bot.store import _path

log = logging.getLogger("main_learner")

COST = 0.02            # bigger, more liquid coins: fees + slippage per round trip
WINDOW = 4 * 3600
STEP = 300             # 5-minute candles
MIN_TRADES = 15
OBSERVED, SAMPLES, PARAMS = "main_observed.json", "main_learning.json", "main_params.json"
LEARN_EVERY = 1800

GRID = {
    "take_profit_pct": [10, 15, 20, 30, 50, 100],
    "stop_loss_pct": [10, 15, 25, 40, 60],
    "min_price_change_h1_pct": [-100, 0, 2, 5],
    "min_price_change_h24_pct": [-100, 0, 10],
    "max_price_change_h24_pct": [50, 100, 300, 1e9],
    "min_whale_net_flow_h1_usd": [-1e12, 0, 1000, 5000],
    "min_whale_net_flow_h4_usd": [-1e12, 0, 5000, 20000],
    "min_whale_buyers_h4": [0, 3, 5, 10],
    "min_liquidity_usd": [50_000, 100_000, 200_000, 500_000],
    "min_market_cap_usd": [100_000, 1_000_000, 5_000_000],
    "max_market_cap_usd": [10_000_000, 100_000_000, 500_000_000, 1e15],
    "green_only": [True, False],
}


def _load(name: str, default):
    p = _path(name)
    return json.loads(p.read_text()) if p.exists() else default


def _save(name: str, obj) -> None:
    p = _path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, separators=(",", ":")) + "\n")


def features(row: dict) -> dict:
    w = row.get("whale") or {}
    h1, h4 = w.get("h1") or {}, w.get("h4") or {}
    return {"mcap": row.get("market_cap_usd"), "liq": row.get("liquidity_usd"),
            "h1": row.get("price_change_pct_h1"), "h24": row.get("price_change_pct_h24"),
            "wf1": h1.get("whale_net_flow_usd"), "wf4": h4.get("whale_net_flow_usd"),
            "wb4": h4.get("whale_buyers_count"), "safety": row.get("safety_tier")}


def observe(rows: list[dict], now: float | None = None) -> int:
    """Log leaderboard rows (one per coin per 30 minutes)."""
    now = now or time.time()
    obs = _load(OBSERVED, [])
    recent = {(o["token_address"]) for o in obs if now - o["t"] < 1800}
    added = 0
    for r in rows:
        tok, price = r.get("token_address"), r.get("price_usd")
        if not tok or not price or tok in recent:
            continue
        obs.append({"token_address": tok, "symbol": r.get("symbol"), "t": now, "price": price, **features(r)})
        added += 1
    if added:
        _save(OBSERVED, obs[-5000:])
    return added


def passes(s: dict, p: dict) -> bool:
    def v(k):
        x = s.get(k)
        return 0.0 if x is None else float(x)
    if p["green_only"] and s.get("safety") != "green":
        return False
    if s.get("safety") == "red":
        return False
    return (p["min_market_cap_usd"] <= v("mcap") <= p["max_market_cap_usd"]
            and v("liq") >= p["min_liquidity_usd"]
            and v("h1") > p["min_price_change_h1_pct"] and v("h24") > p["min_price_change_h24_pct"]
            and v("h24") <= p["max_price_change_h24_pct"]
            and v("wf1") >= p["min_whale_net_flow_h1_usd"] and v("wf4") >= p["min_whale_net_flow_h4_usd"]
            and v("wb4") >= p["min_whale_buyers_h4"])


def trade_return(s: dict, p: dict) -> float:
    tp, sl = p["take_profit_pct"] / 100, p["stop_loss_pct"] / 100
    for hi, lo in s["path"]:
        if lo <= 1 - sl:          # assume the stop fires first inside a step (conservative)
            return -sl - COST
        if hi >= 1 + tp:
            return tp - COST
    return s["end"] - 1 - COST


def evaluate(samples: list[dict], p: dict) -> dict:
    rs = [trade_return(s, p) for s in samples if passes(s, p)]
    return {"total": sum(rs), "trades": len(rs), "avg": sum(rs) / len(rs) if rs else 0.0}


def optimize(samples: list[dict], start: dict, rounds: int = 3) -> tuple[dict, dict]:
    p = dict(start)
    best = evaluate(samples, p)
    for _ in range(rounds):
        changed = False
        for key, values in GRID.items():
            for v in values:
                q = {**p, key: v}
                if q["min_market_cap_usd"] >= q["max_market_cap_usd"]:
                    continue
                r = evaluate(samples, q)
                if r["trades"] >= MIN_TRADES and r["total"] > best["total"] + 1e-9:
                    p, best, changed = q, r, True
        if not changed:
            break
    return p, best


def start_params(cfg: dict) -> dict:
    s, r = cfg.get("strategy", {}), cfg.get("risk", {})
    return {"take_profit_pct": r.get("take_profit_pct", 50), "stop_loss_pct": r.get("stop_loss_pct", 25),
            "min_price_change_h1_pct": s.get("min_price_change_h1_pct", 0),
            "min_price_change_h24_pct": s.get("min_price_change_h24_pct", 0),
            "max_price_change_h24_pct": s.get("max_price_change_h24_pct", 100),
            "min_whale_net_flow_h1_usd": s.get("min_whale_net_flow_h1_usd", 1000),
            "min_whale_net_flow_h4_usd": s.get("min_whale_net_flow_h4_usd", 5000),
            "min_whale_buyers_h4": s.get("min_whale_buyers_h4", 3),
            "min_liquidity_usd": s.get("min_liquidity_usd", 200_000),
            "min_market_cap_usd": s.get("min_market_cap_usd", 1_000_000),
            "max_market_cap_usd": s.get("max_market_cap_usd", 500_000_000), "green_only": True}


def apply(cfg: dict) -> dict:
    """Overlay learned settings (if any) on config.yaml values."""
    learned = _load(PARAMS, {}).get("params")
    if not learned:
        return cfg
    s, r = cfg.setdefault("strategy", {}), cfg.setdefault("risk", {})
    for k in ("take_profit_pct", "stop_loss_pct"):
        r[k] = learned[k]
    for k, v in learned.items():
        if k not in ("take_profit_pct", "stop_loss_pct"):
            s[k] = v
    return cfg


def collect(client, max_new: int = 25) -> int:
    obs, samples = _load(OBSERVED, []), _load(SAMPLES, [])
    done = {x["key"] for x in samples}
    now, added = time.time(), 0
    for o in obs:
        key = f"{o['token_address']}:{int(o['t'])}"
        if key in done or now < o["t"] + WINDOW + STEP:
            continue
        try:
            time.sleep(1.0)
            k = client.call_tool("get_token_ohlcv", {"token_address": o["token_address"], "interval": "5m",
                                                     "limit": 60, "time_from": int(o["t"]),
                                                     "time_to": int(o["t"] + WINDOW + STEP)})
        except Exception as e:
            log.warning("main learner candles failed for %s: %s", o.get("symbol"), e)
            continue
        c = sorted(zip([x // 1000 for x in k.get("t", [])], k.get("h", []), k.get("l", []), k.get("c", [])))
        c = [x for x in c if o["t"] <= x[0] + STEP and x[0] <= o["t"] + WINDOW]
        done.add(key)
        if c:
            samples.append({"key": key, "symbol": o.get("symbol"),
                            **{f: o.get(f) for f in ("mcap", "liq", "h1", "h24", "wf1", "wf4", "wb4", "safety")},
                            "path": [[round(h / o["price"], 4), round(lo / o["price"], 4)] for _, h, lo, _ in c],
                            "end": round(c[-1][3] / o["price"], 4)})
            added += 1
        if added >= max_new:
            break
    if added:
        _save(SAMPLES, samples[-3000:])
    return added


def maybe_learn(client, cfg: dict) -> dict | None:
    """Run at most every LEARN_EVERY seconds: collect matured samples, optimise, save params."""
    state = _load(PARAMS, {})
    if time.time() - state.get("learned_at_ts", 0) < LEARN_EVERY:
        return None
    added = collect(client)
    samples = _load(SAMPLES, [])
    start = state.get("params") or start_params(cfg)
    if len(samples) < MIN_TRADES * 2:
        state.update(learned_at_ts=time.time(), note=f"waiting for data: {len(samples)} samples")
        _save(PARAMS, state)
        return None
    p, exp = optimize(samples, start)
    hist = state.get("history", [])
    if p != state.get("params"):
        hist.append({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "params": p,
                     "expected_avg_pct": round(exp["avg"] * 100, 1), "trades_in_samples": exp["trades"],
                     "samples": len(samples)})
        log.info("main strategy learned: %s (expected %+.1f%%/trade over %d samples)", p, exp["avg"] * 100, len(samples))
    state.update(params=p, expected=exp, learned_at_ts=time.time(), samples=len(samples), new=added,
                 history=hist[-100:], note=None)
    _save(PARAMS, state)
    return p
