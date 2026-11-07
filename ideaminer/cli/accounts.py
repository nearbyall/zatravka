"""The accounts command: the registry of Threads profiles to read."""
from __future__ import annotations

import argparse

from ..llm import BudgetExceeded
from ..pipeline import Ctx
from ..pipeline.stages.discover import check_account, discover_accounts
from ..storage.accounts import add_account, list_accounts, normalize_username, remove_account
from ..storage.directions import sync_directions
from .common import csv_list, load, mark

DEFAULT_DISCOVER_DIRECTION = "region_cis"


def cmd_accounts(args: argparse.Namespace) -> None:
    cfg, ctx = load(args)
    sync_directions(cfg, ctx.db)
    ACTIONS[args.action](ctx, args)


def _list(ctx: Ctx, args: argparse.Namespace) -> None:
    rows = list_accounts(ctx.db, "threads")
    if not rows:
        print("Аккаунтов нет. Добавить: ideaminer accounts add @имя --direction ключ;"
              " подобрать ИИ: ideaminer accounts discover --direction ключ")
    for r in rows:
        state = "вкл " if r["enabled"] else "выкл"
        last = (r["last_fetched_at"] or "—")[:16].replace("T", " ")
        note = f"  — {r['note']}" if r["note"] else ""
        print(f"  {state} @{r['username']:<28} {r['direction'] or 'общий':<22} {r['origin']:<7}"
              f" читали: {last}, раз: {r['fetches']}{note}")


def _add(ctx: Ctx, args: argparse.Namespace) -> None:
    for raw in args.names:
        ok = add_account(ctx.db, "threads", raw, args.direction, origin="manual")
        print(f"  {mark(ok)} {raw}" + ("" if ok else " — не похоже на имя аккаунта"))


def _remove(ctx: Ctx, args: argparse.Namespace) -> None:
    for raw in args.names:
        name = normalize_username(raw)
        removed = bool(name) and remove_account(ctx.db, "threads", name)
        print(f"  {'удалён' if removed else 'не найден'} @{name}")


def _check(ctx: Ctx, args: argparse.Namespace) -> None:
    for raw in args.names:
        name = normalize_username(raw) or raw
        ok, why = check_account(ctx, name, args.direction)
        print(f"  {mark(ok)} @{name}: {why}")


def _discover(ctx: Ctx, args: argparse.Namespace) -> None:
    for key in csv_list(args.direction) or [DEFAULT_DISCOVER_DIRECTION]:
        try:
            added = discover_accounts(ctx, key, args.count)
        except (BudgetExceeded, ValueError) as e:
            print(f"{key}: {e}")
            continue
        print(f"{key}: добавлено {len(added)}: " + (", ".join("@" + a for a in added) or "—"))


ACTIONS = {"list": _list, "add": _add, "remove": _remove, "check": _check, "discover": _discover}
