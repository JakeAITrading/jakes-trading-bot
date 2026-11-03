"""Whale-pop experiment: buy right after a tracked whale opens a position in a small coin,
then sell at a fixed gain. No stop-loss (Jake's call: these coins can dip and recover in seconds).

  python -m trading_bot.sniper --trades 4 --usd 5 --take-profit 15          # LIVE (real money)
  python -m trading_bot.sniper --trades 4 --usd 5 --take-profit 15 --dry-run

Watches Moby's whale feed every few seconds. A whale buy qualifies when it is:
  - a tracked wallet OPENING a position (action first_buy), at least $250,
  - in a coin with market cap $20k-$1M that Moby does not rate red,
  - seen within --max-age seconds of the trade, in a coin we haven't traded.
Before buying it glances at the chart (--max-runup, --max-spike) and skips coins that already
pumped. Then: a LIMIT buy at the price seen +5% (cancelled after 2 min if not filled), so it never
pays for the spike (--market-buy uses quick_buy instead). Once filled, place a take-profit order at
entry * (1 + tp%).
When a take-profit fills it buys the same coin right back with a fresh take-profit (the profit is
kept; --rebuy-if-rising only re-buys while the price is at/above the sale with a green last minute),
once per sale (no cap on how many times a coin can cycle; --max-rebuys sets one). Re-buys don't
count toward --trades.
The take-profit is self-learning (trading_bot/learner.py): every 10 minutes it scores each candidate
target on how high bought and skipped coins went in the 2 hours after entry, adopts the best one and
moves open take-profits to it. --no-learn keeps --take-profit fixed.
Every trade is written to state/sniper.json (the main bot leaves these coins alone).
Stops when --trades buys are done, or immediately if a STOP / REMOTE_STOP file exists.
"""
from __future__ import annotations

import argparse
import random
import datetime as dt
import json
import re
import logging
import time

from trading_bot.learner import Learner

