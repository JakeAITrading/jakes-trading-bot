"""Plain-English results: wallet, practice P&L, and recent trades (read-only).

  python -m trading_bot.report
"""
from __future__ import annotations

from trading_bot import store


def main() -> None:
    state = store.load_state()
    balances = store.read_lines("balances.jsonl")
    trades = store.read_lines("trades.jsonl")
    if balances:
        first, last = balances[0], balances[-1]
        print(f"Wallet: ${last.get('wallet_usd') or 0:.2f} now (was ${first.get('wallet_usd') or 0:.2f} "
              f"at {first['time'][:16]}), {len(balances)} cycles logged")
    closed = state.get("paper_closed", [])
    if closed:
        wins = sum(1 for c in closed if c["pnl_usd"] > 0)
        total = sum(c["pnl_usd"] for c in closed)
        print(f"Practice: {len(closed)} closed, {wins} wins, P&L ${total:+.2f}")
    for token, p in (state.get("paper") or {}).items():
        print(f"Practice open: {p['symbol']} ${p['usd']} at {p['entry_price']:.6g}")
    print("Recent trades:")
    for t in trades[-10:]:
        print(f"  {t['time'][:16]} [{t['mode']}] {t['why']} -> {t['status']}")


if __name__ == "__main__":
    main()
