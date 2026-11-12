"""Commands that set up and check the environment."""
from __future__ import annotations

import argparse
from pathlib import Path

import httpx

from ..config import Config, load_config
from ..pipeline import Ctx
from ..sources.threads import ensure_token, exchange_token, whoami
from ..storage.db import DB
from ..storage.directions import sync_directions
from ..storage.snapshot import write_snapshot
from ..storage.usage import spent_today
from .common import load, mark

CHECK_TIMEOUT_S = 15
X_USAGE_URL = "https://api.x.com/2/usage/tweets"
SHOWCASE_DB = "data/showcase/ideas.db"
SHOWCASE_PORT = 8620


def cmd_init(args: argparse.Namespace) -> None:
    cfg, ctx = load(args)
    sync_directions(cfg, ctx.db)
    for d in (cfg.storage.inbox_dir, cfg.storage.exports_dir, cfg.storage.digests_dir):
        cfg.path(d).mkdir(parents=True, exist_ok=True)
    print(f"База: {cfg.db_path}")
    doctor(ctx)


def cmd_doctor(args: argparse.Namespace) -> None:
    doctor(load(args)[1])


def doctor(ctx: Ctx) -> None:
    cfg, s = ctx.cfg, ctx.cfg.secrets
    print("Источники:")
    for name, src in ctx.sources.items():
        ok, why = src.status()
        print(f"  {mark(ok)} {name:8} {why}")
    print("ИИ:")
    print(_deepseek_line(cfg))
    print(f"  {mark(bool(s.replicate_api_token))} replicate "
          f"{cfg.llm.replicate.model if s.replicate_api_token else 'нет REPLICATE_API_TOKEN'}")
    if s.x_bearer_token or s.threads_access_token:
        print("Ключи площадок:")
    if s.x_bearer_token:
        print(_x_line(ctx))
    if s.threads_access_token:
        print(_threads_line(ctx))
    print(f"Бюджет: сегодня потрачено ${spent_today(ctx.db):.4f} из ${cfg.budget.daily_usd:.2f}")


def _deepseek_line(cfg: Config) -> str:
    key = cfg.secrets.deepseek_api_key
    if not key:
        return "  ✗ deepseek  нет DEEPSEEK_API_KEY"
    try:
        r = httpx.get(f"{cfg.llm.deepseek.base_url}/user/balance", timeout=CHECK_TIMEOUT_S,
                      headers={"Authorization": f"Bearer {key}"})
        balance = ", ".join(f"{b['total_balance']} {b['currency']}" for b in r.json().get("balance_infos", []))
        return f"  ✓ deepseek  ключ есть, баланс: {balance}"
    except Exception as e:  # noqa: BLE001 - a diagnostic must report, not fail
        return f"  ? deepseek  ключ есть, баланс не получен: {e}"


def _x_line(ctx: Ctx) -> str:
    """Project usage is free to read: it does not fetch any posts."""
    cfg = ctx.cfg
    try:
        r = httpx.get(X_USAGE_URL, timeout=CHECK_TIMEOUT_S,
                      headers={"Authorization": f"Bearer {cfg.secrets.x_bearer_token}"})
    except Exception as e:  # noqa: BLE001
        return f"  ? x        проверить не удалось: {e}"
    if r.status_code != 200:
        return f"  ✗ x        API ответил {r.status_code}: {r.text[:160]}"
    d = r.json().get("data", {})
    return (f"  ✓ x        токен рабочий; прочитано постов в периоде: {d.get('project_usage', '?')}"
            f" из {d.get('project_cap', '?')}; сегодня zatravka прочитал"
            f" {ctx.sources['x'].reads_today()} из {cfg.sources.x.daily_read_limit or '∞'}")


def _threads_line(ctx: Ctx) -> str:
    try:
        me = whoami(ensure_token(ctx.cfg, ctx.db) or "")
        return f"  ✓ threads  токен рабочий, аккаунт @{me.get('username')}"
    except Exception as e:  # noqa: BLE001
        return f"  ✗ threads  токен не принят: {e}"


def cmd_threads_token(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    data = exchange_token(args.short_token, args.app_secret)
    token = data["access_token"]
    me = whoami(token)
    print(f"Долгий токен получен для @{me.get('username')}, действует {int(data.get('expires_in', 0)) // 86400} дн.")
    if not args.save:
        print(f"THREADS_ACCESS_TOKEN={token}")
        return
    env = cfg.base_dir / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    lines = [line for line in lines if not line.startswith("THREADS_ACCESS_TOKEN=")]
    env.write_text("\n".join([*lines, f"THREADS_ACCESS_TOKEN={token}"]) + "\n", encoding="utf-8")
    env.chmod(0o600)
    print(f"Записан в {env}. Дальше демон продлевает его сам.")


def cmd_serve(args: argparse.Namespace) -> None:
    # FastAPI and uvicorn are loaded only by this command.
    import uvicorn

    from ..web.app import create_app

    cfg = load_config(args.config)
    uvicorn.run(create_app(cfg), host=args.host or cfg.web.host, port=args.port or cfg.web.port, log_level="warning")


def cmd_showcase(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    snapshot = Path(args.db) if args.db else cfg.path(SHOWCASE_DB)
    if args.action == "build":
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        db = DB(cfg.db_path)
        print(f"Снимок для витрины: {snapshot}, идей {write_snapshot(db, snapshot)}")
        db.close()
        return
    if not snapshot.exists():
        raise SystemExit(f"Нет снимка {snapshot}. Сначала: zatravka showcase build")
    import uvicorn

    from ..web.app import create_showcase_app

    uvicorn.run(create_showcase_app(cfg, snapshot), host=args.host or "127.0.0.1", port=args.port or SHOWCASE_PORT,
                log_level="warning")
