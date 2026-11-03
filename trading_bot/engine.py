"""Smart-money trend strategy on Moby.

Each cycle:
  1. Safety checks (Moby guardrails paused).
  2. Manage what we hold: every position gets a stop-loss + take-profit on Moby,
     and is sold early if smart money flips to net selling.
  3. Look for new entries on Moby's leaderboard: trending up, tracked whales net
     buying on 1h AND 4h, Moby's Smart Money Read = buying, safe contract.
  4. Buy the best candidate (if room and cash), then place its exits.

Dry run (practice) mode never calls a money-moving tool. Instead it keeps "paper"
positions at the real market price and closes them with the same exit rules, so
practice results show what the strategy would have made.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

log = logging.getLogger(__name__)

MONEY_TOOLS = {"quick_buy", "quick_sell", "create_order", "execute_swap"}


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def new_state() -> dict:
    return {"last_traded": {}, "day": None, "bought_today_usd": 0.0, "paper": {}, "paper_closed": []}


class TradingEngine:
    def __init__(self, client, config: dict, state: dict | None = None):
        self.client = client
        self.cfg = config
        self.state = {**new_state(), **(state or {})}

    # -- config helpers --------------------------------------------------
    @property
    def s(self) -> dict:
        return self.cfg.get("strategy", {})

    @property
    def r(self) -> dict:
        return self.cfg.get("risk", {})

    @property
    def dry_run(self) -> bool:
        return bool(self.cfg.get("trading", {}).get("dry_run", True))

    # -- Moby calls ------------------------------------------------------
    def _read(self, tool: str, args: dict | None = None) -> Any:
        assert tool not in MONEY_TOOLS
        return self.client.call_tool(tool, args or {})

    def _price(self, token: str) -> float:
        q = self._read("get_token_price", {"token_address": token})
        return _num(q.get("price_usd") if isinstance(q, dict) else q)

    def _money(self, report: dict, tool: str, args: dict, why: str) -> Any:
        """Send a money-moving call (live), or record it as simulated (dry run)."""
        entry = {"time": _now().isoformat(), "tool": tool, "arguments": args, "why": why}
        if self.dry_run:
            entry["status"] = "simulated"
            report["actions"].append(entry)
            log.info("[DRY RUN] %s %s (%s)", tool, args, why)
            return None
        try:
            result = self.client.call_tool(tool, args)
        except Exception as exc:
            entry["status"] = f"failed: {exc}"
            report["actions"].append(entry)
            log.error("%s failed: %s", tool, exc)
            return None
        blocked = isinstance(result, dict) and result.get("blocked")
        entry["status"] = f"blocked by Moby: {result.get('reason')}" if blocked else "sent"
        entry["result"] = result
        report["actions"].append(entry)
        log.info("%s %s -> %s", tool, args, entry["status"])
        return None if blocked else result

    def _mark_traded(self, token: str) -> None:
        self.state["last_traded"][token] = _now().isoformat()

    def _in_cooldown(self, token: str) -> bool:
        last = self.state["last_traded"].get(token)
        if not last:
            return False
        hours = _num(self.s.get("cooldown_hours"), 24)
        return _now() - dt.datetime.fromisoformat(last) < dt.timedelta(hours=hours)

    # -- holdings --------------------------------------------------------
    def holdings(self) -> tuple[float, list[dict]]:
        """(USDC cash, real non-cash, non-SOL positions) from the chain via Moby."""
        from trading_bot.sniper import sniper_tokens
        skip = sniper_tokens()  # coins owned by the whale-pop experiment
        bal = self._read("get_my_balances")
        cash = _num((bal.get("usd_token") or {}).get("quantity"))
        positions = []
        for t in bal.get("tokens") or []:
            if t.get("token_address") in skip:
                continue
            if t.get("is_quote") or t.get("is_cash"):
                cash = _num(t.get("quantity"), cash)
            elif not (t.get("is_native") or t.get("is_dust")) and _num(t.get("quantity")) > 0:
                positions.append(t)  # SOL is skipped: no TP/SL on it, and we never buy it
        return cash, positions

    # -- exits -----------------------------------------------------------
    def _levels(self, entry: float, price: float) -> tuple[float, float]:
        sl = min(entry, price) * (1 - _num(self.r.get("stop_loss_pct"), 25) / 100)
        tp = max(entry, price) * (1 + _num(self.r.get("take_profit_pct"), 50) / 100)
        return sl, tp

    def _has_stop(self, token: str) -> bool:
        """True if Moby already has a live stop-loss on this token. Checked against the open
        orders themselves: get_my_position's stop_loss_set flag stayed false after a bracket
        was placed, which made the bot place a new bracket every cycle."""
        orders = self._read("get_my_orders") or {}
        return any(o.get("token_address") == token and o.get("order_type") in ("sl_only", "sl_and_tp")
                   for o in orders.get("orders") or [])

    def _protect(self, report: dict, token: str, symbol: str, qty: float) -> None:
        if self._has_stop(token):
            return
        pos = self._read("get_my_position", {"token_address": token}) or {}
        if pos.get("stop_loss_set"):
            return
        price = self._price(token)
        if price <= 0:
            report["notes"].append(f"{symbol}: no price, could not place exits")
            return
        basis_qty = _num(pos.get("basis_known_token_amount"))
        entry = _num(pos.get("cost_basis_usd")) / basis_qty if basis_qty > 0 else price
        sl, tp = self._levels(entry, price)
        self._money(report, "create_order", {
            "order_type": "sl_and_tp", "token_address": token, "amount": qty,
            "stop_loss_price": round(sl, 12), "take_profit_price": round(tp, 12),
        }, f"protect {symbol}: stop-loss {sl:.6g}, take-profit {tp:.6g}")

    def _smart_money_selling(self, token: str) -> bool:
        if not self.r.get("exit_on_smart_money_selling", True):
            return False
        brief = self._read("get_token_trade_brief", {"token_address": token}) or {}
        flow = (brief.get("smart_money_flow") or {}).get("flow") or {}
        h1 = _num((flow.get("h1") or {}).get("net_flow_usd"))
        h4 = _num((flow.get("h4") or {}).get("net_flow_usd"))
        label = str((brief.get("smart_money_read") or {}).get("label", "")).lower()
        return (h1 < 0 and h4 < 0) or "sell" in label

    def manage_positions(self, report: dict, positions: list[dict]) -> None:
        for p in positions:
            token, symbol, qty = p["token_address"], p.get("symbol", "?"), _num(p.get("quantity"))
            if self._smart_money_selling(token):
                self._money(report, "quick_sell", {"token_address": token, "pct": 100},
                            f"exit {symbol}: smart money turned to net selling")
                self._mark_traded(token)
                continue
            self._protect(report, token, symbol, qty)

    def manage_paper(self, report: dict) -> None:
        """Close practice positions with the same rules the live exits use."""
        for token, p in list(self.state["paper"].items()):
            price = self._price(token)
            if price <= 0:
                continue
            reason = None
            if price <= p["stop_loss"]:
                reason = "stop-loss"
            elif price >= p["take_profit"]:
                reason = "take-profit"
            elif self._smart_money_selling(token):
                reason = "smart money selling"
            if reason:
                pnl = p["usd"] * (price / p["entry_price"] - 1)
                closed = {**p, "token_address": token, "exit_price": price, "closed_at": _now().isoformat(),
                          "exit_reason": reason, "pnl_usd": round(pnl, 4)}
                self.state["paper_closed"].append(closed)
                del self.state["paper"][token]
                self._mark_traded(token)
                report["actions"].append({"time": closed["closed_at"], "tool": "quick_sell", "status": "simulated",
                                          "arguments": {"token_address": token, "pct": 100},
                                          "why": f"paper exit {p['symbol']}: {reason}, P&L ${pnl:+.2f}"})

    # -- entries ---------------------------------------------------------
    def passes_screen(self, row: dict) -> str | None:
        """None if the leaderboard row qualifies, else why not."""
        s = self.s
        mc, liq = _num(row.get("market_cap_usd")), _num(row.get("liquidity_usd"))
        h1, h24 = _num(row.get("price_change_pct_h1")), _num(row.get("price_change_pct_h24"))
        whale = row.get("whale") or {}
        w1, w4 = whale.get("h1") or {}, whale.get("h4") or {}
        if row.get("safety_tier") != "green":
            return f"safety {row.get('safety_tier')}"
        if not (_num(s.get("min_market_cap_usd")) <= mc <= _num(s.get("max_market_cap_usd"), 1e18)):
            return "market cap out of range"
        if liq < _num(s.get("min_liquidity_usd")):
            return "thin liquidity"
        if h1 <= _num(s.get("min_price_change_h1_pct")) or h24 <= _num(s.get("min_price_change_h24_pct")):
            return "not trending up"
        if h24 > _num(s.get("max_price_change_h24_pct"), 1e9):
            return "already ran too far"
        if _num(w1.get("whale_net_flow_usd")) < _num(s.get("min_whale_net_flow_h1_usd")):
            return "whales not net buying (1h)"
        if _num(w4.get("whale_net_flow_usd")) < _num(s.get("min_whale_net_flow_h4_usd")):
            return "whales not net buying (4h)"
        if _num(w4.get("whale_buyers_count")) < _num(s.get("min_whale_buyers_h4")):
            return "too few whale buyers"
        return None

    def passes_brief(self, brief: dict) -> str | None:
        if (brief.get("safety") or {}).get("safety_tier") != "green":
            return "brief: safety not green"
        tier = (brief.get("smart_money_read") or {}).get("tier")
        if tier not in (self.s.get("required_smart_money_tiers") or ["green"]):
            return f"brief: smart money read is {tier}"
        risk = brief.get("risk_signals") or {}
        if risk.get("honeypot") is True:
            return "brief: honeypot"
        if risk.get("mint_authority_renounced") is False or risk.get("freeze_authority_renounced") is False:
            return "brief: mint/freeze authority not renounced"
        if _num(risk.get("sell_tax_pct")) > 5:
            return "brief: sell tax"
        return None

    def find_candidates(self, report: dict, held: set[str]) -> list[dict]:
        lb = self._read("get_screener_leaderboard", {
            "limit": int(self.s.get("leaderboard_size", 30)), "whale_windows": ["h1", "h4"]})
        rows = lb.get("entries", []) if isinstance(lb, dict) else []
        blocked = set(self.r.get("blocked_tokens") or [])
        picks = []
        for row in rows:
            token, sym = row.get("token_address"), row.get("symbol")
            if not token or token in held or token in blocked or self._in_cooldown(token):
                continue
            reason = self.passes_screen(row)
            if reason:
                report["screened_out"].append(f"{sym}: {reason}")
            else:
                picks.append(row)
        picks.sort(key=lambda r: _num(((r.get("whale") or {}).get("h4") or {}).get("whale_net_flow_usd")),
                   reverse=True)
        return picks

    def buy(self, report: dict, row: dict, usd: float) -> None:
        token, sym = row["token_address"], row.get("symbol", "?")
        w = row.get("whale") or {}
        rationale = (f"Trend + smart money: {sym} up {row.get('price_change_pct_h1')}% 1h / "
                     f"{row.get('price_change_pct_h24')}% 24h; whale net flow "
                     f"${_num((w.get('h1') or {}).get('whale_net_flow_usd')):,.0f} 1h, "
                     f"${_num((w.get('h4') or {}).get('whale_net_flow_usd')):,.0f} 4h. "
                     f"Exit: SL -{self.r.get('stop_loss_pct')}%, TP +{self.r.get('take_profit_pct')}%, "
                     f"or when smart money turns seller.")[:500]
        result = self._money(report, "quick_buy",
                             {"output_token": token, "amount_usd": usd, "rationale": rationale},
                             f"buy ${usd:.2f} of {sym}")
        self._mark_traded(token)
        self.state["bought_today_usd"] += usd
        if self.dry_run:
            price = _num(row.get("price_usd")) or self._price(token)
            sl, tp = self._levels(price, price)
            self.state["paper"][token] = {"symbol": sym, "usd": usd, "entry_price": price,
                                          "stop_loss": sl, "take_profit": tp, "opened_at": _now().isoformat()}
            return
        # Place exits now if the fill is booked; otherwise the next cycle's manage step does it.
        qty = _num(result.get("filled_output_amount_ui") if isinstance(result, dict) else None)
        if qty > 0:
            self._protect(report, token, sym, qty)

    # -- the cycle -------------------------------------------------------
    def run_cycle(self) -> dict:
        today = _now().date().isoformat()
        if today != self.state["day"]:
            self.state["day"], self.state["bought_today_usd"] = today, 0.0
        report: dict = {"time": _now().isoformat(), "dry_run": self.dry_run,
                        "actions": [], "notes": [], "screened_out": []}

        guard = self._read("get_my_guardrails") or {}
        if guard.get("paused"):
            report["notes"].append("Trading is paused in the Moby app")
            return report

        cash, positions = self.holdings()
        report["cash_usd"] = round(cash, 2)
        report["positions"] = [p.get("symbol") for p in positions]
        self.manage_positions(report, positions)
        held = {p["token_address"] for p in positions}
        if self.dry_run:
            self.manage_paper(report)
            report["paper_positions"] = [p["symbol"] for p in self.state["paper"].values()]
            cash -= sum(p["usd"] for p in self.state["paper"].values())
            held |= set(self.state["paper"])

        usd = _num(self.r.get("usd_per_trade"), 20)
        if guard.get("maxTradeUsd") is not None:
            usd = min(usd, _num(guard["maxTradeUsd"]))
        slots = int(self.r.get("max_open_positions", 3)) - len(held)
        buys_left = int(self.r.get("max_buys_per_cycle", 1))
        if slots <= 0:
            report["notes"].append("max open positions reached")
            return report

        for row in self.find_candidates(report, held):
            if slots <= 0 or buys_left <= 0:
                break
            if cash - usd < _num(self.r.get("min_cash_reserve_usd"), 5):
                report["notes"].append(f"not enough cash (${cash:.2f}) for a ${usd:.2f} buy")
                break
            if self.state["bought_today_usd"] + usd > _num(self.r.get("max_daily_buy_usd"), 40):
                report["notes"].append("daily buy budget used up")
                break
            brief = self._read("get_token_trade_brief", {"token_address": row["token_address"]}) or {}
            reason = self.passes_brief(brief)
            if reason:
                report["screened_out"].append(f"{row.get('symbol')}: {reason}")
                continue
            self.buy(report, row, usd)
            cash -= usd
            slots -= 1
            buys_left -= 1
        return report
