"""Стадии конвейера и общие помощники для параллельных ИИ-вызовов."""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import TYPE_CHECKING, Callable, Iterable, Iterator, TypeVar

from ..llm import BudgetExceeded

if TYPE_CHECKING:
    from ..context import Ctx

T = TypeVar("T")
R = TypeVar("R")


def parallel(ctx: "Ctx", stage: str, items: Iterable[T],
             fn: Callable[[T], R]) -> Iterator[tuple[T, R | None, Exception | None]]:
    """Гоняет fn по items в пуле потоков, не больше llm.concurrency одновременно.
    После BudgetExceeded новые задачи не запускаются: начатые догорают и отдаются,
    а затем исключение пробрасывается — конвейер должен знать, что бюджет кончился."""
    it = iter(items)
    stop = False
    budget_error: BudgetExceeded | None = None
    with ThreadPoolExecutor(max_workers=max(1, ctx.cfg.llm.concurrency)) as ex:
        inflight: dict[Future, T] = {}

        def submit_next() -> None:
            if stop:
                return
            try:
                item = next(it)
            except StopIteration:
                return
            inflight[ex.submit(fn, item)] = item

        for _ in range(max(1, ctx.cfg.llm.concurrency)):
            submit_next()
        while inflight:
            done, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
            for f in done:
                item = inflight.pop(f)
                try:
                    yield item, f.result(), None
                except BudgetExceeded as e:
                    if not stop:
                        ctx.log.warning("[%s] %s — стадия остановлена", stage, e)
                        ctx.stat(stage, "budget_stop")
                    stop = True
                    budget_error = e
                    yield item, None, e
                except Exception as e:  # noqa: BLE001 — ошибку отдаём вызывающему
                    ctx.log.warning("[%s] ошибка: %s", stage, e)
                    ctx.stat(stage, "errors")
                    yield item, None, e
                submit_next()
    if budget_error:
        raise budget_error
