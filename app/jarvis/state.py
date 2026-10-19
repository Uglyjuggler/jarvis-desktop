"""Persisted app state — selected model, chat transcript, window size.

Lives at `jarvis-desktop/appdata/state.json` (inside the project folder per the
job's containment rule; git-ignored). Writes are atomic and best-effort: state is
a nicety, it never takes the app down.
"""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = PROJECT_ROOT / "appdata" / "state.json"

MAX_PERSISTED_MESSAGES = 200

DEFAULT: dict = {
    "version": 1,
    "model": "buster-ai",
    "messages": [],
    "window": [1080, 760],
}


def load() -> dict:
    """Load state, sanitized; a missing/corrupt file yields defaults."""
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(DEFAULT)
    except Exception:
        return dict(DEFAULT)

    if not isinstance(data, dict):
        return dict(DEFAULT)

    merged = dict(DEFAULT)
    merged.update(data)

    if not isinstance(merged.get("messages"), list):
        merged["messages"] = []
    merged["messages"] = [
        m
        for m in merged["messages"]
        if isinstance(m, dict)
        and m.get("role") in ("user", "assistant")
        and isinstance(m.get("content"), str)
    ][-MAX_PERSISTED_MESSAGES:]

    win = merged.get("window")
    if (
        not isinstance(win, (list, tuple))
        or len(win) != 2
        or not all(isinstance(v, (int, float)) and v > 0 for v in win)
    ):
        merged["window"] = list(DEFAULT["window"])

    if not isinstance(merged.get("model"), str):
        merged["model"] = DEFAULT["model"]

    return merged


def save(model: str, messages: list[dict], window: tuple[int, int]) -> None:
    """Write state atomically; never raises."""
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "model": model,
            "messages": messages[-MAX_PERSISTED_MESSAGES:],
            "window": [int(window[0]), int(window[1])],
        }
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_PATH)
    except Exception:
        pass
