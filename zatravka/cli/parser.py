from __future__ import annotations

import argparse

from .accounts import ACTIONS, cmd_accounts
from .ideas import cmd_export, cmd_ideas, cmd_import, cmd_stats
from .runs import (cmd_collect, cmd_daemon, cmd_digest, cmd_explore, cmd_process, cmd_reclassify, cmd_rescore,
                   cmd_run, cmd_unlock)
from .setup import cmd_doctor, cmd_init, cmd_serve, cmd_showcase, cmd_threads_token

REGIONS_HELP = "cis, eu, north_america, latam, east_europe, mena, africa, south_asia, east_asia, oceania, global"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zatravka", description="Поиск продуктовых идей в Reddit, X и Threads")
    p.add_argument("-c", "--config", help="путь к config.yaml (по умолчанию ./config.yaml или $ZATRAVKA_CONFIG)")
    p.add_argument("--set", action="append", metavar="KEY=VALUE",
                   help="переопределить настройку, напр. --set triage.max_posts_per_run=20")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="создать базу и проверить окружение").set_defaults(fn=cmd_init)
    sub.add_parser("doctor", help="проверить источники, ключи и бюджет").set_defaults(fn=cmd_doctor)
    _add_run_commands(sub)
    _add_idea_commands(sub)
    sub.add_parser("stats", help="статистика базы, направлений и расходов").set_defaults(fn=cmd_stats)
    sub.add_parser("daemon", help="работать по расписанию из config.yaml").set_defaults(fn=cmd_daemon)
    sub.add_parser("unlock", help="снять зависшую блокировку прогона").set_defaults(fn=cmd_unlock)
    _add_account_commands(sub)

    s = sub.add_parser("serve", help="веб-интерфейс")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_serve)

    sh = sub.add_parser("showcase", help="публичная витрина: только идеи, фильтры и выгрузки")
    sh.add_argument("action", choices=["build", "serve"],
                    help="build — собрать очищенный снимок базы; serve — показать его, только чтение")
    sh.add_argument("--db", help="путь к снимку (по умолчанию data/showcase/ideas.db)")
    sh.add_argument("--host")
    sh.add_argument("--port", type=int)
    sh.set_defaults(fn=cmd_showcase)
    return p


def _add_run_commands(sub: argparse._SubParsersAction) -> None:
    r = sub.add_parser("run", help="полный прогон: сбор → ИИ-обработка")
    r.add_argument("--sources", help="только эти источники: reddit,x,threads,rss,inbox; ИИ разбирает только их посты")
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


def _add_filters(sp: argparse.ArgumentParser, limit: int) -> None:
    sp.add_argument("--market", help="B2B, B2C, B2B2C, C2C, B2G")
    sp.add_argument("--sphere", help="ключ сферы: games, beauty, business_ops…")
    sp.add_argument("--nature", help="digital, physical, hybrid, service")
    sp.add_argument("--insight", help="explicit, implicit, lateral")
    sp.add_argument("--platform", help="reddit, x, threads")
    sp.add_argument("--region", help=f"где запускать: {REGIONS_HELP}")
    sp.add_argument("--no-global", action="store_true", help="с --region: не показывать глобальные идеи")
    sp.add_argument("--origin", help="откуда пришёл сигнал (тот же список регионов)")
    sp.add_argument("--q", help="поиск по тексту")
    sp.add_argument("--min-score", type=float)
    sp.add_argument("--days", type=float, help="только за последние N дней")
    sp.add_argument("--status", default="active", help="active (по умолчанию), all, new, starred, rejected…")
    sp.add_argument("--sort", default="score", help="score, new, mentions, novelty, demand, feasibility")
    sp.add_argument("--limit", type=int, default=limit)


def _add_idea_commands(sub: argparse._SubParsersAction) -> None:
    i = sub.add_parser("ideas", help="показать идеи в терминале")
    _add_filters(i, 20)
    i.set_defaults(fn=cmd_ideas)

    e = sub.add_parser("export", help="выгрузить идеи в Excel/CSV/JSON/Markdown")
    _add_filters(e, 10000)
    e.add_argument("--format", choices=["xlsx", "csv", "json", "md"], default="xlsx")
    e.add_argument("--out")
    e.set_defaults(fn=cmd_export)

    im = sub.add_parser("import", help="импортировать посты из JSON/JSONL/CSV")
    im.add_argument("files", nargs="+")
    im.add_argument("--direction", default="inbox")
    im.set_defaults(fn=cmd_import)


def _add_account_commands(sub: argparse._SubParsersAction) -> None:
    ac = sub.add_parser("accounts", help="аккаунты Threads для чтения профилей (веб-режим и API)")
    ac.add_argument("action", choices=list(ACTIONS),
                    help="list — список; add/remove/check — по именам; discover — подобрать ИИ с проверкой;"
                         " expand — авторы похожих тредов под лучшими постами направления, без ИИ")
    ac.add_argument("names", nargs="*", help="@имя или ссылка на профиль")
    ac.add_argument("--direction",
                    help="ключ направления (для discover и expand — через запятую; по умолчанию region_cis)")
    ac.add_argument("--count", type=int, default=8,
                    help="discover: сколько аккаунтов попросить у ИИ; expand: сколько страниц постов открыть")
    ac.set_defaults(fn=cmd_accounts)

    tt = sub.add_parser("threads-token", help="обменять короткий токен Threads на долгий (60 дней)")
    tt.add_argument("short_token", help="короткий токен из OAuth или «Graph API Explorer»")
    tt.add_argument("--app-secret", required=True, help="секрет приложения Threads (App settings → Basic)")
    tt.add_argument("--save", action="store_true", help="записать токен в .env")
    tt.set_defaults(fn=cmd_threads_token)
