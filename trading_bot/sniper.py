"""Whale-pop experiment: buy right after a tracked whale opens a position in a small coin,
then sell at a fixed gain. No stop-loss (Jake's call: these coins can dip and recover in seconds).

  python -m trading_bot.sniper --trades 4 --usd 5 --take-profit 25          # LIVE (real money)
  python -m trading_bot.sniper --trades 4 --usd 5 --take-profit 25 --dry-run

Watches Moby's whale feed every few seconds. A whale buy qualifies when it is:
  - a tracked wallet OPENING a position (action first_buy), at least $250,
  - in a coin with market cap $20k-$1M that Moby does not rate red,
  - seen within --max-age seconds of the trade, in a coin we haven't traded.
Then: quick_buy $usd, wait for the fill, place a take-profit order at entry * (1 + tp%).
Every trade is written to state/sniper.json (the main bot leaves these coins alone).
Stops when --trades buys are done, or immediately if a STOP / REMOTE_STOP file exists.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import time

from trading_bot.moby_client import MobyClient, MobyError
from trading_bot.settings import ROOT, load_env
from trading_bot.store import _path

log = logging.getLogger("sniper")
STATE = "sniper.json"


def load() -> dict:
    p = _path(STATE)
    return json.loads(p.read_text()) if p.exists() else {"trades": []}


def save(state: dict) -> None:
    p = _path(STATE)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, default=str) + "\n")
    tmp.replace(p)


def sniper_tokens() -> set[str]:
    """Coins owned by this experiment, so the main strategy ignores them."""
    return {t["token_address"] for t in load()["trades"]}


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stopped() -> bool:
    return any((ROOT / n).exists() for n in ("STOP", "REMOTE_STOP"))


class Sniper:
    def __init__(self, client, usd: float, tp_pct: float, max_age: float, dry_run: bool,
                 min_whale_usd: float = 250, min_mcap: float = 20_000, max_mcap: float = 1_000_000):
        self.c, self.usd, self.tp, self.max_age, self.dry_run = client, usd, tp_pct / 100, max_age, dry_run
        self.min_whale_usd, self.min_mcap, self.max_mcap = min_whale_usd, min_mcap, max_mcap
        self.state = load()
        self.seen: set[str] = set()

    def call(self, tool: str, args: dict | None = None):
        for _ in range(4):
            try:
                return self.c.call_tool(tool, args or {})
            except MobyError as e:
                if "rate limit" not in str(e):
                    raise
                time.sleep(4)
        raise MobyError(f"{tool}: still rate limited")

    def qualifies(self, row: dict, now: float) -> str | None:
        tok = row.get("token") or {}
        if row.get("transaction_type") != "buy" or row.get("action") != "first_buy":
            return "not an opening buy"
        if (row.get("amount_usd") or 0) < self.min_whale_usd:
            return "whale buy too small"
        mcap = row.get("token_market_cap") or 0
        if not (self.min_mcap <= mcap <= self.max_mcap):
            return "market cap out of range"
        if tok.get("safety_tier") == "red":
            return "red safety"
        if tok.get("token_address") in sniper_tokens():
            return "already traded"
        age = now - dt.datetime.fromisoformat(row["timestamp"]).replace(tzinfo=dt.timezone.utc).timestamp()
        if age > self.max_age:
            return f"too old ({age:.0f}s)"
        return None

    def wait_fill(self, buy: dict) -> dict:
        """Poll until the buy is confirmed; return the swap status (or the buy itself if already filled)."""
        if buy.get("filled_output_amount_ui") and not buy.get("fill_pending"):
            return buy
        qid = buy.get("quote_id")
        for _ in range(20):
            time.sleep(1.5)
            st = self.call("get_swap_status", {"request_id": qid})
            if st.get("status") in ("confirmed", "failed", "expired"):
                return st
        return {"status": "unknown"}

    def trade(self, row: dict, seen_at: float) -> dict:
        tok = row["token"]
        token, sym = tok["token_address"], tok.get("symbol")
        whale_ts = dt.datetime.fromisoformat(row["timestamp"]).replace(tzinfo=dt.timezone.utc).timestamp()
        rec = {"token_address": token, "symbol": sym, "whale_time": row["timestamp"],
               "whale_type": row.get("wallet_list_type"), "whale_usd": row.get("amount_usd"),
               "whale_mcap": row.get("token_market_cap"), "seen_after_s": round(seen_at - whale_ts, 1),
               "usd": self.usd, "take_profit_pct": self.tp * 100, "mode": "practice" if self.dry_run else "LIVE"}
        price_seen = self.call("get_token_price", {"token_address": token}).get("price_usd")
        rec["price_when_seen"] = price_seen
        if self.dry_run:
            rec.update(status="simulated", entry_price=price_seen, bought_at=_iso(_now()))
            return rec
        buy = self.call("quick_buy", {"output_token": token, "amount_usd": self.usd,
                                      "rationale": f"Whale-pop experiment: {row.get('wallet_list_type')} opened "
                                                   f"${row.get('amount_usd', 0):,.0f} in {sym}; sell at +{self.tp*100:.0f}%."})
        rec["buy_response"] = {k: buy.get(k) for k in ("status", "quote_id", "transaction_signature", "blocked", "reason")}
        if buy.get("blocked"):
            rec["status"] = f"blocked: {buy.get('reason')}"
            return rec
        fill = self.wait_fill(buy)
        got = float(fill.get("filled_output_amount_ui") or 0)
        spent = float(fill.get("filled_input_amount_ui") or self.usd)
        rec["bought_at"] = _iso(_now())
        rec["buy_after_whale_s"] = round(_now() - whale_ts, 1)
        if got <= 0:
            rec["status"] = f"buy not confirmed ({fill.get('status')})"
            return rec
        entry = spent / got
        rec.update(tokens=got, spent_usd=spent, entry_price=entry,
                   entry_vs_seen_pct=round((entry / price_seen - 1) * 100, 1) if price_seen else None)
        held = float(self.call("get_my_token_balance", {"token_address": token}).get("balance_token_amount") or got)
        tp_price = entry * (1 + self.tp)
        rec["take_profit_price"] = tp_price
        try:
            order = self.call("create_order", {"order_type": "tp_only", "token_address": token, "amount": held,
                                               "take_profit_price": tp_price})
            rec["tp_order"] = order
            if isinstance(order, dict) and "tp_below_market" in json.dumps(order):
                raise MobyError("tp_below_market")
            if isinstance(order, dict) and (order.get("blocked") or order.get("error")):
                raise MobyError(json.dumps(order)[:300])
            rec["status"] = "holding, take-profit set"
        except MobyError as e:
            if "tp_below_market" in str(e):  # already above the target: take it now
                rec["sell"] = self.call("quick_sell", {"token_address": token, "pct": 100})
                rec["status"] = "sold immediately (already past take-profit)"
            else:
                rec["status"] = f"bought, take-profit FAILED: {e}"
        return rec

    def retarget(self) -> None:
        """Move every open take-profit from this experiment to the current --take-profit level.
        A coin already at or above the new target is sold now."""
        orders = {o["id"]: o for o in (self.call("get_my_orders").get("orders") or [])}
        for rec in self.state["trades"]:
            if rec.get("mode") != "LIVE" or not rec.get("entry_price") or rec.get("closed"):
                continue
            token, sym = rec["token_address"], rec.get("symbol")
            old_id = (rec.get("tp_order") or {}).get("order_id")
            if old_id and old_id not in orders:
                rec["closed"] = "take-profit no longer open (filled or cancelled)"
                continue
            held = float(self.call("get_my_token_balance", {"token_address": token}).get("balance_token_amount") or 0)
            if held <= 0:
                rec["closed"] = "no tokens left"
                continue
            if old_id:
                self.call("cancel_order", {"order_id": old_id})
            target = rec["entry_price"] * (1 + self.tp)
            price = float(self.call("get_token_price", {"token_address": token}).get("price_usd") or 0)
            rec["take_profit_pct"], rec["take_profit_price"] = self.tp * 100, target
            if price >= target:
                rec["sell"] = self.call("quick_sell", {"token_address": token, "pct": 100})
                rec["status"] = rec["closed"] = f"sold at +{(price / rec['entry_price'] - 1) * 100:.0f}% when retargeting"
            else:
                rec["tp_order"] = self.call("create_order", {"order_type": "tp_only", "token_address": token,
                                                             "amount": held, "take_profit_price": target})
                rec["status"] = f"holding, take-profit moved to +{self.tp * 100:.0f}%"
            log.info("%s: %s", sym, rec["status"])
            save(self.state)

    def run(self, trades: int, poll: float, hours: float) -> None:
        done = sum(1 for t in self.state["trades"] if t.get("mode") == ("practice" if self.dry_run else "LIVE"))
        deadline = _now() + hours * 3600
        after = _iso(_now() - 60)
        log.info("watching whale feed: %d/%d trades done", done, trades)
        while done < trades and _now() < deadline:
            if stopped():
                log.info("STOP file present: exiting")
                return
            try:
                feed = self.call("get_whalewatch_feed_window", {"after_timestamp": after, "transaction_types": ["buy"],
                                                                 "limit": 100})
            except Exception as e:
                log.warning("feed error: %s", e)
                time.sleep(poll * 2)
                continue
            now = _now()
            rows = [r for r in feed.get("transactions", []) if r["id"] not in self.seen]
            for r in rows:
                self.seen.add(r["id"])
            for r in sorted(rows, key=lambda r: r["timestamp"]):
                if self.qualifies(r, now) is None and done < trades:
                    log.info("whale %s opened $%.0f in %s (mcap $%.0f): buying", r.get("wallet_list_type"),
                             r.get("amount_usd", 0), r["token"].get("symbol"), r.get("token_market_cap", 0))
                    rec = self.trade(r, now)
                    self.state["trades"].append(rec)
                    save(self.state)
                    log.info("trade: %s", json.dumps({k: v for k, v in rec.items() if k not in ("tp_order", "sell")}, default=str))
                    done += 1
            after = _iso(now - 30)
            time.sleep(poll)
        log.info("finished: %d trades", done)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", type=int, default=4)
    ap.add_argument("--usd", type=float, default=5)
    ap.add_argument("--take-profit", type=float, default=25)
    ap.add_argument("--max-age", type=float, default=20, help="ignore whale buys older than this many seconds")
    ap.add_argument("--poll", type=float, default=3)
    ap.add_argument("--hours", type=float, default=20)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--retarget", action="store_true",
                    help="move open take-profits to --take-profit, then keep trading")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_env()
    s = Sniper(MobyClient(), a.usd, a.take_profit, a.max_age, a.dry_run)
    if a.retarget and not a.dry_run:
        s.retarget()
    s.run(a.trades, a.poll, a.hours)


if __name__ == "__main__":
    main()
