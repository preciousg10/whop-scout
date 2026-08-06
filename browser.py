"""Persistent, headful Chromium session.

The point of this tool is that it is *my* logged-in browser profile, driven
slowly. So: always headful, one tab, persistent user_data_dir, no stealth
plugins, no proxies. The only fingerprint touch-up is hiding
navigator.webdriver, which some sites read even when you're just automating your
own account. Locale and timezone are left to the system; the bundled Chromium UA
is kept as-is.
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

_WEBDRIVER_INIT = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
)


class Session:
    def __init__(self, *, profile_dir, viewport, headful=True):
        self.profile_dir = str(Path(profile_dir).resolve())
        self.viewport = viewport
        self.headful = headful
        self._pw = None
        self.context = None
        self.page = None

    def __enter__(self):
        self._pw = sync_playwright().start()
        # Persistent context => the login session survives between runs.
        self.context = self._pw.chromium.launch_persistent_context(
            user_data_dir=self.profile_dir,
            headless=not self.headful,
            viewport={"width": self.viewport[0], "height": self.viewport[1]},
        )
        self.context.add_init_script(_WEBDRIVER_INIT)
        self.page = (
            self.context.pages[0] if self.context.pages else self.context.new_page()
        )
        return self

    def __exit__(self, *_exc):
        try:
            if self.context:
                self.context.close()
        finally:
            if self._pw:
                self._pw.stop()
        return False
