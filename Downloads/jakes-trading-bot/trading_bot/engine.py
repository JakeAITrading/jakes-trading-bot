"""Smart-money trend strategy on Moby.

Each cycle:
  1. Safety checks (kill switch, Moby guardrails paused).
  2. Manage what we hold: make sure every position has a stop-loss + take-profit
     on Moby, and sell early if smart money has flipped to selling.
  3. Look for new entries on Moby's leaderboard: trending up, tracked whales
     net buying on 1h AND 4h, Moby's Smart Money Read = buying, safe contract.
  4. Buy the best candidate (if room and cash), then place its exits.

In dry-run mode every money-moving call (quick_buy, quick_sell, create_order)
is logged instead of sent. Read-only calls still run so the decisions are real.
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


class TradingEngine:
    def __init__(self, client, config: dict, notifier=None):
        self.client = client
        self.cfg = config
        self.notifier = notifier
        self.last_traded: dict[str, dt.datetime] = {}   # token -> last buy/sell time
        self._day: dt.date | None = None
        self._bought_today_usd = 0.0
        self.last_report: dict | None = None

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

    def _money(self, report: dict, tool: str, args: dict, why: str) -> Any:
        entry = {"tool": tool, "arguments": args, "why": why}
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
        if isinstance(result, dict) and result.get("blocked"):
            entry["status"] = f"blocked by Moby: {result.get('reason')}"
        else:
            entry["status"] = "sent"
        entry["result"] = result
        report["actions"].append(entry)
        log.info("%s %s -> %s", tool, args, entry["status"])
        return result if entry["status"] == "sent" else None

    # -- holdings --------------------------------------------------------
    def holdings(self) -> tuple[float, list[dict]]:
        """Return (USDC cash, list of non-cash, non-SOL positions)."""
        bal = self._read("get_my_balances")
        cash = _num((bal.get("usd_token") or {}).get("quantity"))
        positions = []
        for t in bal.get("tokens") or []:
            if t.get("is_quote") or t.get("is_cash"):
                cash = _num(t.get("quantity"), cash)
                continue
            if t.get("is_native") or t.get("is_dust"):
                continue  # SOL: Moby can't place TP/SL on it, and we never buy it
            if _num(t.get("quantity")) > 0:
                positions.append(t)
        return cash, positions

    # -- step 2: manage positions -----------------------------------------
    def _protect(self, report: dict, token: str, symbol: str, qty: float) -> None:
        pos = self._read("get_my_position", {"token_address": token}) or {}
        if pos.get("stop_loss_set"):
            return
        quote = self._read("get_token_price", {"token_address": token})
        price = _num(quote.get("price_usd") if isinstance(quote, dict) else quote)
        if price <= 0:
            report["notes"].append(f"{symbol}: no price, could not place exits")
            return
        basis_qty = _num(pos.get("basis_known_token_amount"))
        entry = _num(pos.get("cost_basis_usd")) / basis_qty if basis_qty > 0 else price
        # Use the lower of entry and current so the stop is never above market.
        base = min(entry, price)
        sl = base * (1 - _num(self.r.get("stop_loss_pct"), 25) / 100)
        tp = max(entry, price) * (1 + _num(self.r.get("take_profit_pct"), 50) / 100)
        self._money(report, "create_order", {
            "order_type": "sl_and_tp", "token_address": token, "amount": qty,
            "stop_loss_price": round(sl, 12), "take_profit_price": round(tp, 12),
        }, f"protect {symbol}: stop-loss {sl:.6g}, take-profit {tp:.6g}")

    def _smart_money_selling(self, brief: dict) -> bool:
        flow = (brief.get("smart_money_flow") or {}).get("flow") or {}
        h1 = _num((flow.get("h1") or {}).get("net_flow_usd"))
        h4 = _num((flow.get("h4") or {}).get("net_flow_usd"))
        label = str((brief.get("smart_money_read") or {}).get("label", "")).lower()
        return (h1 < 0 and h4 < 0) or "sell" in label

    def manage_positions(self, report: dict, positions: list[dict]) -> None:
        for p in positions:
            token, symbol, qty = p["token_address"], p.get("symbol", "?"), _num(p.get("quantity"))
            if self.r.get("exit_on_smart_money_selling", True):
                brief = self._read("get_token_trade_brief", {"token_address": token}) or {}
                if self._smart_money_selling(brief):
                    self._money(report, "quick_sell", {"token_address": token, "pct": 100},
                                f"exit {symbol}: smart money turned to net selling")
                    self.last_traded[token] = _now()
                    continue
            self._protect(report, token, symbol, qty)

    # -- step 3: find entries -------------------------------------------
    def passes_screen(self, row: dict) -> str | None:
        """Return None if the leaderboard row qualifies, else the reason it doesn't."""
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
        cooldown = dt.timedelta(hours=_num(self.s.get("cooldown_hours"), 24))
        picks = []
        for row in rows:
            token, sym = row.get("token_address"), row.get("symbol")
            if not token or token in held or token in blocked:
                continue
            if token in self.last_traded and _now() - self.last_traded[token] < cooldown:
                continue
            reason = self.passes_screen(row)
            if reason:
                report["screened_out"].append(f"{sym}: {reason}")
                continue
            picks.append(row)
        # Strongest smart-money inflow first.
        picks.sort(key=lambda r: _num(((r.get("whale") or {}).get("h4") or {}).get("whale_net_flow_usd")),
                   reverse=True)
        return picks

    # -- step 4: buy ------------------------------------------------------
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
        self.last_traded[token] = _now()
        self._bought_today_usd += usd
        if result is None:
            return
        # Place exits now if the fill is booked; otherwise next cycle's manage step does it.
        qty = _num(result.get("filled_output_amount_ui") if isinstance(result, dict) else None)
        if qty > 0:
            self._protect(report, token, sym, qty)

    # -- the cycle --------------------------------------------------------
    def run_cycle(self) -> dict:
        today = _now().date()
        if today != self._day:
            self._day, self._bought_today_usd = today, 0.0
        report: dict = {"time": _now().isoformat(), "dry_run": self.dry_run,
                        "actions": [], "notes": [], "screened_out": []}
        self.last_report = report

        if self.cfg.get("trading", {}).get("kill_switch"):
            report["notes"].append("KILL_SWITCH is on: no trading")
            return report
        guard = self._read("get_my_guardrails") or {}
        if guard.get("paused"):
            report["notes"].append("Trading is paused in the Moby app")
            return report

        cash, positions = self.holdings()
        report["cash_usd"], report["positions"] = round(cash, 2), [p.get("symbol") for p in positions]
        self.manage_positions(report, positions)

        usd = _num(self.r.get("usd_per_trade"), 20)
        max_trade = guard.get("maxTradeUsd")
        if max_trade is not None:
            usd = min(usd, _num(max_trade))
        slots = int(self.r.get("max_open_positions", 3)) - len(positions)
        buys_left = int(self.r.get("max_buys_per_cycle", 1))
        if slots <= 0:
            report["notes"].append("max open positions reached")
            return self._finish(report)

        held = {p["token_address"] for p in positions}
        for row in self.find_candidates(report, held):
            if slots <= 0 or buys_left <= 0:
                break
            if cash - usd < _num(self.r.get("min_cash_reserve_usd"), 5):
                report["notes"].append(f"not enough cash (${cash:.2f}) for a ${usd:.2f} buy")
                break
            if self._bought_today_usd + usd > _num(self.r.get("max_daily_buy_usd"), 40):
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
        return self._finish(report)

    def _finish(self, report: dict) -> dict:
        if self.notifier and report["actions"]:
            self.notifier.send_report(report)
        return report
