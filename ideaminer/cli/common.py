from __future__ import annotations

import argparse
import logging
from typing import Any

import yaml

from ..config import Config, load_config
from ..pipeline import Ctx, make_ctx

NOISY_LOGGERS = ("httpx", "httpcore", "uvicorn.access")


def parse_overrides(pairs: list[str] | None) -> dict[str, Any]:
    """["triage.max_posts_per_run=20", "planner.random_directions=1"] → a nested dict."""
    out: dict[str, Any] = {}
    for pair in pairs or []:
        key, _, raw = pair.partition("=")
        node = out
        *parents, leaf = key.strip().split(".")
        for p in parents:
            node = node.setdefault(p, {})
        node[leaf] = yaml.safe_load(raw)
    return out


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname).1s %(message)s", datefmt="%H:%M:%S")
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def csv_list(value: str | None) -> list[str] | None:
    return [x.strip() for x in value.split(",") if x.strip()] if value else None


def load(args: argparse.Namespace) -> tuple[Config, Ctx]:
    cfg = load_config(args.config, parse_overrides(args.set))
    return cfg, make_ctx(cfg, seed=getattr(args, "seed", None))


def print_stats(result: dict[str, Any]) -> None:
    if "run_id" in result:
        print(f"\nПрогон #{result['run_id']}: {result['status']}")
    for stage, s in (result.get("stats") or {}).items():
        print(f"  {stage:10} {s}")
    print(f"  расход     ${result.get('cost_usd', 0):.4f}")


def mark(ok: bool | None) -> str:
    return {True: "✓", False: "✗", None: "?"}[ok]
