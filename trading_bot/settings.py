from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "config.yaml"


def _truthy(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def load_env() -> None:
    """Load secrets from ROOT/.env (gitignored) if present."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT / ".env")


def load_config(path: str | os.PathLike | None = None) -> dict:
    path = Path(path or os.environ.get("BOT_CONFIG", DEFAULT_PATH))
    with open(path) as fh:
        cfg = yaml.safe_load(fh) or {}
    trading = cfg.setdefault("trading", {})
    if "DRY_RUN" in os.environ:
        trading["dry_run"] = _truthy(os.environ["DRY_RUN"])
    if os.environ.get("MOBY_MCP_URL"):
        cfg.setdefault("moby", {})["mcp_url"] = os.environ["MOBY_MCP_URL"]
    return cfg
