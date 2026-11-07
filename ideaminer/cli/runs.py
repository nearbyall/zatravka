"""Commands that run the pipeline or parts of it."""
from __future__ import annotations

import argparse

from ..config import load_config
from ..llm import BudgetExceeded
from ..pipeline import run_once
from ..pipeline.daemon import daemon
from ..pipeline.stages.classify import run_classify
from ..pipeline.stages.digest import run_digest
from ..pipeline.stages.explore import run_explore
from ..storage.directions import sync_directions
from ..storage.ideas import count_outdated, count_unclassified, forget_taxonomy, refresh_mentions, rescore_all
from ..storage.runs import abort_running, finish_run, release_lock, start_run
from ..storage.usage import spent_in_run
from .common import csv_list, load, print_stats


def cmd_run(args: argparse.Namespace) -> None:
    _, ctx = load(args)
    print_stats(run_once(ctx, trigger="manual", only_sources=csv_list(args.sources),
                         only_directions=csv_list(args.directions), use_llm=not args.no_llm,
                         explore=True if args.explore else None))
    if args.digest:
        print(f"Дайджест: {run_digest(ctx, days=1)}")


def cmd_process(args: argparse.Namespace) -> None:
    _, ctx = load(args)
    print_stats(run_once(ctx, trigger="process", collect=False, only_sources=csv_list(args.sources),
                         only_directions=csv_list(args.directions)))


def cmd_collect(args: argparse.Namespace) -> None:
    _, ctx = load(args)
    print_stats(run_once(ctx, trigger="collect", only_sources=csv_list(args.sources),
                         only_directions=csv_list(args.directions), use_llm=False))


def cmd_explore(args: argparse.Namespace) -> None:
    cfg, ctx = load(args)
    sync_directions(cfg, ctx.db)
    ctx.set_run(start_run(ctx.db, "explore"))
    created = run_explore(ctx)
    finish_run(ctx.db, ctx.run_id, "ok", None, ctx.stats)
    print("Новые направления:", ", ".join(created) or "нет")


def cmd_reclassify(args: argparse.Namespace) -> None:
    _, ctx = load(args)
    version = ctx.cfg.taxonomy.version()
    print(f"К переразметке: {forget_taxonomy(ctx.db) if args.all else count_outdated(ctx.db, version)}")
    run_id = start_run(ctx.db, "reclassify")
    ctx.set_run(run_id)
    try:
        while count_unclassified(ctx.db, version):
            before = ctx.stats.get("classify", {}).get("ideas", 0)
            run_classify(ctx)
            if ctx.stats.get("classify", {}).get("ideas", 0) == before:
                break
    except BudgetExceeded as e:
        print(f"Остановлено: {e}")
    refresh_mentions(ctx.db, ctx.cfg.scoring.mention_bonus)
    finish_run(ctx.db, run_id, "ok", None, ctx.stats)
    print_stats({"stats": ctx.stats, "cost_usd": spent_in_run(ctx.db, run_id)})


def cmd_rescore(args: argparse.Namespace) -> None:
    _, ctx = load(args)
    scoring = ctx.cfg.scoring
    n = rescore_all(ctx.db, scoring.weights, scoring.mention_bonus)
    refresh_mentions(ctx.db, scoring.mention_bonus)
    print(f"Пересчитано идей: {n}")


def cmd_digest(args: argparse.Namespace) -> None:
    _, ctx = load(args)
    path = run_digest(ctx, days=args.days, send=True if args.send else None)
    print(path.read_text(encoding="utf-8"))
    print(f"\n→ {path}")


def cmd_unlock(args: argparse.Namespace) -> None:
    _, ctx = load(args)
    release_lock(ctx.db)
    abort_running(ctx.db)
    print("Блокировка снята, зависшие прогоны помечены как aborted")


def cmd_daemon(args: argparse.Namespace) -> None:
    daemon(load_config(args.config), args.config)
