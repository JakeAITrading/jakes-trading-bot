# Jake's Trading Bot — Moby smart-money trend strategy

Automated trading on **Moby: Trade Smarter** (moby.win) through Moby's MCP server
(`https://mcp.moby.win/mcp`). Solana only, funded with USDC in your Moby agent wallet.

## Strategy (runs every 15 minutes)

1. **Safety first.** Stops if `KILL_SWITCH=true` or trading is paused in the Moby app.
   Moby's own limits (max per trade, daily volume) are always enforced by Moby and
   also respected by the bot.
2. **Manage what it holds.**
   - Every position gets a stop-loss (−25%) and take-profit (+50%) placed on Moby.
   - It sells early if tracked whales turn to net selling on both 1h and 4h.
3. **Find a trend Moby's whales are backing.** From the top 30 on Moby's leaderboard, a coin must:
   - be green safety, $1M–$500M market cap, with $200k+ liquidity;
   - be up on 1h and 24h, but not more than +100% in 24h (no chasing);
   - have tracked whales net buying over 1h *and* 4h, with 3+ distinct whale buyers;
   - pass Moby's trade brief: Smart Money Read = buying, mint/freeze renounced, no honeypot.
4. **Buy the strongest one.** $20 per trade (or Moby's max, if lower), at most 1 buy per
   cycle, 3 open positions and $40 of buys a day, always keeping $5 cash.

All numbers live in `config.yaml`. **No strategy guarantees profit**: memecoins can drop fast, and
stop-losses can fill below their price in a crash. Start small.

## Run locally

```bash
pip install -r requirements.txt
export MOBY_API_KEY=moby_agent_...     # Moby > AI Trading > Agent settings. Never commit it.
pytest
python -m trading_bot.moby_client tools                 # list Moby tools
RUN_TOKEN=pick-a-secret python app.py                   # dry run, cycles every 15 min
```

`trading.dry_run: true` (default) reads Moby and logs every buy/sell/order it *would* send.
Go live with env `DRY_RUN=false`. Stop instantly with `KILL_SWITCH=true`, or pause AI Trading in the Moby app.

## Deploy (Cloud Run)

`cloudbuild.yaml` builds this folder and deploys the `jakes-trading-bot` service.

```bash
printf '%s' 'moby_agent_...' | gcloud secrets create moby-api-key --data-file=-
printf '%s' "$(openssl rand -hex 24)" | gcloud secrets create run-token --data-file=-
gcloud run services update jakes-trading-bot --region us-central1 \
  --update-secrets MOBY_API_KEY=moby-api-key:latest,RUN_TOKEN=run-token:latest \
  --min-instances 1 --max-instances 1 --no-cpu-throttling \
  --update-env-vars DRY_RUN=true
```

`--min-instances 1 --max-instances 1 --no-cpu-throttling` keeps exactly one copy running so the
15-minute loop runs and two copies never trade at once.

## Endpoints (send `Authorization: Bearer $RUN_TOKEN`)

| Method | Path      | Purpose                                   |
|--------|-----------|-------------------------------------------|
| GET    | `/health` | status, dry-run and kill-switch flags (no auth) |
| GET    | `/wallet` | Moby portfolio                            |
| GET    | `/last`   | what the last cycle did and why           |
| POST   | `/run`    | run one cycle now                         |
| GET    | `/tools`  | list Moby MCP tools                       |
