# Jake's Trading Bot — Moby smart-money trend strategy

Automated trading on **Moby: Trade Smarter** (moby.win) through Moby's MCP server
(`https://mcp.moby.win/mcp`). Solana only, funded with USDC in your Moby agent wallet.
Set up the same way as the Kalshi bot: a loop on the Mac mini, results committed to GitHub.

## Strategy (one cycle every 15 minutes)

1. **Manage what it holds.**
   - Every position gets a stop-loss (−25%) and take-profit (+50%) placed on Moby.
   - It sells early if tracked whales turn to net selling on both 1h and 4h.
2. **Find a trend Moby's whales are backing.** From the top 30 on Moby's leaderboard, a coin must:
   - be green safety, $1M–$500M market cap, with $200k+ liquidity;
   - be up on 1h and 24h, but not more than +100% in 24h (no chasing);
   - have tracked whales net buying over 1h *and* 4h, with 3+ distinct whale buyers;
   - pass Moby's trade brief: Smart Money Read = buying, mint/freeze renounced, no honeypot.
3. **Buy the strongest one.**
   - $20 per trade, or Moby's max per trade if that's lower.
   - At most 1 buy per cycle, 3 open positions and $40 of buys a day.
   - Always keeps $5 in cash.

All numbers live in `config.yaml`. **No strategy guarantees profit**: memecoins can drop fast, and a
stop-loss can fill below its price in a crash.

**Practice mode (default):** reads Moby for real, but never sends a buy, sell or order. It keeps
"paper" positions at real prices and closes them by the same rules, so `python -m trading_bot.report`
shows what the strategy would have made.

## Files

| Path | What it is |
|------|------------|
| `trading_bot/engine.py` | the strategy |
| `trading_bot/moby_client.py` | Moby MCP connection (`python -m trading_bot.moby_client tools`) |
| `trading_bot/run.py` | one cycle: run, save memory and log, write `last_run.txt` |
| `trading_bot/report.py` | wallet, practice P&L, recent trades |
| `trading_bot/kill.py` | kill switch |
| `scripts/cycle.sh`, `scripts/loop.sh` | one cycle / repeat with GitHub sync |
| `state/` | the bot's memory and trade log (committed; no secrets) |

## Set up (Mac mini)

```bash
git clone https://github.com/JakeAITrading/jakes-trading-bot ~/moby-bot && cd ~/moby-bot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # paste a NEW Moby key: MOBY_API_KEY=moby_agent_...
.venv/bin/python -m pytest
bash scripts/cycle.sh       # one practice cycle
COUNT=4 INTERVAL=900 bash scripts/loop.sh   # every 15 min for an hour, saving to GitHub
```

Run it continuously with a launchd agent like the Kalshi bot's (`COUNT=4 INTERVAL=900`, KeepAlive).
Run the loop in **one place only**.

**Go live:** add `DRY_RUN=false` to `.env`.

## Stopping it

- **From your phone:** ask Claude to commit a `REMOTE_STOP` file to GitHub. The next cycle pulls it and stops.
  Delete the file to resume.
- **On the Mac:** `python -m trading_bot.kill` stops new trades and leaves your stop-losses in place.
  Add `--sell-all` to also sell everything now. Use `--resume` to undo.
- **No computer needed:** pause AI Trading in the Moby app, or revoke the API key there.
