"""Whale-pop experiment: buy right after a tracked whale opens a position in a small coin,
then sell at a fixed gain. No stop-loss (Jake's call: these coins can dip and recover in seconds).

  python -m trading_bot.sniper --trades 4 --usd 5 --take-profit 25          # LIVE (real money)
  python -m trading_bot.sniper --trades 4 --usd 5 --take-profit 25 --dry-run

Watches Moby's whale feed every few seconds. A whale buy qualifies when it is:
  - a tracked wallet OPENING a position (action first_buy), at least $250,
  - in a coin with market cap $20k-$1M that Moby does not rate red,
  - seen within --max-age seconds of the trade, in a coin we haven't traded.
Before buying it glances at the chart (--max-runup, --max-spike) and skips coins that already
pumped. Then: quick_buy $usd, wait for the fill, place a take-profit order at entry * (1 + tp%).
When a take-profit fills it buys the same coin right back with a fresh take-profit (the profit is
kept; --rebuy-if-rising only re-buys while the price is at/above the sale with a green last minute),
once per sale (no cap on how many times a coin can cycle; --max-rebuys sets one). Re-buys don't
count toward --trades.
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
        self.max_rebuys, self.rebuy_window = 0, 600  # 0 = no cap per coin; look for 10 min after a sale
        self.max_open, self.cash_floor = 5, 10.0       # our own limits (Moby's are off)
        self.look_first, self.max_runup, self.max_spike = True, 1.0, 0.20
        self.rebuy_always = True   # re-buy right after every +tp% sale (False = only if still rising)

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
        if low5 and price / low5 - 1 > self.max_runup:
            return f"already pumped: +{info['above_5m_low_pct']:.0f}% above 5-min low", info
        if cur_open and price / cur_open - 1 > self.max_spike:
            return f"mid-spike: +{info['this_minute_pct']:.0f}% this minute", info
        return None, info

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
        if skip:
            rec["status"] = f"skipped: {skip}"
            return rec
        if self.dry_run:
            rec.update(status="simulated", entry_price=price_seen, bought_at=_iso(_now()))
            return rec
        return self._buy(rec, token, sym, whale_ts,
                         f"Whale-pop experiment: {row.get('wallet_list_type')} opened "
                         f"${row.get('amount_usd', 0):,.0f} in {sym}; sell at +{self.tp*100:.0f}%.")

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

    def open_count(self) -> int:
        return sum(1 for r in self.state["trades"]
                   if r.get("mode") == "LIVE" and r.get("entry_price") and not r.get("closed"))

    def can_buy(self) -> str | None:
        """Our own safeguards. None = OK to buy, else the reason not to."""
        if stopped():
            return "STOP file present"
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
                               rebuy_until=_now() + self.rebuy_window)
                    log.info("%s: take-profit filled, profit $%.2f", rec["symbol"], rec["profit_usd"])
                    save(self.state)
                else:
                    rec["closed"] = f"take-profit {o.get('status')}"
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
            self._buy(new, rec["token_address"], rec["symbol"], _now(),
                      f"Re-buy: {rec['symbol']} still rising after +{self.tp*100:.0f}% take-profit.")
            log.info("re-buy: %s", json.dumps({k: v for k, v in new.items() if k not in ("tp_order", "sell")}, default=str))
            if new.get("entry_price"):
                # Bought: one re-buy per sale; the new position re-buys again only after it sells at +tp%.
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

    def _watching(self) -> bool:
        """Keep running while a live position could still sell and be re-bought."""
        return not self.dry_run and any(r.get("mode") == "LIVE" and r.get("entry_price")
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
            if not self.dry_run and _now() - last_exit_check > 15:
                last_exit_check = _now()
                try:
                    self.check_rebuys()
                except Exception as e:
                    log.warning("re-buy check error: %s", e)
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
                if self.qualifies(r, now) is None and done < trades:
                    why_not = self.can_buy()
                    if why_not:
                        log.info("skip whale buy in %s: %s", r["token"].get("symbol"), why_not)
                        continue
                    log.info("whale %s opened $%.0f in %s (mcap $%.0f): buying", r.get("wallet_list_type"),
                             r.get("amount_usd", 0), r["token"].get("symbol"), r.get("token_market_cap", 0))
                    rec = self.trade(r, now)
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
    ap.add_argument("--max-open", type=int, default=5, help="most whale coins held at once")
    ap.add_argument("--max-rebuys", type=int, default=0, help="re-buy cycles per coin (0 = no cap)")
    ap.add_argument("--no-look", action="store_true", help="skip the chart check before buying")
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
    if a.retarget and not a.dry_run:
        s.retarget()
    s.run(a.trades, a.poll, a.hours)


if __name__ == "__main__":
    main()
