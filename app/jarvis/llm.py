"""Streaming chat client for the local llama.cpp servers.

Same wire the Model Switcher's model-chat.py already uses on this machine:
`POST /v1/chat/completions` with `"stream": true` — SSE `data:` chunks, each
carrying `choices[0].delta.content`, terminated by `data: [DONE]` (Phase 0
verified against the installed llama.cpp source, NOTES.md §4a).

Runs the blocking read on a daemon thread; Qt queues the signals onto the UI
thread, so the window never blocks on the network.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

from PyQt6.QtCore import QObject, pyqtSignal

REQUEST_TIMEOUT_SECONDS = 900  # long enough for slow first tokens on a cold model
MAX_ERROR_CHARS = 300


class LlmWorker(QObject):
    """One request at a time; call `start()` again only after finished/failed."""

    delta = pyqtSignal(str)     # one streamed piece of the reply
    finished = pyqtSignal(str)  # the full reply text
    failed = pyqtSignal(str)    # human-readable error

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    def start(
        self,
        port: int,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int | None = 512,
    ) -> bool:
        """Begin a streaming request; returns False if one is already running."""
        if self._busy:
            return False
        self._busy = True
        thread = threading.Thread(
            target=self._run,
            args=(port, messages, temperature, max_tokens),
            name="jarvis-llm",
            daemon=True,
        )
        thread.start()
        return True

    def _run(
        self,
        port: int,
        messages: list[dict],
        temperature: float,
        max_tokens: int | None,
    ) -> None:
        body: dict = {"messages": messages, "stream": True, "temperature": temperature}
        if max_tokens:
            # Spoken answers are short; a cap keeps replies tight and fast.
            body["max_tokens"] = int(max_tokens)
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        collected: list[str] = []
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except ValueError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    piece = (choices[0].get("delta") or {}).get("content") or ""
                    if piece:
                        collected.append(piece)
                        self.delta.emit(piece)
            self.finished.emit("".join(collected))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="ignore")[:MAX_ERROR_CHARS].strip()
            except Exception:
                pass
            message = f"The model server answered HTTP {exc.code}."
            if detail:
                message += f" {detail}"
            self.failed.emit(message)
        except Exception as exc:  # URLError, timeouts, socket drops…
            self.failed.emit(
                f"Couldn't reach the model ({exc.__class__.__name__}: {exc}). "
                "It may have been stopped — check the Model Switcher."
            )
        finally:
            self._busy = False
