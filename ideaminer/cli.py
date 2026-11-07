"""Командная строка: ideaminer <команда>. Подробности — ideaminer <команда> -h."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import yaml

from .config import Config, load_config
from .context import Ctx, make_ctx
from .core.dates import utcnow


def parse_overrides(pairs: list[str] | None) -> dict[str, Any]:
    """--set triage.max_posts_per_run=20 --set planner.random_directions=1 → вложенный dict."""
    out: dict[str, Any] = {}
    for pair in pairs or []:
        key, _, raw = pair.partition("=")
        node = out
        parts = key.strip().split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = yaml.safe_load(raw)
    return out


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname).1s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _csv(v: str | None) -> list[str] | None:
    return [x.strip() for x in v.split(",") if x.strip()] if v else None


def _ctx(args: argparse.Namespace) -> tuple[Config, Ctx]:
    cfg = load_config(args.config, parse_overrides(args.set))
    return cfg, make_ctx(cfg, seed=getattr(args, "seed", None))


# ── команды ─────────────────────────────────────────────────────────────
def cmd_init(args: argparse.Namespace) -> None:
    from .planner import sync_directions
    cfg, ctx = _ctx(args)
    sync_directions(cfg, ctx.db)
    for d in ("inbox_dir", "exports_dir", "digests_dir"):
        cfg.path(getattr(cfg.storage, d)).mkdir(parents=True, exist_ok=True)
    print(f"База: {cfg.db_path}")
    cmd_doctor(args, ctx)


def cmd_doctor(args: argparse.Namespace, ctx: Ctx | None = None) -> None:
    import httpx
    if ctx is None:
        _, ctx = _ctx(args)
    cfg = ctx.cfg
    print("Источники:")
    for name, src in ctx.sources.items():
        ok, why = src.status()
        print(f"  {'✓' if ok else '✗'} {name:8} {why}")
    print("ИИ:")
    s = cfg.secrets
    if s.deepseek_api_key:
        try:
            r = httpx.get(f"{cfg.llm.deepseek.base_url}/user/balance", timeout=15,
                          headers={"Authorization": f"Bearer {s.deepseek_api_key}"})
            bal = ", ".join(f"{b['total_balance']} {b['currency']}" for b in r.json().get("balance_infos", []))
            print(f"  ✓ deepseek  ключ есть, баланс: {bal}")
        except Exception as e:  # noqa: BLE001
            print(f"  ? deepseek  ключ есть, баланс не получен: {e}")
    else:
        print("  ✗ deepseek  нет DEEPSEEK_API_KEY")
    print(f"  {'✓' if s.replicate_api_token else '✗'} replicate {cfg.llm.replicate.model if s.replicate_api_token else 'нет REPLICATE_API_TOKEN'}")
    if s.x_bearer_token or s.threads_access_token:
        print("Ключи площадок:")
    if s.x_bearer_token:
        try:  # учёт использования проекта — без чтения постов, поэтому бесплатно
            r = httpx.get("https://api.x.com/2/usage/tweets", timeout=15,
                          headers={"Authorization": f"Bearer {s.x_bearer_token}"})
            if r.status_code == 200:
                d = r.json().get("data", {})
                print(f"  ✓ x        токен рабочий; прочитано постов в периоде: {d.get('project_usage', '?')}"
                      f" из {d.get('project_cap', '?')}; сегодня ideaminer прочитал"
                      f" {ctx.sources['x'].reads_today()} из {cfg.sources.x.daily_read_limit or '∞'}")
            else:
                print(f"  ✗ x        API ответил {r.status_code}: {r.text[:160]}")
        except Exception as e:  # noqa: BLE001
            print(f"  ? x        проверить не удалось: {e}")
    if s.threads_access_token:
        from .sources.threads import ensure_token, whoami
        try:
            me = whoami(ensure_token(cfg, ctx.db) or "")
            print(f"  ✓ threads  токен рабочий, аккаунт @{me.get('username')}")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ threads  токен не принят: {e}")
    print(f"Бюджет: сегодня потрачено ${ctx.llm.spent_today():.4f} из ${cfg.budget.daily_usd:.2f}")


def cmd_run(args: argparse.Namespace) -> None:
    from .pipeline import run_once
    _, ctx = _ctx(args)
    res = run_once(ctx, trigger="manual", only_sources=_csv(args.sources), only_directions=_csv(args.directions),
                   use_llm=not args.no_llm, explore=True if args.explore else None)
    print_stats(res)
    if args.digest:
        from .stages.digest import run_digest
        print(f"Дайджест: {run_digest(ctx, days=1)}")


def cmd_process(args: argparse.Namespace) -> None:
    from .pipeline import run_once
    _, ctx = _ctx(args)
    print_stats(run_once(ctx, trigger="process", collect=False, only_sources=_csv(args.sources),
                         only_directions=_csv(args.directions)))


def cmd_collect(args: argparse.Namespace) -> None:
    from .pipeline import run_once
    _, ctx = _ctx(args)
    print_stats(run_once(ctx, trigger="collect", only_sources=_csv(args.sources),
                         only_directions=_csv(args.directions), use_llm=False))


def cmd_explore(args: argparse.Namespace) -> None:
    from .planner import sync_directions
    from .stages.explore import run_explore
    cfg, ctx = _ctx(args)
    sync_directions(cfg, ctx.db)
    run_id = ctx.db.start_run("explore")
    ctx.set_run(run_id)
    created = run_explore(ctx)
    ctx.db.finish_run(run_id, "ok", None, ctx.stats)
    print("Новые направления:", ", ".join(created) or "нет")


def cmd_reclassify(args: argparse.Namespace) -> None:
    from .llm import BudgetExceeded
    from .stages.classify import reset_classification, run_classify
    from .stages.dedup import refresh_mentions
    _, ctx = _ctx(args)
    n = reset_classification(ctx, only_outdated=not args.all)
    print(f"К переразметке: {n}")
    run_id = ctx.db.start_run("reclassify")
    ctx.set_run(run_id)
    try:
        while ctx.db.scalar("SELECT COUNT(*) FROM ideas WHERE classified_at IS NULL OR taxonomy_version IS NOT ?",
                            (ctx.cfg.taxonomy.version(),), 0):
            before = ctx.stats.get("classify", {}).get("ideas", 0)
            run_classify(ctx)
            if ctx.stats.get("classify", {}).get("ideas", 0) == before:
                break
    except BudgetExceeded as e:
        print(f"Остановлено: {e}")
    refresh_mentions(ctx)
    ctx.db.finish_run(run_id, "ok", None, ctx.stats)
    print_stats({"stats": ctx.stats, "cost_usd": ctx.db.spent_in_run(run_id)})


def cmd_rescore(args: argparse.Namespace) -> None:
    from .stages.classify import rescore_all
    from .stages.dedup import refresh_mentions
    _, ctx = _ctx(args)
    n = rescore_all(ctx)
    refresh_mentions(ctx)
    print(f"Пересчитано идей: {n}")


def cmd_digest(args: argparse.Namespace) -> None:
    from .stages.digest import run_digest
    _, ctx = _ctx(args)
    path = run_digest(ctx, days=args.days, send=True if args.send else None)
    print(path.read_text(encoding="utf-8"))
    print(f"\n→ {path}")


def _filters(args: argparse.Namespace) -> dict[str, Any]:
    return dict(market=args.market, sphere=args.sphere, nature=args.nature, insight=args.insight,
                platform=args.platform, q=args.q, min_score=args.min_score, days=args.days,
                status=args.status, sort=args.sort, region=args.region, origin=args.origin,
                with_global=not args.no_global)


def cmd_ideas(args: argparse.Namespace) -> None:
    from .export import query_ideas
    _, ctx = _ctx(args)
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
    from datetime import datetime
    from .export import export_rows, query_ideas, to_csv, to_json, to_markdown, to_xlsx
    from .core.taxonomy import resolve_taxonomy
    cfg, ctx = _ctx(args)
    rows, _ = query_ideas(ctx.db, limit=args.limit, **_filters(args))
    items = export_rows(ctx.db, rows)
    out = Path(args.out) if args.out else cfg.path(cfg.storage.exports_dir) / f"ideas-{datetime.now():%Y%m%d-%H%M}.{args.format}"
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "xlsx":
        out.write_bytes(to_xlsx(items, resolve_taxonomy(cfg)))
    else:
        out.write_text({"csv": to_csv, "json": to_json, "md": to_markdown}[args.format](items), encoding="utf-8")
    print(f"Выгружено {len(items)} идей → {out}")


def cmd_import(args: argparse.Namespace) -> None:
    from .core.models import FeedSpec
    from .stages.collect import insert_posts, run_prefilter
    _, ctx = _ctx(args)
    src = ctx.sources["inbox"]
    for f in args.files:
        posts = src.fetch(FeedSpec("inbox", "file", str(Path(f).resolve()), args.direction))
        for p in posts:
            p.direction = args.direction if args.direction != "inbox" else (p.direction or "inbox")
        print(f"{f}: прочитано {len(posts)}, новых {insert_posts(ctx.db, posts, None)}")
    run_prefilter(ctx)
    print("Готово. Обработать: ideaminer process")


def cmd_stats(args: argparse.Namespace) -> None:
    from .planner import direction_stats
    _, ctx = _ctx(args)
    db = ctx.db
    print("Посты по статусам:")
    for r in db.query("SELECT status, COUNT(*) n FROM posts GROUP BY status ORDER BY n DESC"):
        print(f"  {r['status']:10} {r['n']}")
    print("Посты по платформам:")
    for r in db.query("SELECT platform, COUNT(*) n FROM posts GROUP BY platform ORDER BY n DESC"):
        print(f"  {r['platform']:10} {r['n']}")
    n_ideas = db.scalar("SELECT COUNT(*) FROM ideas", (), 0)
    n_cl = db.scalar("SELECT COUNT(*) FROM ideas WHERE cluster_id=id", (), 0)
    print(f"Идей: {n_ideas}, уникальных (кластеров): {n_cl}")
    for col in ("market", "nature", "sphere", "insight_type", "origin_region"):
        rows = db.query(f"SELECT {col} AS k, COUNT(*) n FROM ideas WHERE cluster_id=id GROUP BY {col} ORDER BY n DESC")
        print(f"  {col}: " + ", ".join(f"{r['k']}={r['n']}" for r in rows))
    rows = db.query("SELECT j.value AS k, COUNT(*) n FROM ideas i, json_each(COALESCE(i.regions_json,'[]')) j"
                    " WHERE i.cluster_id=i.id GROUP BY j.value ORDER BY n DESC")
    print("  regions: " + ", ".join(f"{r['k']}={r['n']}" for r in rows))
    print("Направления (пост → триаж → хорошие идеи):")
    for k, s in sorted(direction_stats(db, ctx.cfg).items(), key=lambda kv: -kv[1].get("yield", 0)):
        print(f"  {k:24} {int(s['posts']):5} → {int(s['triaged']):4} → {int(s.get('good', 0)):3}  yield={s['yield']}")
    total = db.scalar("SELECT SUM(cost_usd) FROM llm_calls", (), 0.0)
    calls = db.scalar("SELECT COUNT(*) FROM llm_calls", (), 0)
    print(f"ИИ: вызовов {calls}, всего ${total:.4f}, сегодня ${ctx.llm.spent_today():.4f}")


def cmd_unlock(args: argparse.Namespace) -> None:
    from .pipeline import release_lock
    _, ctx = _ctx(args)
    release_lock(ctx)
    ctx.db.execute("UPDATE runs SET status='aborted', finished_at=? WHERE status='running'", (utcnow(),))
    print("Блокировка снята, зависшие прогоны помечены как aborted")


def cmd_threads_token(args: argparse.Namespace) -> None:
    from .sources.threads import exchange_token, whoami
    cfg = load_config(args.config)
    data = exchange_token(args.short_token, args.app_secret)
    token = data["access_token"]
    days = int(data.get("expires_in", 0)) // 86400
    me = whoami(token)
    print(f"Долгий токен получен для @{me.get('username')}, действует {days} дн.")
    if args.save:
        env = cfg.base_dir / ".env"
        lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
        lines = [l for l in lines if not l.startswith("THREADS_ACCESS_TOKEN=")] + [f"THREADS_ACCESS_TOKEN={token}"]
        env.write_text("\n".join(lines) + "\n", encoding="utf-8")
        env.chmod(0o600)
        print(f"Записан в {env}. Дальше демон продлевает его сам.")
    else:
        print(f"THREADS_ACCESS_TOKEN={token}")


def cmd_accounts(args: argparse.Namespace) -> None:
    from .accounts import add_account, list_accounts, normalize_username
    from .planner import sync_directions
    cfg, ctx = _ctx(args)
    sync_directions(cfg, ctx.db)
    act = args.action
    if act == "list":
        rows = list_accounts(ctx.db, "threads")
        if not rows:
            print("Аккаунтов нет. Добавить: ideaminer accounts add @имя --direction ключ;"
                  " подобрать ИИ: ideaminer accounts discover --direction ключ")
        for r in rows:
            state = "вкл " if r["enabled"] else "выкл"
            last = (r["last_fetched_at"] or "—")[:16].replace("T", " ")
            print(f"  {state} @{r['username']:<28} {r['direction'] or 'общий':<22} {r['origin']:<7}"
                  f" читали: {last}, раз: {r['fetches']}{'  — ' + r['note'] if r['note'] else ''}")
    elif act == "add":
        for raw in args.names:
            ok = add_account(ctx.db, "threads", raw, args.direction, origin="manual")
            print(f"  {'✓' if ok else '✗'} {raw}" + ("" if ok else " — не похоже на имя аккаунта"))
    elif act == "remove":
        for raw in args.names:
            name = normalize_username(raw)
            n = ctx.db.execute("DELETE FROM accounts WHERE platform='threads' AND username=?", (name,)).rowcount
            print(f"  {'удалён' if n else 'не найден'} @{name}")
    elif act == "check":
        from .stages.discover import check_account
        for raw in args.names:
            name = normalize_username(raw) or raw
            ok, why = check_account(ctx, name, args.direction)
            print(f"  {'✓' if ok else '✗'} @{name}: {why}")
    elif act == "discover":
        from .llm import BudgetExceeded
        from .stages.discover import discover_accounts
        keys = _csv(args.direction) or ["region_cis"]
        for key in keys:
            try:
                added = discover_accounts(ctx, key, args.count)
            except (BudgetExceeded, ValueError) as e:
                print(f"{key}: {e}")
                continue
            print(f"{key}: добавлено {len(added)}: " + (", ".join("@" + a for a in added) or "—"))


def cmd_daemon(args: argparse.Namespace) -> None:
    from .pipeline import daemon
    cfg = load_config(args.config)
    daemon(cfg, args.config)


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn
    from .web.app import create_app
    cfg = load_config(args.config)
    uvicorn.run(create_app(cfg), host=args.host or cfg.web.host, port=args.port or cfg.web.port,
                log_level="warning")


def print_stats(res: dict[str, Any]) -> None:
    if "run_id" in res:
        print(f"\nПрогон #{res['run_id']}: {res['status']}")
    for stage, s in (res.get("stats") or {}).items():
        print(f"  {stage:10} {s}")
    print(f"  расход     ${res.get('cost_usd', 0):.4f}")


# ── разбор аргументов ───────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ideaminer", description="Поиск продуктовых идей в Reddit, X и Threads")
    p.add_argument("-c", "--config", help="путь к config.yaml (по умолчанию ./config.yaml или $IDEAMINER_CONFIG)")
    p.add_argument("--set", action="append", metavar="KEY=VALUE",
                   help="переопределить настройку, напр. --set triage.max_posts_per_run=20")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="создать базу и проверить окружение").set_defaults(fn=cmd_init)
    sub.add_parser("doctor", help="проверить источники, ключи и бюджет").set_defaults(fn=cmd_doctor)

    r = sub.add_parser("run", help="полный прогон: сбор → ИИ-обработка")
    r.add_argument("--sources", help="только эти источники: reddit,x,threads,rss,inbox;"
                   " ИИ разбирает только их посты")
    r.add_argument("--directions", help="только эти направления (ключи через запятую);"
                   " ИИ разбирает только их посты, новые направления не ищутся")
    r.add_argument("--no-llm", action="store_true", help="только сбор и префильтр")
    r.add_argument("--explore", action="store_true", help="принудительно запустить ИИ-исследователя направлений")
    r.add_argument("--digest", action="store_true", help="после прогона собрать дайджест")
    r.add_argument("--seed", type=int, help="зерно случайности для воспроизводимого плана")
    r.set_defaults(fn=cmd_run)

    c = sub.add_parser("collect", help="только сбор постов, без ИИ")
    c.add_argument("--sources")
    c.add_argument("--directions")
    c.add_argument("--seed", type=int)
    c.set_defaults(fn=cmd_collect)

    pr = sub.add_parser("process", help="ИИ-обработка накопленной очереди без сбора")
    pr.add_argument("--sources", help="только посты этих источников")
    pr.add_argument("--directions", help="только посты этих направлений")
    pr.set_defaults(fn=cmd_process)
    sub.add_parser("explore", help="ИИ предлагает новые направления поиска").set_defaults(fn=cmd_explore)

    rc = sub.add_parser("reclassify", help="переразметить идеи после смены таксономии")
    rc.add_argument("--all", action="store_true", help="все идеи, а не только размеченные старой таксономией")
    rc.set_defaults(fn=cmd_reclassify)
    sub.add_parser("rescore", help="пересчитать баллы после смены весов (без ИИ)").set_defaults(fn=cmd_rescore)

    d = sub.add_parser("digest", help="дайджест лучших новых идей")
    d.add_argument("--days", type=float, default=1)
    d.add_argument("--send", action="store_true", help="отправить в Telegram")
    d.set_defaults(fn=cmd_digest)

    def add_filters(sp: argparse.ArgumentParser, limit: int) -> None:
        sp.add_argument("--market", help="B2B, B2C, B2B2C, C2C, B2G")
        sp.add_argument("--sphere", help="ключ сферы: games, beauty, business_ops…")
        sp.add_argument("--nature", help="digital, physical, hybrid, service")
        sp.add_argument("--insight", help="explicit, implicit, lateral")
        sp.add_argument("--platform", help="reddit, x, threads")
        sp.add_argument("--region", help="где запускать: cis, eu, north_america, latam, east_europe, mena, "
                                         "africa, south_asia, east_asia, oceania, global")
        sp.add_argument("--no-global", action="store_true", help="с --region: не показывать глобальные идеи")
        sp.add_argument("--origin", help="откуда пришёл сигнал (тот же список регионов)")
        sp.add_argument("--q", help="поиск по тексту")
        sp.add_argument("--min-score", type=float)
        sp.add_argument("--days", type=float, help="только за последние N дней")
        sp.add_argument("--status", default="active", help="active (по умолчанию), all, new, starred, rejected…")
        sp.add_argument("--sort", default="score", help="score, new, mentions, novelty, demand, feasibility")
        sp.add_argument("--limit", type=int, default=limit)

    i = sub.add_parser("ideas", help="показать идеи в терминале")
    add_filters(i, 20)
    i.set_defaults(fn=cmd_ideas)

    e = sub.add_parser("export", help="выгрузить идеи в Excel/CSV/JSON/Markdown")
    add_filters(e, 10000)
    e.add_argument("--format", choices=["xlsx", "csv", "json", "md"], default="xlsx")
    e.add_argument("--out")
    e.set_defaults(fn=cmd_export)

    im = sub.add_parser("import", help="импортировать посты из JSON/JSONL/CSV")
    im.add_argument("files", nargs="+")
    im.add_argument("--direction", default="inbox")
    im.set_defaults(fn=cmd_import)

    sub.add_parser("stats", help="статистика базы, направлений и расходов").set_defaults(fn=cmd_stats)
    sub.add_parser("daemon", help="работать по расписанию из config.yaml").set_defaults(fn=cmd_daemon)
    sub.add_parser("unlock", help="снять зависшую блокировку прогона").set_defaults(fn=cmd_unlock)

    ac = sub.add_parser("accounts", help="аккаунты Threads для чтения профилей (веб-режим и API)")
    ac.add_argument("action", choices=["list", "add", "remove", "check", "discover"],
                    help="list — список; add/remove/check — по именам; discover — подобрать ИИ с проверкой")
    ac.add_argument("names", nargs="*", help="@имя или ссылка на профиль")
    ac.add_argument("--direction", help="ключ направления (для discover — через запятую; по умолчанию region_cis)")
    ac.add_argument("--count", type=int, default=8, help="discover: сколько аккаунтов попросить у ИИ")
    ac.set_defaults(fn=cmd_accounts)

    tt = sub.add_parser("threads-token", help="обменять короткий токен Threads на долгий (60 дней)")
    tt.add_argument("short_token", help="короткий токен из OAuth или «Graph API Explorer»")
    tt.add_argument("--app-secret", required=True, help="секрет приложения Threads (App settings → Basic)")
    tt.add_argument("--save", action="store_true", help="записать токен в .env")
    tt.set_defaults(fn=cmd_threads_token)

    s = sub.add_parser("serve", help="веб-интерфейс")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        args.fn(args)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
