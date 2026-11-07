"""Честный headless-рендер страниц: для площадок, которые отдают содержимое только
после выполнения JavaScript.

Никакой маскировки: стандартный User-Agent HeadlessChrome, без входа в аккаунты,
без cookies (чистый временный профиль на каждую страницу), без прокси. Если сайт
требует вход или блокирует headless-браузер — вызывающий код должен остановиться,
а не искать обход.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

CANDIDATES = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome-headless-shell")
PLAYWRIGHT_GLOBS = (
    "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
    "~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome",
)


class BrowserError(RuntimeError):
    pass


def find_browser(explicit: str | None = None) -> str | None:
    """Путь к Chromium: явный из конфига, $IDEAMINER_CHROMIUM, PATH или кэш Playwright."""
    for path in (explicit, os.environ.get("IDEAMINER_CHROMIUM")):
        if path and Path(path).exists():
            return path
    for name in CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    for pattern in PLAYWRIGHT_GLOBS:
        matches = sorted(glob.glob(os.path.expanduser(pattern)), reverse=True)
        if matches:
            return matches[0]
    return None


class HeadlessBrowser:
    def __init__(self, path: str | None = None, budget_ms: int = 12000, timeout_s: float = 60,
                 no_sandbox: bool = False):
        self.path = find_browser(path)
        self.budget_ms = budget_ms
        self.timeout_s = timeout_s
        self.no_sandbox = no_sandbox or os.environ.get("IDEAMINER_CHROMIUM_NO_SANDBOX") == "1"

    @property
    def available(self) -> bool:
        return bool(self.path)

    def render(self, url: str) -> str:
        """HTML страницы после выполнения скриптов (виртуальное время budget_ms)."""
        if not self.path:
            raise BrowserError("Chromium не найден: установите chromium или задайте путь в конфиге")
        with tempfile.TemporaryDirectory(prefix="ideaminer-chrome-") as profile:
            args = [
                self.path, "--headless", "--disable-gpu", "--disable-extensions", "--mute-audio",
                "--disable-background-networking", "--no-first-run", "--no-default-browser-check",
                "--blink-settings=imagesEnabled=false", f"--user-data-dir={profile}",
                # Зависшую страницу (виртуальное время стоит при висящих запросах) прерывает
                # таймаут процесса ниже. Флаг --timeout не годится: с ним DOM приходит пустым
                f"--virtual-time-budget={self.budget_ms}", "--dump-dom", url,
            ]
            if self.no_sandbox:
                args.insert(1, "--no-sandbox")
            try:
                done = subprocess.run(args, capture_output=True, timeout=self.timeout_s, check=False)
            except subprocess.TimeoutExpired as e:
                raise BrowserError(f"рендер {url} не уложился в {self.timeout_s:.0f} с") from e
        html = done.stdout.decode("utf-8", errors="replace")
        if not html.strip():
            err = done.stderr.decode("utf-8", errors="replace")
            hint = " (нет песочницы: включите no_sandbox или запускайте в Docker)" if "sandbox" in err.lower() else ""
            raise BrowserError(f"браузер вернул пустую страницу, код {done.returncode}{hint}")
        return html
