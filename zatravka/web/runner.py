from __future__ import annotations

import collections
import logging
import threading
import time
from pathlib import Path
from typing import Any

from ..config import load_config
from ..pipeline import make_ctx, run_once

LOG_TAIL = 60


class Runner:
    """Starts a pipeline run from the UI in a background thread and keeps the log tail."""

    def __init__(self, cfg_path: Path | None):
        self.cfg_path = cfg_path
        self.thread: threading.Thread | None = None
        self.lines: collections.deque[str] = collections.deque(maxlen=LOG_TAIL)
        self.result: dict[str, Any] | None = None
        self.started: float | None = None
        handler = logging.Handler(logging.INFO)
        handler.emit = self._emit  # type: ignore[method-assign]
        pipeline_log = logging.getLogger("zatravka")
        pipeline_log.addHandler(handler)
        pipeline_log.setLevel(logging.INFO)

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self, collect: bool) -> bool:
        if self.running:
            return False
        self.lines.clear()
        self.result = None
        self.started = time.time()
        self.thread = threading.Thread(target=self._work, args=(collect,), daemon=True, name="zatravka-run")
        self.thread.start()
        return True

    def state(self) -> dict[str, Any]:
        return {"running": self.running, "lines": list(self.lines), "result": self.result,
                "elapsed": round(time.time() - self.started) if self.started else None}

    def _work(self, collect: bool) -> None:
        ctx = make_ctx(load_config(self.cfg_path))
        try:
            self.result = run_once(ctx, trigger="web", collect=collect)
        except Exception as e:  # noqa: BLE001 - the UI shows the error instead of a dead thread
            self.result = {"status": "error", "error": str(e)}
        finally:
            ctx.close()

    def _emit(self, record: logging.LogRecord) -> None:
        if self.running:
            self.lines.append(f"{time.strftime('%H:%M:%S')} {record.getMessage()}")
