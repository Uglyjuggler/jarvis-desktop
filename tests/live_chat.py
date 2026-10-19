#!/usr/bin/env python3
"""Live streaming check — sends one prompt to whichever local model is running,
through the app's own LlmWorker (the same code path the chat UI uses).

    QT_QPA_PLATFORM=offscreen python3 tests/live_chat.py

Exit codes: 0 = streamed fine, 2 = no model live, 3 = request failed.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

from PyQt6.QtCore import QCoreApplication, QTimer  # noqa: E402

from jarvis import models  # noqa: E402
from jarvis.health import check_port  # noqa: E402
from jarvis.llm import LlmWorker  # noqa: E402
from jarvis.prompt import build_system_prompt  # noqa: E402

PROMPT = "In one short spoken sentence, say hello and confirm you are working."


def main() -> int:
    app = QCoreApplication([])

    live = next(
        (m for m in models.MODELS if check_port(m.port) == "running"),
        None,
    )
    if live is None:
        print("NO MODEL LIVE — start one in the Model Switcher first.")
        return 2

    print(f"Streaming from {live.name} (port {live.port})…\n")
    worker = LlmWorker()
    result = {"ok": False}

    def on_delta(piece: str) -> None:
        print(piece, end="", flush=True)

    def on_finished(full: str) -> None:
        print(f"\n\n--- done: {len(full)} chars streamed ---")
        result["ok"] = True
        app.quit()

    def on_failed(error: str) -> None:
        print(f"\nFAILED: {error}")
        app.quit()

    worker.delta.connect(on_delta)
    worker.finished.connect(on_finished)
    worker.failed.connect(on_failed)

    messages = [
        {"role": "system", "content": build_system_prompt(datetime.now())},
        {"role": "user", "content": PROMPT},
    ]
    if not worker.start(live.port, messages):
        print("worker busy — unexpected")
        return 3

    QTimer.singleShot(180_000, app.quit)
    app.exec()
    return 0 if result["ok"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
