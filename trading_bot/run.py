"""Run ONE trading cycle, save the bot's memory and log, write last_run.txt.

  python -m trading_bot.run            # mode comes from config.yaml / DRY_RUN (practice by default)

Does nothing if a STOP or REMOTE_STOP file exists in the repo root.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sys

from trading_bot import main_learner, store
from trading_bot.engine import TradingEngine
from trading_bot.moby_client import MobyClient
from trading_bot.settings import ROOT, load_config, load_env

log = logging.getLogger("run")


def stop_reason() -> str | None:
    for name in ("STOP", "REMOTE_STOP"):
        if (ROOT / name).exists():
            return f"{name} file present, no trading"
    return None


def write_last_run(line: str) -> None:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (ROOT / "last_run.txt").write_text(f"{stamp} {line}\n")
    print(f"{stamp} {line}")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_env()
    reason = stop_reason()
    if reason:
        write_last_run(reason)
        return 0
    cfg = main_learner.apply(load_config())   # learned settings override config.yaml
    try:
        client = MobyClient(url=cfg.get("moby", {}).get("mcp_url"))
        engine = TradingEngine(client, cfg, store.load_state())
        report = engine.run_cycle()
    except Exception as exc:
        log.exception("cycle failed")
        write_last_run(f"FAILED: {exc}")
        return 1
    store.save_state(engine.state)
    try:
        main_learner.maybe_learn(client, load_config())
    except Exception as exc:
        log.warning("main learning failed: %s", exc)
    mode = "practice" if report["dry_run"] else "LIVE"
    for action in report["actions"]:
        store.append("trades.jsonl", {"mode": mode, **action})
    try:
        summary = (client.call_tool("get_my_portfolio") or {}).get("summary", {})
        store.append("balances.jsonl", {"time": report["time"], "mode": mode,
                                        "wallet_usd": summary.get("hero_usd"), "cash_usd": report.get("cash_usd")})
    except Exception as exc:  # the snapshot is nice-to-have
        log.warning("portfolio snapshot failed: %s", exc)
    acts = "; ".join(a["why"] + f" [{a['status']}]" for a in report["actions"]) or "no trades"
    notes = (" | " + "; ".join(report["notes"])) if report["notes"] else ""
    write_last_run(f"{mode}: {acts}{notes}")
    log.info("report: %s", json.dumps({k: v for k, v in report.items() if k != "screened_out"}, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
