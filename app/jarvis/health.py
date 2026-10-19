"""Model liveness detection — which of the three llama.cpp servers is actually up.

llama.cpp exposes `GET /health` publicly (Phase 0 finding, NOTES.md §4a):
200 `{"status":"ok"}` once the model is loaded, 503 while loading, connection
refused when the service is off. Polled on a background thread; the UI just
subscribes to `updated`.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.request

from PyQt6.QtCore import QObject, pyqtSignal

from .models import MODELS

POLL_SECONDS = 3.0
TIMEOUT_SECONDS = 1.5

RUNNING = "running"
LOADING = "loading"
OFF = "off"


def check_port(port: int) -> str:
    """One-shot liveness probe: RUNNING / LOADING / OFF."""
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as resp:
            return RUNNING if resp.status == 200 else LOADING
    except urllib.error.HTTPError as exc:
        # llama.cpp answers 503 while a model is still loading.
        return LOADING if exc.code == 503 else OFF
    except Exception:
        return OFF


class HealthPoller(QObject):
    """Background poller; emits {model_key: status} every cycle on the UI thread."""

    updated = pyqtSignal(dict)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._stop = threading.Event()

    def start(self) -> None:
        thread = threading.Thread(target=self._loop, name="jarvis-health", daemon=True)
        thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            statuses = {m.key: check_port(m.port) for m in MODELS}
            self.updated.emit(statuses)
            self._stop.wait(POLL_SECONDS)
