"""Pipeline stages and a helper that runs LLM calls in parallel."""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import TYPE_CHECKING, Callable, Iterable, Iterator, TypeVar

from ..llm import BudgetExceeded

if TYPE_CHECKING:
    from ..context import Ctx

T = TypeVar("T")
R = TypeVar("R")
_DONE = object()


def parallel(ctx: "Ctx", stage: str, items: Iterable[T],
             fn: Callable[[T], R]) -> Iterator[tuple[T, R | None, Exception | None]]:
    """Runs fn over items with at most llm.concurrency calls in flight, yielding
    (item, result, error). After BudgetExceeded no new calls start: those in flight
    finish and are yielded, then the exception is re-raised so the pipeline stops."""
    pending = iter(items)
    workers = max(1, ctx.cfg.llm.concurrency)
    budget_error: BudgetExceeded | None = None
    with ThreadPoolExecutor(max_workers=workers) as pool:
        inflight: dict[Future, T] = {}

        def submit_next() -> None:
            if budget_error:
                return
            item = next(pending, _DONE)
            if item is not _DONE:
                inflight[pool.submit(fn, item)] = item

        for _ in range(workers):
            submit_next()
        while inflight:
            done, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
            for future in done:
                item = inflight.pop(future)
                try:
                    yield item, future.result(), None
                except BudgetExceeded as e:
                    if not budget_error:
                        ctx.log.warning("[%s] %s — стадия остановлена", stage, e)
                        ctx.stat(stage, "budget_stop")
                    budget_error = e
                    yield item, None, e
                except Exception as e:  # noqa: BLE001 - the caller decides what a failed item means
                    ctx.log.warning("[%s] ошибка: %s", stage, e)
                    ctx.stat(stage, "errors")
                    yield item, None, e
                submit_next()
    if budget_error:
        raise budget_error