OBSERVE_MCAP = (5_000, 10_000_000)   # whale buys logged for learning (wider than what we trade)
OBSERVE_MIN_USD = 100
from trading_bot.moby_client import blocked_until, MobyClient, MobyError
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
        self.max_rebuys, self.rebuy_window = 0, 600  # 0 = no cap per coin; look for 10 min after a sale
        self.max_open, self.cash_floor = 5, 10.0       # our own limits (Moby's are off)
        self.look_first, self.max_runup, self.max_spike = True, 1.0, 0.20
        self.rebuy_always = True
        self.rebuy_enabled = True   # --no-rebuy: take the profit and keep the cash for the next whale signal
        self.limit_cushion, self.limit_wait = 0.05, 120  # whale buys: limit at seen +5%, cancel after 2 min
        self.learner, self.learn_every, self._last_learn = None, 600, 0.0   # self-learning settings
        self.whale_type, self.explore = "any", 0.2   # explore: share of rule-failing signals traded anyway

    WRITE_TOOLS = ("create_order", "cancel_order", "quick_buy", "quick_sell")

    def call(self, tool: str, args: dict | None = None):
        until = float(self.state.get("orders_blocked_until") or 0)
        if tool in self.WRITE_TOOLS and _now() < until:
            raise MobyError(f"{tool}: Moby order limit hit; paused until {_iso(until)}")
        for _ in range(4):
            try:
                return self.c.call_tool(tool, args or {})
            except MobyError as e:
                if "rate limit" not in str(e):
                    raise
                # Moby sometimes says "STOP calling Moby tools for about N minutes (S seconds)": retrying
                # sooner only extends it, so pause order tools until then (reads keep working).
                m = re.search(r"\((\d+) seconds\)", str(e))
                if m and int(m.group(1)) > 60:
                    self.state["orders_blocked_until"] = _now() + int(m.group(1)) + 60
                    save(self.state)
                    log.warning("Moby order limit hit: pausing order tools until %s", _iso(self.state["orders_blocked_until"]))
                    raise MobyError(f"{tool}: Moby order limit hit; paused until {_iso(self.state['orders_blocked_until'])}")
                time.sleep(4)
        raise MobyError(f"{tool}: still rate limited")

    def qualifies(self, row: dict, now: float) -> str | None:
        """None = trade. Reasons starting with "rule:" come from learned settings; exploration may
        override those. Other reasons are hard limits."""
        tok = row.get("token") or {}
        if row.get("transaction_type") != "buy" or row.get("action") != "first_buy":
            return "not an opening buy"
        if tok.get("safety_tier") == "red":
            return "red safety"
        if tok.get("token_address") in sniper_tokens():
            return "already traded"
        age = now - dt.datetime.fromisoformat(row["timestamp"]).replace(tzinfo=dt.timezone.utc).timestamp()
        if age > self.max_age:
            return f"too old ({age:.0f}s)"
        mcap = row.get("token_market_cap") or 0
        if not (OBSERVE_MCAP[0] <= mcap <= OBSERVE_MCAP[1]) or (row.get("amount_usd") or 0) < OBSERVE_MIN_USD:
            return "outside what we observe"
        if (row.get("amount_usd") or 0) < self.min_whale_usd:
            return "rule: whale buy too small"
        if not (self.min_mcap <= mcap <= self.max_mcap):
            return "rule: market cap out of range"
        if self.whale_type != "any" and row.get("wallet_list_type") != self.whale_type:
            return f"rule: not a {self.whale_type} buy"
        return None

    def observe(self, row: dict) -> None:
        """Log every whale opening buy we can see, traded or not, so the learner can study it later."""
        tok = row.get("token") or {}
        mcap = row.get("token_market_cap") or 0
        if (row.get("transaction_type") != "buy" or row.get("action") != "first_buy" or tok.get("safety_tier") == "red"
                or not (OBSERVE_MCAP[0] <= mcap <= OBSERVE_MCAP[1]) or (row.get("amount_usd") or 0) < OBSERVE_MIN_USD):
            return
        t = dt.datetime.fromisoformat(row["timestamp"]).replace(tzinfo=dt.timezone.utc).timestamp()
        obs = self.state.setdefault("observed", [])
        if any(o["token_address"] == tok.get("token_address") and abs(o["t"] - t) < 600 for o in obs[-200:]):
            return  # one sample per coin per 10 minutes
        obs.append({"token_address": tok.get("token_address"), "symbol": tok.get("symbol"), "ts": row["timestamp"],
                    "t": t, "mcap": mcap, "whale_usd": row.get("amount_usd"), "whale_type": row.get("wallet_list_type")})
        self.state["observed"] = obs[-3000:]

    def glance(self, token: str) -> tuple[str | None, dict]:
        """Quick look at the chart before buying (one call, ~0.2s). Returns (reason to skip, numbers).
        Backtest on 120 whale signals: skipping coins already >100% above their 5-minute low cut the
        average loss from -16.5% to -6%; buying mid-spike (current minute sharply up) did worst."""
        c = self.call("get_token_ohlcv", {"token_address": token, "interval": "1m", "limit": 6,
                                          "include_in_progress_candle": True})
        if not c.get("c"):
            return "no chart data", {}
        price, cur_open = c["c"][0], c["o"][0]           # newest first; [0] is the live minute
        low5 = min(c["l"])
        info = {"above_5m_low_pct": round((price / low5 - 1) * 100, 1) if low5 else None,
                "this_minute_pct": round((price / cur_open - 1) * 100, 1) if cur_open else None}
        if self.max_runup is not None and low5 and price / low5 - 1 > self.max_runup:
            return f"already pumped: +{info['above_5m_low_pct']:.0f}% above 5-min low", info
        if self.max_spike is not None and cur_open and price / cur_open - 1 > self.max_spike:
            return f"mid-spike: +{info['this_minute_pct']:.0f}% this minute", info
        return None, info

    def views(self, token: str) -> int | None:
        """Moby's view count for the coin (people looking at it in the app). Logged only, after the
        buy/skip decision, so it never slows a buy; compared against outcomes later."""
        try:
            return (self.call("get_token_overview", {"token_address": token}).get("overview") or {}).get("view_count")
        except Exception:
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
        skip, rec["glance"] = self.glance(token) if self.look_first else (None, {})
        if skip and random.random() < self.explore:
            rec["explore"], skip = skip, None   # trade it anyway so the learner sees this kind of coin
        if skip:
            rec["status"] = f"skipped: {skip}"
            return rec
        if self.dry_run:
            rec.update(status="simulated", entry_price=price_seen, bought_at=_iso(_now()))
            return rec
        rationale = (f"Whale-pop experiment: {row.get('wallet_list_type')} opened "
                     f"${row.get('amount_usd', 0):,.0f} in {sym}; sell at +{self.tp*100:.0f}%.")
        if self.limit_cushion is not None and price_seen:
            return self._limit_buy(rec, token, float(price_seen), rationale)
        return self._buy(rec, token, sym, whale_ts, rationale)

    def _limit_buy(self, rec: dict, token: str, price_seen: float, rationale: str) -> dict:
        """Buy only at or below the price we saw + cushion, so we never chase the spike. The order
        rests on Moby; check_pending() places the take-profit when it fills, or cancels it after
        limit_wait seconds."""
        limit = price_seen * (1 + self.limit_cushion)
        try:
            order = self.call("create_order", {"order_type": "limit_buy", "token_address": token, "amount": self.usd,
                                               "limit_price": limit, "rationale": rationale})
        except MobyError as e:
            order = {"error": str(e)}
        oid = order.get("order_id") if isinstance(order, dict) else None
        if not oid and "above_market" in json.dumps(order):
            # Price is already at/below our limit, so Moby won't rest the order: that price is fine, buy now.
            rec["limit_note"] = "price already under limit; bought at market"
            return self._buy(rec, token, rec.get("symbol"), _now(), rationale)
        if not oid:
            rec["status"] = f"limit buy FAILED: {json.dumps(order)[:200]}"
            return rec
        rec.update(status="limit pending", limit_price=limit, limit_order_id=oid,
                   limit_placed_at=_iso(_now()), limit_expires=_now() + self.limit_wait)
        return rec

    def check_pending(self) -> None:
        """Limit buys: on fill, record entry and place the take-profit; past the wait, cancel."""
        pending = [r for r in self.state["trades"] if r.get("status") == "limit pending"]
        if not pending:
            return
        live = {o["id"]: o for o in (self.call("get_my_orders").get("orders") or [])}
        done = {o["id"]: o for o in (self.call("get_my_orders", {"status": "terminal"}).get("orders") or [])}
        for rec in pending:
            oid, token = rec["limit_order_id"], rec["token_address"]
            o = done.get(oid)
            if o and str(o.get("status", "")).startswith("filled"):
                held = float(self.call("get_my_token_balance", {"token_address": token}).get("balance_token_amount") or 0)
                entry = float(o.get("filled_price") or rec["limit_price"])
                spent = float(o.get("input_amount_usd") or self.usd)
                rec.update(entry_price=entry, spent_usd=spent, tokens=held, bought_at=o.get("filled_at") or _iso(_now()),
                           entry_vs_seen_pct=round((entry / rec["price_when_seen"] - 1) * 100, 1))
                self._place_tp(rec, token, entry, held)
                log.info("%s: limit buy filled at %+.1f%% vs seen; %s", rec["symbol"], rec["entry_vs_seen_pct"], rec["status"])
            elif o:
                rec["status"] = f"limit buy {o.get('status')} ({o.get('cancel_reason')})"
                rec["closed"] = rec["status"]
            elif oid in live and _now() > rec["limit_expires"]:
                self.call("cancel_order", {"order_id": oid})
                rec["status"] = rec["closed"] = "limit buy not filled in time, cancelled"
                # A limit can fill partly before the cancel; give whatever we got its take-profit.
                held = float(self.call("get_my_token_balance", {"token_address": token}).get("balance_token_amount") or 0)
                if held > 0:
                    entry = rec["limit_price"]
                    rec.pop("closed", None)
                    rec.update(entry_price=entry, spent_usd=round(held * entry, 4), tokens=held, partial_fill=True,
                               bought_at=_iso(_now()), entry_vs_seen_pct=round((entry / rec["price_when_seen"] - 1) * 100, 1))
                    self._place_tp(rec, token, entry, held)
                    rec["status"] = f"partly filled (${held * entry:.2f}) before cancel; {rec['status']}"
                log.info("%s: %s", rec["symbol"], rec["status"])
            save(self.state)

    def _buy(self, rec: dict, token: str, sym: str, t_ref: float, rationale: str) -> dict:
        """Buy $usd of token, wait for the fill, and place the take-profit. Fills in rec."""
        price_seen = rec.get("price_when_seen")
        buy = self.call("quick_buy", {"output_token": token, "amount_usd": self.usd, "rationale": rationale})
        rec["buy_response"] = {k: buy.get(k) for k in ("status", "quote_id", "transaction_signature", "blocked", "reason")}
        if buy.get("blocked"):
            rec["status"] = f"blocked: {buy.get('reason')}"
            return rec
        fill = self.wait_fill(buy)
        got = float(fill.get("filled_output_amount_ui") or 0)
        spent = float(fill.get("filled_input_amount_ui") or self.usd)
        rec["bought_at"] = _iso(_now())
        rec["buy_after_whale_s"] = round(_now() - t_ref, 1)
        if got <= 0:
            rec["status"] = f"buy not confirmed ({fill.get('status')})"
            return rec
        entry = spent / got
        rec.update(tokens=got, spent_usd=spent, entry_price=entry,
                   entry_vs_seen_pct=round((entry / price_seen - 1) * 100, 1) if price_seen else None)
        held = float(self.call("get_my_token_balance", {"token_address": token}).get("balance_token_amount") or got)
        return self._place_tp(rec, token, entry, held)

    def _place_tp(self, rec: dict, token: str, entry: float, held: float) -> dict:
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
            # Moby words this two ways: a "tp_below_market" code, or "must be above current price".
            if "tp_below_market" in str(e) or "must be above current price" in str(e):  # past target: take it now
                self._take_now(rec, token, held)
            else:
                rec["status"] = f"bought, take-profit FAILED: {e}"
        return rec

    def _take_now(self, rec: dict, token: str, held: float) -> None:
        """Price is already past the target. A market sell (quick_sell) costs ~$0.90 on Moby, a take-profit
        order ~0.75%, so first try a take-profit order just above the current price (it fills almost at
        once); only if Moby rejects that too, sell at market."""
        try:
            price = float(self.call("get_token_price", {"token_address": token}).get("price_usd") or 0)
            if price > 0:
                order = self.call("create_order", {"order_type": "tp_only", "token_address": token, "amount": held,
                                                   "take_profit_price": price * 1.005})
                if not (isinstance(order, dict) and (order.get("blocked") or order.get("error")
                                                     or "tp_below_market" in json.dumps(order))):
                    rec.update(tp_order=order, take_profit_price=price * 1.005,
                               status="holding, take-profit set just above market (already past target)")
                    return
        except MobyError as e:
            log.info("%s: near-market take-profit refused (%s); selling at market", rec.get("symbol"), e)
        sell = self.call("quick_sell", {"token_address": token, "pct": 100})
        got = float(sell.get("filled_output_amount_ui") or sell.get("quoted_output_amount_ui") or 0)
        rec.update(sell=sell, status="sold immediately (already past take-profit)", closed="sold past target",
                   proceeds_usd=got, profit_usd=round(got - rec.get("spent_usd", self.usd), 2) if got else None)

    def fix_missing_tp(self) -> None:
        """Retry the take-profit for coins we hold whose take-profit order failed (e.g. rate limited)."""
        for rec in self.state["trades"]:
            if (rec.get("mode") == "LIVE" and rec.get("entry_price") and not rec.get("closed")
                    and ("take-profit FAILED" in (rec.get("status") or "") or rec.get("tp_lost"))):
                token = rec["token_address"]
                held = float(self.call("get_my_token_balance", {"token_address": token}).get("balance_token_amount") or 0)
                if held <= 0:
                    rec["closed"] = "no tokens left"
                else:
                    rec.pop("tp_lost", None)
                    self._place_tp(rec, token, rec["entry_price"], held)
                log.info("%s: retried take-profit: %s", rec.get("symbol"), rec.get("status"))
                save(self.state)

    def open_count(self) -> int:
        return sum(1 for r in self.state["trades"]
                   if r.get("mode") == "LIVE" and (r.get("entry_price") or r.get("status") == "limit pending")
                   and not r.get("closed"))

    def can_buy(self) -> str | None:
        """Our own safeguards. None = OK to buy, else the reason not to."""
        if stopped():
            return "STOP file present"
        if _now() < float(self.state.get("orders_blocked_until") or 0):
            return f"Moby order limit: paused until {_iso(self.state['orders_blocked_until'])}"
        if self.open_count() >= self.max_open:
            return f"{self.max_open} whale coins already held"
        bal = self.call("get_my_balances")
        cash = float((bal.get("usd_token") or {}).get("quantity") or 0)
        if cash - self.usd < self.cash_floor:
            return f"cash ${cash:.2f} would fall below ${self.cash_floor:.0f} floor"
        return None

    def _rising(self, token: str, sold_price: float) -> tuple[bool, float]:
        """Still going up: price at or above where we sold AND the last closed 1-minute candle is green."""
        price = float(self.call("get_token_price", {"token_address": token}).get("price_usd") or 0)
        c = self.call("get_token_ohlcv", {"token_address": token, "interval": "1m", "limit": 2})
        green = bool(c.get("c")) and c["c"][0] > c["o"][0]  # newest first
        return price >= sold_price and green, price

    def check_rebuys(self) -> None:
        """Find take-profits that filled; if the coin is still rising, buy it again (same size, new +tp%)."""
        live = [r for r in self.state["trades"] if r.get("mode") == "LIVE" and r.get("entry_price")]
        if not any(not r.get("closed") or r.get("rebuy_until") for r in live):
            return
        open_ids = {o["id"] for o in (self.call("get_my_orders").get("orders") or [])}
        done = {o["id"]: o for o in (self.call("get_my_orders", {"status": "terminal"}).get("orders") or [])}
        for rec in live:
            oid = (rec.get("tp_order") or {}).get("order_id")
            if not rec.get("closed") and oid and oid not in open_ids and oid in done:
                o = done[oid]
                if str(o.get("status", "")).startswith("filled"):
                    sold = float(o.get("filled_price") or rec["take_profit_price"])
                    rec.update(closed=f"take-profit filled at {sold:.6g}", sold_price=sold,
                               proceeds_usd=o.get("filled_output_amount"),
                               profit_usd=round(float(o.get("filled_output_amount") or 0) - rec.get("spent_usd", self.usd), 2),
                               rebuy_until=(_now() + self.rebuy_window) if self.rebuy_enabled else None)
                    log.info("%s: take-profit filled, profit $%.2f", rec["symbol"], rec["profit_usd"])
                    save(self.state)
                else:
                    # Cancelled/expired but maybe still holding: fix_missing_tp re-places it (or closes at 0).
                    rec["tp_lost"] = True
                    rec["status"] = f"take-profit {o.get('status')}; re-placing"
            if not rec.get("rebuy_until"):
                continue
            if _now() > rec["rebuy_until"] or (self.max_rebuys and rec.get("rebuys", 0) >= self.max_rebuys):
                rec["rebuy_until"] = None
                save(self.state)
                continue
            if self.rebuy_always:   # Jake: buy right back after each sale, keep the profit
                price = float(self.call("get_token_price", {"token_address": rec["token_address"]}).get("price_usd") or 0)
            else:
                rising, price = self._rising(rec["token_address"], rec["sold_price"])
                if not rising:
                    continue
            why_not = self.can_buy()
            if why_not:
                log.info("%s still rising but no re-buy: %s", rec["symbol"], why_not)
                continue
            new = {"token_address": rec["token_address"], "symbol": rec["symbol"], "mode": "LIVE",
                   "rebuy_of": rec.get("bought_at"), "rebuys": rec.get("rebuys", 0) + 1,
                   "usd": self.usd, "take_profit_pct": self.tp * 100, "price_when_seen": price}
            log.info("%s sold at %.6g, now %.6g: buying again", rec["symbol"], rec["sold_price"], price)
            why = f"Re-buy: {rec['symbol']} after +{self.tp*100:.0f}% take-profit."
            if self.limit_cushion is not None:
                # Limit at the SALE price + cushion: a market re-buy into a spike filled 22% high (ANON).
                new["price_when_seen"] = rec["sold_price"]
                self._limit_buy(new, rec["token_address"], rec["sold_price"], why)
            else:
                self._buy(new, rec["token_address"], rec["symbol"], _now(), why)
            log.info("re-buy: %s", json.dumps({k: v for k, v in new.items() if k not in ("tp_order", "sell")}, default=str))
            if new.get("entry_price") or new.get("status") == "limit pending":
                # Bought (or limit resting): one re-buy per sale; the new position re-buys again only after
                # it sells at +tp%. An unfilled re-buy limit is cancelled after limit_wait like any other.
                rec["rebuy_until"] = None
                self.state["trades"].append(new)
            else:
                # Blocked or not filled: keep trying until the window ends (PEPE2 lost its re-buy this way).
                rec.setdefault("rebuy_attempts", []).append({"at": _iso(_now()), "status": new.get("status")})
            save(self.state)

    def retarget(self) -> None:
        """Move every open take-profit from this experiment to the current --take-profit level.
        A coin already at or above the new target is sold now."""
        orders = {o["id"]: o for o in (self.call("get_my_orders").get("orders") or [])}
        for rec in self.state["trades"]:
            if rec.get("mode") != "LIVE" or not rec.get("entry_price") or rec.get("closed") or rec.get("keep_tp"):
                continue
            token, sym = rec["token_address"], rec.get("symbol")
            old_id = (rec.get("tp_order") or {}).get("order_id")
            if old_id and old_id not in orders:
                # Filled -> closed. But a take-profit can also vanish (cancelled mid-retarget when the bot
                # was stopped): if we still hold the coin, flag it so fix_missing_tp re-places it.
                held = float(self.call("get_my_token_balance", {"token_address": token}).get("balance_token_amount") or 0)
                if held > 0 and held * float(self.call("get_token_price", {"token_address": token}).get("price_usd") or 0) > 0.5:
                    rec["tp_lost"] = True
                else:
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
                self._take_now(rec, token, held)
            else:
                rec["tp_order"] = self.call("create_order", {"order_type": "tp_only", "token_address": token,
                                                             "amount": held, "take_profit_price": target})
                rec["status"] = f"holding, take-profit moved to +{self.tp * 100:.0f}%"
            log.info("%s: %s", sym, rec["status"])
            save(self.state)

    def apply(self, p: dict) -> None:
        self.tp, self.limit_cushion = p["take_profit"], p["limit_cushion"]
        self.max_runup, self.max_spike = p["max_runup"], p["max_spike"]
        self.min_mcap, self.max_mcap = p["min_mcap"], p["max_mcap"]
        self.min_whale_usd, self.whale_type = p["min_whale_usd"], p["whale_type"]

    def learn(self) -> None:
        """Self-learning: learn from matured observations, adopt the settings with the best expected
        total profit, and move open take-profits when the target changes."""
        added = self.learner.collect(self.state)
        old_tp, old = self.tp, dict(self.learner.params)
        p, exp = self.learner.choose()
        self.apply(p)
        entry = {"at": _iso(_now()), "new_samples": added, "live_samples": len(self.learner.live),
                 "settings": p, "expected_avg_pct": round(exp.get("avg", 0) * 100, 1),
                 "expected_trades_in_samples": round(exp.get("trades", 0), 1)}
        hist = self.state.setdefault("learning_log", [])
        if not hist or hist[-1].get("settings") != p or added:
            hist.append(entry)
            self.state["learning_log"] = hist[-300:]
            save(self.state)
        if p != old:
            log.info("learned settings: %s (expected %+.1f%% per trade)", p, exp.get("avg", 0) * 100)
        # Retargeting cancels and re-creates every open take-profit (2 order calls per coin), which is
        # what used up Moby's order limit; do it at most once an hour.
        if abs(self.tp - old_tp) > 1e-9 and not self.dry_run:
            if _now() - float(self.state.get("last_retarget") or 0) > 3600:
                self.state["last_retarget"] = _now()
                self.retarget()
            else:
                self.tp = old_tp   # keep current targets until the next allowed retarget
                log.info("take-profit change to %+.0f%% deferred (retarget at most hourly)", p["take_profit"] * 100)

    def _watching(self) -> bool:
        """Keep running while a live position could still sell and be re-bought."""
        return not self.dry_run and any(r.get("mode") == "LIVE" and (r.get("entry_price") or r.get("status") == "limit pending")
                                        and (not r.get("closed") or r.get("rebuy_until"))
                                        for r in self.state["trades"])

    def run(self, trades: int, poll: float, hours: float) -> None:
        done = sum(1 for t in self.state["trades"]
                   if t.get("mode") == ("practice" if self.dry_run else "LIVE") and not t.get("rebuy_of"))
        deadline = _now() + hours * 3600
        after = _iso(_now() - 60)
        log.info("watching whale feed: %d/%d trades done", done, trades)
        last_exit_check = 0.0
        while _now() < deadline and (done < trades or self._watching()):
            if stopped():
                log.info("STOP file present: exiting")
                return
            if blocked_until() > _now():      # Moby locked the key: make no calls at all until then
                time.sleep(min(60.0, blocked_until() - _now()))
                continue
            pending = any(r.get("status") == "limit pending" for r in self.state["trades"])
            if not self.dry_run and _now() - last_exit_check > (5 if pending else 20):
                last_exit_check = _now()
                try:
                    self.check_pending()
                    self.check_rebuys()
                    self.fix_missing_tp()
                except Exception as e:
                    log.warning("pending/re-buy check error: %s", e)
            if self.learner and _now() - self._last_learn > self.learn_every:
                self._last_learn = _now()
                try:
                    self.learn()
                except Exception as e:
                    log.warning("learning error: %s", e)
            if done >= trades:
                time.sleep(poll)
                continue
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
                self.observe(r)
                why = self.qualifies(r, now)
                explore = bool(why and why.startswith("rule:") and random.random() < self.explore)
                if (why is None or explore) and done < trades:
                    why_not = self.can_buy()
                    if why_not:
                        log.info("skip whale buy in %s: %s", r["token"].get("symbol"), why_not)
                        continue
                    log.info("whale %s opened $%.0f in %s (mcap $%.0f): buying", r.get("wallet_list_type"),
                             r.get("amount_usd", 0), r["token"].get("symbol"), r.get("token_market_cap", 0))
                    rec = self.trade(r, now)
                    if explore:
                        rec["explore"] = why
                    rec["views"] = self.views(rec["token_address"])
                    if rec.get("status", "").startswith("skipped"):
                        log.info("skip %s: %s", rec["symbol"], rec["status"])
                        self.state.setdefault("skipped", []).append({**rec, "skipped_at": _iso(_now())})
                        self.state["skipped"] = self.state["skipped"][-300:]
                        save(self.state)
                        continue
                    self.state["trades"].append(rec)
                    save(self.state)
                    log.info("trade: %s", json.dumps({k: v for k, v in rec.items() if k not in ("tp_order", "sell")}, default=str))
                    done += 1
            if rows:
                save(self.state)
            after = _iso(now - 30)
            time.sleep(poll)
        log.info("finished: %d trades", done)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", type=int, default=4)
    ap.add_argument("--usd", type=float, default=5)
    ap.add_argument("--take-profit", type=float, default=25)
    ap.add_argument("--max-age", type=float, default=20, help="ignore whale buys older than this many seconds")
    ap.add_argument("--poll", type=float, default=4)
    ap.add_argument("--hours", type=float, default=20)
    ap.add_argument("--max-open", type=int, default=5, help="most whale coins held at once")
    ap.add_argument("--max-rebuys", type=int, default=0, help="re-buy cycles per coin (0 = no cap)")
    ap.add_argument("--no-look", action="store_true", help="skip the chart check before buying")
    ap.add_argument("--market-buy", action="store_true", help="whale buys at market instead of a limit order")
    ap.add_argument("--limit-cushion", type=float, default=5, help="limit price = price seen + this %%")
    ap.add_argument("--limit-wait", type=float, default=120, help="cancel an unfilled limit buy after this many seconds")
    ap.add_argument("--no-learn", action="store_true", help="fixed settings instead of self-learning")
    ap.add_argument("--explore", type=float, default=20, help="%% of signals failing learned rules traded anyway")
    ap.add_argument("--no-rebuy", action="store_true",
                    help="never buy back after a take-profit (history: lost less at every target)")
    ap.add_argument("--rebuy-if-rising", action="store_true",
                    help="only re-buy after a sale if the coin is still rising (default: always re-buy)")
    ap.add_argument("--max-runup", type=float, default=100, help="skip if price is more than this %% above its 5-min low")
    ap.add_argument("--max-spike", type=float, default=20, help="skip if price is up more than this %% in the current minute")
    ap.add_argument("--cash-floor", type=float, default=10, help="never let USDC fall below this")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--retarget", action="store_true",
                    help="move open take-profits to --take-profit, then keep trading")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_env()
    s = Sniper(MobyClient(), a.usd, a.take_profit, a.max_age, a.dry_run)
    s.max_open, s.cash_floor, s.max_rebuys = a.max_open, a.cash_floor, a.max_rebuys
    s.look_first, s.max_runup, s.max_spike = not a.no_look, a.max_runup / 100, a.max_spike / 100
    s.rebuy_always = not a.rebuy_if_rising
    s.rebuy_enabled = not a.no_rebuy
    s.explore = a.explore / 100
    if not a.no_learn:
        s.learner = Learner(s.c)   # every setting is learned; command-line values are only the start
    s.limit_cushion = None if a.market_buy else a.limit_cushion / 100
    s.limit_wait = a.limit_wait
    if a.retarget and not a.dry_run:
        s.retarget()
    s.run(a.trades, a.poll, a.hours)


if __name__ == "__main__":
    main()
