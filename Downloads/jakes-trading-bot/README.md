# Jake's Trading Bot — Moby automation

Automates trading through **Moby: Trade Smarter** (moby.win) using Moby's MCP
server at `https://mcp.moby.win/mcp`.

Each cycle the bot:
1. calls Moby's **signals** tool,
2. filters signals through risk rules (allow/block lists, $ per trade, trades per day),
3. calls Moby's **trade** tool for each one, or only logs it when `dry_run: true` (the default).

## Setup

1. In the Moby app, go to **Agent settings** and create an API key (`moby_agent_...`).
   Never commit it. Revoke any key that has been shared in a screenshot or chat.
2. Find out which tools Moby exposes:
   ```bash
   pip install -r requirements.txt
   export MOBY_API_KEY=moby_agent_...
   python -m trading_bot.moby_client tools
   python -m trading_bot.moby_client call <wallet_tool_name>     # read-only check
   ```
3. Put the tool names in `config.yaml` under `moby.tools` (`wallet`, `signals`, `trade`),
   and make `moby.trade_arguments` match the trade tool's input fields.
4. Run `pytest` and then do a dry run: `curl -X POST -H "Authorization: Bearer $RUN_TOKEN" localhost:8080/run`.
5. When the simulated trades look right, set `trading.dry_run: false` (or env `DRY_RUN=false`).

## Deploy (Cloud Run)

`cloudbuild.yaml` builds this folder and deploys the `jakes-trading-bot` service. Store secrets
in Secret Manager and attach them once:

```bash
printf '%s' 'moby_agent_...' | gcloud secrets create moby-api-key --data-file=-
printf '%s' "$(openssl rand -hex 24)" | gcloud secrets create run-token --data-file=-
gcloud run services update jakes-trading-bot --region us-central1 \
  --update-secrets MOBY_API_KEY=moby-api-key:latest,RUN_TOKEN=run-token:latest
```

To trade on a schedule, create a Cloud Scheduler job that POSTs to `/run` with the run token
(for example every 15 minutes), or set `schedule.interval_seconds`. With the in-container
loop, keep `--min-instances 1` so the container keeps running between requests.

## Endpoints

| Method | Path      | Auth      | Purpose                              |
|--------|-----------|-----------|--------------------------------------|
| GET    | `/health` | none      | status, dry-run flag, key present    |
| GET    | `/tools`  | RUN_TOKEN | list Moby MCP tools                  |
| GET    | `/wallet` | RUN_TOKEN | call the configured wallet tool      |
| POST   | `/run`    | RUN_TOKEN | run one trading cycle                |

Optional email summaries: set `ALERT_EMAIL_TO`, `SMTP_USER`, `SMTP_PASSWORD` (`SMTP_HOST`/`SMTP_PORT` default to Gmail).
