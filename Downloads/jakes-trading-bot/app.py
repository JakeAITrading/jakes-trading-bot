"""Web entrypoint for Cloud Run.

GET  /health  - liveness check (no auth)
GET  /tools   - list the Moby MCP tools         (needs RUN_TOKEN)
GET  /wallet  - Moby portfolio                 (needs RUN_TOKEN)
GET  /last    - report from the last cycle     (needs RUN_TOKEN)
POST /run     - run one trading cycle           (needs RUN_TOKEN)

Send the token as "Authorization: Bearer <RUN_TOKEN>".
"""
from __future__ import annotations

import hmac
import logging
import os
import threading
import time

from flask import Flask, abort, jsonify, request

from trading_bot.email_alerts import EmailNotifier
from trading_bot.engine import TradingEngine
from trading_bot.moby_client import MobyClient
from trading_bot.settings import load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

app = Flask(__name__)
_lock = threading.Lock()
_engine: TradingEngine | None = None


def engine() -> TradingEngine:
    global _engine
    if _engine is None:
        cfg = load_config()
        client = MobyClient(url=cfg.get("moby", {}).get("mcp_url"))
        _engine = TradingEngine(client, cfg, EmailNotifier())
    return _engine


def _require_token() -> None:
    expected = os.environ.get("RUN_TOKEN", "")
    given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not expected or not hmac.compare_digest(given, expected):
        abort(401)


@app.get("/health")
def health():
    cfg = load_config()
    return jsonify(ok=True, dry_run=cfg.get("trading", {}).get("dry_run", True),
                   kill_switch=cfg.get("trading", {}).get("kill_switch", False),
                   moby_key_set=bool(os.environ.get("MOBY_API_KEY")))


@app.get("/tools")
def tools():
    _require_token()
    return jsonify([{"name": t["name"], "description": t.get("description", "")}
                    for t in engine().client.list_tools()])


@app.get("/wallet")
def wallet():
    _require_token()
    return jsonify(engine().client.call_tool("get_my_portfolio"))


@app.get("/last")
def last():
    _require_token()
    return jsonify(engine().last_report)


@app.post("/run")
def run():
    _require_token()
    with _lock:
        return jsonify(engine().run_cycle())


def _loop(interval: int) -> None:
    while True:
        try:
            with _lock:
                log.info("cycle: %s", engine().run_cycle())
        except Exception:
            log.exception("Trading cycle failed")
        time.sleep(interval)


_interval = int(load_config().get("schedule", {}).get("interval_seconds", 0) or 0)
if _interval > 0:
    threading.Thread(target=_loop, args=(_interval,), daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
