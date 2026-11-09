"""Commands that read, export and import data."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from ..core.models import FeedSpec
from ..core.taxonomy import resolve_taxonomy
from ..export import FORMATS, export_file_name, export_rows
from ..pipeline.stages.collect import run_prefilter
from ..storage.directions import direction_stats
from ..storage.idea_queries import query_ideas
from ..storage.ideas import count_ideas, idea_breakdown, region_breakdown
from ..storage.posts import insert_posts, post_counts_by
from ..storage.usage import llm_totals, spent_today
from .common import load

IDEA_BREAKDOWNS = ("market", "nature", "sphere", "insight_type", "origin_region")


def _filters(args: argparse.Namespace) -> dict[str, Any]:
    return dict(market=args.market, sphere=args.sphere, nature=args.nature, insight=args.insight,
                platform=args.platform, q=args.q, min_score=args.min_score, days=args.days, status=args.status,
                sort=args.sort, region=args.region, origin=args.origin, with_global=not args.no_global)


def cmd_ideas(args: argparse.Namespace) -> None:
    _, ctx = load(args)
    rows, total = query_ideas(ctx.db, limit=args.limit, **_filters(args))
    print(f"Идей: {total} (показано {len(rows)})\n")
    for r in rows:
        mentions = f" ×{r['mentions']}" if r["mentions"] > 1 else ""
        print(f"[{r['score']:4.1f}] #{r['id']} {r['title']}{mentions}")
        regions = ", ".join(r.get("regions") or []) or "—"
        print(f"       {r['market']} · {r['sphere']} · {r['nature']} · {r['business_model']} · {r['insight_type']}"
              f" · регионы: {regions}")
        if r["one_liner"]:
            print(f"       {r['one_liner']}")
        print()


def cmd_export(args: argparse.Namespace) -> None:
    cfg, ctx = load(args)
    rows, _ = query_ideas(ctx.db, limit=args.limit, **_filters(args))
    items = export_rows(ctx.db, rows)
    out = Path(args.out) if args.out else cfg.path(cfg.storage.exports_dir) / export_file_name(args.format)
    out.parent.mkdir(parents=True, exist_ok=True)
    content = FORMATS[args.format].render(items, resolve_taxonomy(cfg))
    if isinstance(content, bytes):
        out.write_bytes(content)
    else:
        out.write_text(content, encoding="utf-8")
    print(f"Выгружено {len(items)} идей → {out}")


def cmd_import(args: argparse.Namespace) -> None:
    _, ctx = load(args)
    inbox = ctx.sources["inbox"]
    for f in args.files:
        posts = inbox.fetch(FeedSpec("inbox", "file", str(Path(f).resolve()), args.direction))
        for p in posts:
            p.direction = args.direction if args.direction != "inbox" else (p.direction or "inbox")
        print(f"{f}: прочитано {len(posts)}, новых {insert_posts(ctx.db, posts, None)}")
    run_prefilter(ctx)
    print("Готово. Обработать: zatravka process")


def cmd_stats(args: argparse.Namespace) -> None:
    _, ctx = load(args)
    db = ctx.db
    for title, column in (("Посты по статусам:", "status"), ("Посты по платформам:", "platform")):
        print(title)
        for key, n in post_counts_by(db, column).items():
            print(f"  {key:10} {n}")
    print(f"Идей: {count_ideas(db)}, уникальных (кластеров): {count_ideas(db, clusters_only=True)}")
    for column in IDEA_BREAKDOWNS:
        print(f"  {column}: " + ", ".join(f"{r['k']}={r['n']}" for r in idea_breakdown(db, column)))
    print("  regions: " + ", ".join(f"{r['k']}={r['n']}" for r in region_breakdown(db)))
    print("Направления (пост → триаж → хорошие идеи):")
    for key, s in sorted(direction_stats(db, ctx.cfg).items(), key=lambda kv: -kv[1].get("yield", 0)):
        print(f"  {key:24} {int(s['posts']):5} → {int(s['triaged']):4} → {int(s.get('good', 0)):3}"
              f"  yield={s['yield']}")
    calls, total = llm_totals(db)
    print(f"ИИ: вызовов {calls}, всего ${total:.4f}, сегодня ${spent_today(db):.4f}")
