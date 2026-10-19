"""Model service control — the exact Model Switcher mechanism.

Mirrors /mnt/PROJECTS/model-switcher/model-switcher.py (read 2026-10-18):

  * every systemctl call is ``sudo -n systemctl …`` (passwordless), 30 s
    timeout, stdout only; failures collapse to "" → state "unknown"
  * ``unit_state()`` = first line of ``is-active``
  * exclusive start = stop every other *active/activating* unit →
    ``reset-failed`` on the target → ``start`` the target
  * stop = ``stop`` → ``reset-failed``
  * **nothing starts on its own** — the only caller of ``start_model`` is the
    user's click in the Models panel (units are disabled + ``Restart=no``
    anyway, so even a crash can't re-arm them)

This is the one place in the app allowed to shell out to sudo, and it uses the
same exact calls the Model Switcher makes.
"""

from __future__ import annotations

import subprocess
import threading

from PyQt6.QtCore import QObject, pyqtSignal

from .models import MODELS

POLL_SECONDS = 2.0      # the Model Switcher's refresh cadence
SYSTEMCTL_TIMEOUT = 30  # the Model Switcher's subprocess timeout

_UNITS = [m.unit for m in MODELS]

# Every unit the Model Switcher manages. sd-image (FLUX image generator) has no
# row in the panel — it is not a chat model — but an exclusive start must free
# the graphics card from it too, exactly like the Model Switcher (which stops
# every other unit, not just the chat ones).
EXCLUSIVE_UNITS = _UNITS + ["sd-image"]

_UNIT_NAMES = {m.unit: m.name for m in MODELS}
_UNIT_NAMES["sd-image"] = "FLUX 3 Klein"


def systemctl(*args: str) -> str:
    """Run a systemctl command through passwordless sudo. Returns stdout text."""
    try:
        out = subprocess.run(
            ["sudo", "-n", "systemctl", *args],
            capture_output=True,
            text=True,
            timeout=SYSTEMCTL_TIMEOUT,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def unit_state(unit: str) -> str:
    """One of: active, activating, inactive, failed, unknown."""
    out = systemctl("is-active", unit)
    return out.splitlines()[0] if out else "unknown"


def stop_unit(unit: str) -> None:
    """The Model Switcher's stop: stop, then clear any failed state."""
    systemctl("stop", unit)
    systemctl("reset-failed", unit)


def start_exclusive(unit: str, progress=None) -> None:
    """The Model Switcher's start order, exactly: stop the others, reset-failed
    the target, start it. ``progress(text)`` gets friendly step updates."""
    for other in EXCLUSIVE_UNITS:
        if other != unit and unit_state(other) in ("active", "activating"):
            if progress:
                progress(f"Stopping {_UNIT_NAMES.get(other, other)}…")
            systemctl("stop", other)
    if progress:
        progress(f"Starting {_UNIT_NAMES.get(unit, unit)}…")
    systemctl("reset-failed", unit)
    systemctl("start", unit)


class UnitPoller(QObject):
    """Polls the three units with ``is-active`` every 2 s (panel open only)."""

    updated = pyqtSignal(dict)  # {unit: state}

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="jarvis-units", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.updated.emit({unit: unit_state(unit) for unit in _UNITS})
            self._stop.wait(POLL_SECONDS)


class Switcher(QObject):
    """Runs start/stop operations on a worker thread so the window stays live."""

    progress = pyqtSignal(str)          # friendly step text while working
    finished = pyqtSignal(bool, str)    # ok, human message
    busy_changed = pyqtSignal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    def start_model(self, model) -> bool:
        """Exclusive start; returns False if another operation is running."""
        if self._busy:
            return False
        self._busy = True
        self.busy_changed.emit(True)
        threading.Thread(
            target=self._do_start, args=(model,), name="jarvis-start", daemon=True
        ).start()
        return True

    def stop_model(self, model) -> bool:
        if self._busy:
            return False
        self._busy = True
        self.busy_changed.emit(True)
        threading.Thread(
            target=self._do_stop, args=(model,), name="jarvis-stop", daemon=True
        ).start()
        return True

    # -- worker bodies ------------------------------------------------------

    def _do_start(self, model) -> None:
        ok, message = True, ""
        try:
            start_exclusive(model.unit, progress=self.progress.emit)
            state = unit_state(model.unit)
            if state in ("active", "activating"):
                message = (
                    f"{model.name} is starting — it joins the chat once it has "
                    "finished loading."
                )
            else:
                ok = False
                message = f"{model.name} did not start (systemd says: {state})."
        except Exception as exc:  # defensive; systemctl() already swallows
            ok, message = False, f"Start failed: {exc}"
        self._finish(ok, message)

    def _do_stop(self, model) -> None:
        ok, message = True, ""
        try:
            self.progress.emit(f"Stopping {model.name}…")
            stop_unit(model.unit)
            state = unit_state(model.unit)
            if state not in ("active", "activating"):
                message = f"{model.name} stopped — the graphics card is free."
            else:
                ok = False
                message = f"{model.name} is still {state}."
        except Exception as exc:
            ok, message = False, f"Stop failed: {exc}"
        self._finish(ok, message)

    def _finish(self, ok: bool, message: str) -> None:
        self._busy = False
        self.busy_changed.emit(False)
        self.finished.emit(ok, message)
