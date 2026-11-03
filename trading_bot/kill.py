"""Kill switch.

  python -m trading_bot.kill              Stop all new trading (writes STOP). Exits already on
                                          Moby (stop-loss / take-profit) stay in place to protect you.
  python -m trading_bot.kill --sell-all   Also sell every position at market now (real money).
  python -m trading_bot.kill --resume     Remove STOP so the bot can trade again.

From your phone: ask Claude to commit a REMOTE_STOP file to GitHub; the bot's next cycle
pulls it and stops. Or pause AI Trading in the Moby app, or revoke the API key there.
"""
from __future__ import annotations

import sys

from trading_bot.settings import ROOT, load_env

STOP_FILE = ROOT / "STOP"


def main() -> None:
    if "--resume" in sys.argv:
        STOP_FILE.unlink(missing_ok=True)
        print("STOP removed. (If REMOTE_STOP exists on GitHub, delete it there too.)")
        return
    STOP_FILE.write_text("Stopped by trading_bot.kill\n")
    print("STOP written: the bot will not place new trades.")
    if "--sell-all" in sys.argv:
        load_env()
        from trading_bot.engine import TradingEngine
        from trading_bot.moby_client import MobyClient
        client = MobyClient()
        _, positions = TradingEngine(client, {}).holdings()
        for p in positions:
            res = client.call_tool("quick_sell", {"token_address": p["token_address"], "pct": 100})
            print(f"sold {p.get('symbol')}: {res.get('status') if isinstance(res, dict) else res}")
        print(f"Sold {len(positions)} position(s).")


if __name__ == "__main__":
    main()
