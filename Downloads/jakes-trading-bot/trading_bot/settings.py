from __future__ import annotations

import os
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path: str | os.PathLike | None = None) -> dict:
    path = Path(path or os.environ.get("BOT_CONFIG", DEFAULT_PATH))
    with open(path) as fh:
        cfg = yaml.safe_load(fh) or {}
    # Environment overrides so Cloud Run can change behaviour without a rebuild.
    if "DRY_RUN" in os.environ:
        cfg.setdefault("trading", {})["dry_run"] = os.environ["DRY_RUN"].lower() in ("1", "true", "yes")
    for role in ("wallet", "signals", "trade"):
        env = os.environ.get(f"MOBY_TOOL_{role.upper()}")
        if env is not None:
            cfg.setdefault("moby", {}).setdefault("tools", {})[role] = env
    return cfg
