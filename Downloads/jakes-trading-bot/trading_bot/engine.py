"""Trading cycle: read Moby signals, apply risk rules, place (or simulate) trades."""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

SYMBOL_KEYS = ("symbol", "ticker", "token", "asset", "coin", "pair")
SIDE_KEYS = ("side", "action", "direction", "signal")
SIDE_ALIASES = {"buy": "buy", "long": "buy", "bullish": "buy",
                "sell": "sell", "short": "sell", "bearish": "sell", "exit": "sell"}


@dataclass
class Signal:
    symbol: str
    side: str
    raw: dict = field(default_factory=dict)


def extract_signals(data: Any) -> list[Signal]:
    """Best-effort conversion of a Moby signals payload into Signal objects."""
    if isinstance(data, dict):
        for key in ("signals", "ideas", "trades", "items", "data", "results"):
            if isinstance(data.get(key), list):
                return extract_signals(data[key])
        data = [data]
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        symbol = next((str(item[k]) for k in SYMBOL_KEYS if item.get(k)), None)
        side_raw = next((str(item[k]).lower() for k in SIDE_KEYS if item.get(k)), "")
        side = SIDE_ALIASES.get(side_raw)
        if symbol and side:
            out.append(Signal(symbol.upper(), side, item))
    return out


class TradingEngine:
    def __init__(self, client, config: dict, notifier=None):
        self.client = client
        self.cfg = config
        self.notifier = notifier
        self._day = None
        self._trades_today = 0
        self._done_today: set[tuple[str, str]] = set()

    # -- helpers ---------------------------------------------------------
    def _tool(self, role: str) -> str:
        return (self.cfg.get("moby", {}).get("tools", {}) or {}).get(role) or ""

    def _reset_day(self) -> None:
        today = dt.date.today()
        if today != self._day:
            self._day, self._trades_today, self._done_today = today, 0, set()

    def _allowed(self, sig: Signal) -> str | None:
        risk = self.cfg.get("risk", {})
        allowed = {s.upper() for s in risk.get("allowed_symbols") or []}
        blocked = {s.upper() for s in risk.get("blocked_symbols") or []}
        if allowed and sig.symbol not in allowed:
            return "not in allowed_symbols"
        if sig.symbol in blocked:
            return "in blocked_symbols"
        if (sig.symbol, sig.side) in self._done_today:
            return "already handled today"
        if self._trades_today >= int(risk.get("max_trades_per_day", 0)):
            return "daily trade limit reached"
        return None

    def _trade_args(self, sig: Signal, amount: float) -> dict:
        template = self.cfg.get("moby", {}).get("trade_arguments") or {}
        values = {"symbol": sig.symbol, "side": sig.side, "amount_usd": amount}
        args = {}
        for k, v in template.items():
            if isinstance(v, str) and v.startswith("{") and v.endswith("}") and v[1:-1] in values:
                args[k] = values[v[1:-1]]  # keep native type (e.g. number)
            elif isinstance(v, str):
                args[k] = v.format(**values)
            else:
                args[k] = v
        return args

    # -- public ----------------------------------------------------------
    def wallet(self) -> Any:
        tool = self._tool("wallet")
        return self.client.call_tool(tool) if tool else None

    def run_cycle(self) -> dict:
        self._reset_day()
        dry_run = bool(self.cfg.get("trading", {}).get("dry_run", True))
        report: dict = {"time": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "dry_run": dry_run, "actions": [], "skipped": []}

        signals_tool = self._tool("signals")
        if not signals_tool:
            report["error"] = "moby.tools.signals is not configured"
            return report
        signals = extract_signals(self.client.call_tool(signals_tool))
        report["signals_seen"] = len(signals)

        trade_tool = self._tool("trade")
        amount = float(self.cfg.get("risk", {}).get("amount_usd_per_trade", 0))
        for sig in signals:
            reason = self._allowed(sig)
            if reason:
                report["skipped"].append({"symbol": sig.symbol, "side": sig.side, "reason": reason})
                continue
            args = self._trade_args(sig, amount)
            action = {"symbol": sig.symbol, "side": sig.side, "tool": trade_tool, "arguments": args}
            if dry_run or not trade_tool:
                action["status"] = "simulated" if trade_tool else "simulated (no trade tool configured)"
            else:
                try:
                    action["result"] = self.client.call_tool(trade_tool, args)
                    action["status"] = "placed"
                except Exception as exc:  # keep going with other signals
                    action["status"] = f"failed: {exc}"
                    log.exception("Trade failed for %s", sig.symbol)
            if action["status"] in ("placed",) or action["status"].startswith("simulated"):
                self._trades_today += 1
                self._done_today.add((sig.symbol, sig.side))
            report["actions"].append(action)
            log.info("%s %s %s -> %s", sig.side, sig.symbol, args, action["status"])

        if self.notifier and report["actions"]:
            self.notifier.send_report(report)
        return report
