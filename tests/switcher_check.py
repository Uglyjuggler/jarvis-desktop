#!/usr/bin/env python3
"""Real service switching check — Phase 2 acceptance, run by hand.

Exercises the app's actual switcher mechanism (``jarvis.switcher``) against the
real systemd services, using the same ``sudo -n systemctl`` calls as the Models
panel and Brandon's Model Switcher:

  1. show the current service states (Qwen3-8B is normally the running one)
  2. exclusively start Qwen2.5-Coder-14B — the other model is stopped first
  3. wait until its /health says the model is loaded
  4. stop it again
  5. restore Qwen3-8B and wait until it serves /health again

It asks for confirmation unless run with ``--yes`` (it stops/starts real
services). Exit codes: 0 all good, 1 a step failed, 2 declined.
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

from PyQt6.QtCore import QCoreApplication, QTimer  # noqa: E402

from jarvis.health import check_port  # noqa: E402
from jarvis.models import BY_KEY, MODELS, Model  # noqa: E402
from jarvis.switcher import Switcher, unit_state  # noqa: E402

HEALTH_TIMEOUT_S = 300


def show_states(label: str) -> None:
    states = ", ".join(f"{m.name}: {unit_state(m.unit)}" for m in MODELS)
    print(f"  {label}: {states}")


def run_op(app: QCoreApplication, switcher: Switcher, model: Model, action: str) -> bool:
    """Fire one start/stop through the real Switcher and wait for its verdict."""
    result = {"ok": False}

    def on_finished(ok: bool, message: str) -> None:
        print(f"  -> {'OK' if ok else 'FAILED'}: {message}")
        result["ok"] = ok
        app.quit()

    switcher.finished.connect(on_finished)
    started = (
        switcher.start_model(model) if action == "start" else switcher.stop_model(model)
    )
    if not started:
        print("  -> FAILED: switcher is busy")
        switcher.finished.disconnect(on_finished)
        return False
    QTimer.singleShot(120_000, app.quit)
    app.exec()
    switcher.finished.disconnect(on_finished)
    return result["ok"]


def wait_health(model: Model, timeout_s: int = HEALTH_TIMEOUT_S) -> bool:
    began = time.time()
    while time.time() - began < timeout_s:
        if check_port(model.port) == "running":
            print(f"  -> {model.name} serves /health after {time.time() - began:.0f}s")
            return True
        time.sleep(2)
    print(f"  -> {model.name} did NOT answer /health within {timeout_s}s")
    return False


def main() -> int:
    if "--yes" not in sys.argv:
        answer = input("This stops/starts real systemd services. Type 'yes': ")
        if answer.strip() != "yes":
            print("declined")
            return 2

    app = QCoreApplication([])
    switcher = Switcher()
    switcher.progress.connect(lambda msg: print(f"     {msg}"))

    small = BY_KEY["buster-14b"]
    ai = BY_KEY["buster-ai"]
    ok = True

    print("1. initial")
    show_states("states")

    print("2. exclusive start of Qwen2.5-Coder-14B (stops the other model first)")
    if run_op(app, switcher, small, "start"):
        show_states("states")
        ok = wait_health(small) and ok
    else:
        ok = False

    print("3. stop Qwen2.5-Coder-14B")
    ok = run_op(app, switcher, small, "stop") and ok
    show_states("states")

    print("4. restore Qwen3-8B (as it was found)")
    if unit_state(ai.unit) not in ("active", "activating"):
        ok = run_op(app, switcher, ai, "start") and ok
        ok = wait_health(ai) and ok
    else:
        print("  -> already running")
    show_states("final states")

    print("switcher check:", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
