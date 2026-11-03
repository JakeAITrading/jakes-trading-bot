"""Was the chart check right? Replays every coin the whale trader SKIPPED as if it had bought,
and compares with the coins it did buy. Read-only: it only reads prices.

  python -m trading_bot.review                 # +15% target (current), 6h window
  python -m trading_bot.review --tp 10

For each skipped coin: a limit buy at the price seen +5% that fills only if the price trades there
within 2 minutes, then sell at the target; otherwise it's marked at the latest price.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics as st
import time

from trading_bot.moby_client import MobyClient
from trading_bot.settings import load_env
from trading_bot.sniper import load


def _ts(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace("Z", "").split(".")[0]).replace(tzinfo=dt.timezone.utc).timestamp()


def what_if(candles: list[tuple], t0: float, seen: float, tp: float, cushion: float = 0.05,
            wait_s: int = 120, hold_s: int = 6 * 3600, cost: float = 0.03) -> dict:
    """candles: sorted (t_seconds, open, high, low, close). Returns outcome of a hypothetical trade."""
    limit = seen * (1 + cushion)
    fill = next((c for c in candles if t0 <= c[0] <= t0 + wait_s + 60 and c[3] <= limit), None)
    if not fill:
        return {"outcome": "limit would not fill", "pnl_pct": 0.0}
    # A resting limit fills at the limit when the price falls to it, or at the open if already below.
    # (Using the candle's close was wrong: it credited the trade with the bottom of a crash candle.)
    entry = min(limit, fill[1])
    after = [c for c in candles if fill[0] < c[0] <= fill[0] + hold_s]
    for c in after:
        if c[2] >= entry * (1 + tp):
            return {"outcome": f"hit +{tp * 100:.0f}%", "pnl_pct": round((tp - cost) * 100, 1)}
    last = (after or [fill])[-1][4]
    best = max((c[2] for c in after), default=entry)
    return {"outcome": "still open" if after and after[-1][0] > time.time() - 300 else "not hit in 6h",
            "pnl_pct": round((last / entry - 1 - cost) * 100, 1), "best_pct": round((best / entry - 1) * 100, 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tp", type=float, default=15)
    a = ap.parse_args()
    load_env()
    c, state = MobyClient(), load()
    rows = []
    for s in state.get("skipped", []):
        t0, seen = _ts(s["skipped_at"]) - 5, s.get("price_when_seen")
        if not seen:
            continue
        time.sleep(1.2)
        k = c.call_tool("get_token_ohlcv", {"token_address": s["token_address"], "interval": "1m", "limit": 400,
                                            "time_from": int(t0 - 60), "time_to": int(time.time())})
        candles = sorted(zip([t // 1000 for t in k.get("t", [])], k.get("o", []), k.get("h", []), k.get("l", []),
                             k.get("c", [])))
        r = what_if(candles, t0, seen, a.tp / 100)
        rows.append({**r, "symbol": s["symbol"], "when": s["skipped_at"][11:16], "why": s["status"].replace("skipped: ", "")})
    print(f"Skipped coins, if bought anyway (limit +5%, sell +{a.tp:.0f}%, 3% costs):")
    for r in rows:
        extra = f" (best {r['best_pct']:+.0f}%)" if "best_pct" in r else ""
        print(f"  {r['when']} {r['symbol']:<10} {r['why']:<42} -> {r['outcome']:<22} {r['pnl_pct']:+6.1f}%{extra}")
    filled = [r["pnl_pct"] for r in rows if r["outcome"] != "limit would not fill"]
    if filled:
        print(f"\nSkips that would have filled: {len(filled)} of {len(rows)} | avg {st.mean(filled):+.1f}% | "
              f"hit target {sum(1 for r in rows if r['outcome'].startswith('hit'))}")
    bought = [t for t in state["trades"] if t.get("entry_price") and not t.get("rebuy_of")]
    wins = [t for t in bought if t.get("profit_usd") is not None]
    print(f"Coins actually bought (first buys): {len(bought)} | sold at target {len(wins)}")


if __name__ == "__main__":
    main()
