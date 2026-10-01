"""HTTP-Session für ZEuS (HISinOne) mit Cookie-Handling, Disk-Cache und Rate-Limit."""
from __future__ import annotations

import hashlib
import logging
import random
import threading
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

BASE = "https://zeus.uni-konstanz.de/hioserver"
START_FLOW = f"{BASE}/pages/startFlow.xhtml"
USER_AGENT = "stundenplan-crawler/0.1 (+https://github.com/; polite, cached)"


class RedirectLoop(RuntimeError):
    """Spring-Webflow hat die Session verworfen (Redirect-Schleife auf _flowExecutionKey)."""


class ZeusSession:
    """Eine Session = ein Cookie-Jar = eine Spring-Webflow-Execution.

    Nicht zwischen Threads teilen: parallele Requests auf derselben JSESSIONID
    stören sich gegenseitig im Flow-State. Das Rate-Limit ist dagegen *global*
    (Klassenattribut), damit mehr Worker den Server nicht härter treffen.
    """

    _rate_lock = threading.Lock()
    _last_request = 0.0

    def __init__(self, cache_dir: Path | str | None = "cache", delay: float = 0.5,
                 timeout: int = 45, max_retries: int = 3):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.delay = delay
        self.timeout = timeout
        self.max_retries = max_retries
        self.stats = {"cache_hits": 0, "fetches": 0, "retries": 0}
        self._new_session()

    # -- intern ---------------------------------------------------------
    def _new_session(self) -> None:
        self.s = requests.Session()
        self.s.headers["User-Agent"] = USER_AGENT
        self.s.headers["Accept-Language"] = "de-DE,de;q=0.9"
        self.s.max_redirects = 10

    def _throttle(self) -> None:
        with ZeusSession._rate_lock:
            wait = self.delay - (time.monotonic() - ZeusSession._last_request)
            if wait > 0:
                time.sleep(wait + random.uniform(0, 0.05))
            ZeusSession._last_request = time.monotonic()

    def _cache_path(self, url: str) -> Path | None:
        if not self.cache_dir:
            return None
        h = hashlib.sha256(url.encode()).hexdigest()[:32]
        return self.cache_dir / h[:2] / f"{h}.html"

    # -- API ------------------------------------------------------------
    def post(self, url: str, data: dict, *, referer: str | None = None):
        """POST mit denselben Wiederholungen wie `get` (nichts wird gecacht).

        Ein einzelner Lesetimeout darf einen stundenlangen Lauf nicht abbrechen.
        """
        headers = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
        if referer:
            headers["Referer"] = referer
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                r = self.s.post(url, data=data, timeout=self.timeout, headers=headers)
                r.raise_for_status()
                return r
            except requests.RequestException as e:
                last_exc = e
                log.warning("POST-Fehler (%s/%s) bei %s: %s",
                            attempt + 1, self.max_retries, url, e)
                self.stats["retries"] += 1
                time.sleep(1.5 * (attempt + 1))
        raise last_exc  # type: ignore[misc]

    def get_response(self, url: str):
        """GET ohne Cache, gibt die Antwort zurück (für Formular-Flows)."""
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                r = self.s.get(url, timeout=self.timeout)
                r.raise_for_status()
                return r
            except requests.TooManyRedirects:
                last_exc = RedirectLoop(url)
                self._new_session()
            except requests.RequestException as e:
                last_exc = e
            self.stats["retries"] += 1
            time.sleep(1.5 * (attempt + 1))
        raise last_exc  # type: ignore[misc]

    def get(self, url: str, *, params: dict | None = None, use_cache: bool = True) -> str:
        if params:
            url = requests.Request("GET", url, params=params).prepare().url
        cp = self._cache_path(url)
        if use_cache and cp and cp.exists():
            self.stats["cache_hits"] += 1
            return cp.read_text(encoding="utf-8")

        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                r = self.s.get(url, timeout=self.timeout)
                r.raise_for_status()
                r.encoding = r.encoding or "utf-8"
                text = r.text
                if cp:
                    cp.parent.mkdir(parents=True, exist_ok=True)
                    cp.write_text(text, encoding="utf-8")
                self.stats["fetches"] += 1
                return text
            except requests.TooManyRedirects as e:
                # Typisches Symptom einer abgelaufenen/fehlenden JSESSIONID.
                last_exc = RedirectLoop(url)
                log.warning("Redirect-Schleife, Session wird erneuert: %s", url)
                self._new_session()
            except (requests.RequestException,) as e:
                last_exc = e
                log.warning("Fehler (%s/%s) bei %s: %s", attempt + 1, self.max_retries, url, e)
            self.stats["retries"] += 1
            time.sleep(1.5 * (attempt + 1))
        raise last_exc  # type: ignore[misc]
