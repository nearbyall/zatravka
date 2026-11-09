"""Honest headless rendering for sites that build their content with JavaScript.

No disguise: the stock HeadlessChrome User-Agent, no logins, no cookies (a fresh
temporary profile per page), no proxies. When a site asks for a login or blocks
headless browsers, callers must stop rather than look for a way around.
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
    """Chromium from the config, $ZATRAVKA_CHROMIUM, PATH or the Playwright cache."""
    for path in (explicit, os.environ.get("ZATRAVKA_CHROMIUM")):
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
        self.no_sandbox = no_sandbox or os.environ.get("ZATRAVKA_CHROMIUM_NO_SANDBOX") == "1"

    @property
    def available(self) -> bool:
        return bool(self.path)

    def render(self, url: str) -> str:
        """The page DOM after scripts ran for budget_ms of virtual time."""
        if not self.path:
            raise BrowserError("Chromium не найден: установите chromium или задайте путь в конфиге")
        with tempfile.TemporaryDirectory(prefix="zatravka-chrome-") as profile:
            try:
                done = subprocess.run(self._args(url, profile), capture_output=True, timeout=self.timeout_s,
                                      check=False)
            except subprocess.TimeoutExpired as e:
                raise BrowserError(f"рендер {url} не уложился в {self.timeout_s:.0f} с") from e
        html = done.stdout.decode("utf-8", errors="replace")
        if not html.strip():
            err = done.stderr.decode("utf-8", errors="replace")
            hint = " (нет песочницы: включите no_sandbox или запускайте в Docker)" if "sandbox" in err.lower() else ""
            raise BrowserError(f"браузер вернул пустую страницу, код {done.returncode}{hint}")
        return html

    def _args(self, url: str, profile: str) -> list[str]:
        # A hung page (virtual time stalls on pending requests) is cut off by the process
        # timeout: Chromium's own --timeout flag makes it dump an empty DOM.
        args = [
            self.path or "", "--headless", "--disable-gpu", "--disable-extensions", "--mute-audio",
            "--disable-background-networking", "--no-first-run", "--no-default-browser-check",
            "--blink-settings=imagesEnabled=false", f"--user-data-dir={profile}",
            f"--virtual-time-budget={self.budget_ms}", "--dump-dom", url,
        ]
        if self.no_sandbox:
            args.insert(1, "--no-sandbox")
        return args
