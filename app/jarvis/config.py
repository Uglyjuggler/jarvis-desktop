"""Persisted app configuration — assistant identity, voice, speech input, wake
word, read-only tools, theme.

Lives at `jarvis-desktop/appdata/config.json` (inside the project folder per the
job's containment rule; git-ignored; auto-created on first save). Everything a
user can retune without touching code lives here: the assistant's name and
greeting, the Kokoro voice (**by name, never by number**), speed, audio output,
the speech-input model/device, the wake word (off by default), the read-only
tools (weather place + unit, on by default), and the theme accents.

The Settings dialog edits the voice/input/output/wake/tools parts as you change
them; the rest is hand-editable. `load()` heals missing/partial/corrupt files
with defaults, so a bad edit degrades to stock rather than crashing.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import stt
from . import tts
from . import wake
from .state import PROJECT_ROOT

CONFIG_PATH = PROJECT_ROOT / "appdata" / "config.json"

DEFAULTS: dict = {
    "version": 1,
    "assistant": {
        "name": "Jarvis",
        "greeting": "How can I help?",
    },
    "voice": {
        "enabled": True,
        "voice": "bm_george",            # by name; see tts.py for resolution
        "speed": 1.0,                    # 0.5 – 2.0
        "sink": None,                    # None = auto-pick the system default sink
        "model_dir": "",                 # "" = the project's own model (models/)
        "preview_text": "Hello! This is how I sound. I'm Jarvis — ready to help whenever you are.",
    },
    "stt": {
        "model_dir": "",                 # "" = the project's own model (models/sense-voice)
        "source": None,                  # None = Automatic (the system default source)
        "language": "en",                # "" = let SenseVoice auto-detect
    },
    "wake": {
        "enabled": False,                # off by default: the mic is only listened to when asked
        "threshold": 0.5,                # 0.30 – 0.90 — "hey jarvis" trigger level
        "model_dir": "",                 # "" = the project's own models/openwakeword
        "silence_seconds": 1.0,          # 0.50 – 3.00 — quiet tail that ends a wake take
        "speech_margin_db": 8.0,         # 4 – 16 — dB above the room's noise floor = speech
    },
    "tools": {
        "enabled": True,                 # read-only lookups (weather, date/time); on by default
        "place": "Frankfort, Kentucky",  # used when a weather question names no place
        "unit": "fahrenheit",            # fahrenheit | celsius
    },
    "theme": {
        "accent": "#FF8A00",             # the Portal orange, baked in as default
        "accent_light": "#FFB74D",
        "accent_deep": "#FF6D00",
        "background": "#0A0A0C",
    },
}


# ---- sanitizers (wrong shapes heal to the default, never crash) -------------

def _text(value: object, fallback: str) -> str:
    return value if isinstance(value, str) and value.strip() else fallback


def _flag(value: object, fallback: bool) -> bool:
    return value if isinstance(value, bool) else fallback


def _speed(value: object, fallback: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0.5, min(2.0, float(value)))
    return fallback


def _sink(value: object, fallback: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _number(value: object, fallback: float, low: float, high: float) -> float:
    """A numeric setting, clamped to [low, high]; wrong shapes heal to the default."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(low, min(high, float(value)))
    return fallback


def _choice(value: object, options: tuple[str, ...], fallback: str) -> str:
    """A string setting constrained to a known set (case-folded); anything else
    heals to the default."""
    if isinstance(value, str) and value.strip().lower() in options:
        return value.strip().lower()
    return fallback


def _section(value: object, fallback: dict) -> dict:
    return value if isinstance(value, dict) else fallback


def load() -> dict:
    """Load config, healed with defaults; a missing/corrupt file yields defaults."""
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = None
    if not isinstance(data, dict):
        data = {}

    default = DEFAULTS
    assistant = _section(data.get("assistant"), {})
    voice = _section(data.get("voice"), {})
    stt_cfg = _section(data.get("stt"), {})
    wake_cfg = _section(data.get("wake"), {})
    tools_cfg = _section(data.get("tools"), {})
    theme_cfg = _section(data.get("theme"), {})
    d_voice = default["voice"]
    d_stt = default["stt"]
    d_wake = default["wake"]
    d_tools = default["tools"]
    d_theme = default["theme"]

    return {
        "version": 1,
        "assistant": {
            "name": _text(assistant.get("name"), default["assistant"]["name"]),
            "greeting": _text(assistant.get("greeting"), default["assistant"]["greeting"]),
        },
        "voice": {
            "enabled": _flag(voice.get("enabled"), d_voice["enabled"]),
            "voice": _text(voice.get("voice"), d_voice["voice"]),
            "speed": _speed(voice.get("speed"), d_voice["speed"]),
            "sink": _sink(voice.get("sink"), d_voice["sink"]),
            "model_dir": voice.get("model_dir") if isinstance(voice.get("model_dir"), str) else "",
            "preview_text": _text(voice.get("preview_text"), d_voice["preview_text"]),
        },
        "stt": {
            "model_dir": stt_cfg.get("model_dir") if isinstance(stt_cfg.get("model_dir"), str) else "",
            "source": _sink(stt_cfg.get("source"), d_stt["source"]),
            "language": stt_cfg.get("language") if isinstance(stt_cfg.get("language"), str) else d_stt["language"],
        },
        "wake": {
            "enabled": _flag(wake_cfg.get("enabled"), d_wake["enabled"]),
            "threshold": _number(wake_cfg.get("threshold"), d_wake["threshold"], 0.3, 0.9),
            "model_dir": wake_cfg.get("model_dir") if isinstance(wake_cfg.get("model_dir"), str) else "",
            "silence_seconds": _number(
                wake_cfg.get("silence_seconds"), d_wake["silence_seconds"], 0.5, 3.0
            ),
            "speech_margin_db": _number(
                wake_cfg.get("speech_margin_db"), d_wake["speech_margin_db"], 4.0, 16.0
            ),
        },
        "tools": {
            "enabled": _flag(tools_cfg.get("enabled"), d_tools["enabled"]),
            "place": _text(tools_cfg.get("place"), d_tools["place"]),
            "unit": _choice(tools_cfg.get("unit"), ("fahrenheit", "celsius"), d_tools["unit"]),
        },
        "theme": {
            "accent": _text(theme_cfg.get("accent"), d_theme["accent"]),
            "accent_light": _text(theme_cfg.get("accent_light"), d_theme["accent_light"]),
            "accent_deep": _text(theme_cfg.get("accent_deep"), d_theme["accent_deep"]),
            "background": _text(theme_cfg.get("background"), d_theme["background"]),
        },
    }


def save(cfg: dict) -> None:
    """Write atomically; never raises."""
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CONFIG_PATH)
    except Exception:
        pass


def model_dir(cfg: dict) -> Path:
    """The Kokoro folder to load: the configured one, else the project's own."""
    raw = str((cfg.get("voice") or {}).get("model_dir") or "").strip()
    return Path(raw).expanduser() if raw else tts.DEFAULT_MODEL_DIR


def stt_model_dir(cfg: dict) -> Path:
    """The speech-to-text folder to load: the configured one, else the project's own."""
    raw = str((cfg.get("stt") or {}).get("model_dir") or "").strip()
    return Path(raw).expanduser() if raw else stt.DEFAULT_MODEL_DIR


def wake_model_dir(cfg: dict) -> Path:
    """The wake-word folder to load: the configured one, else the project's own."""
    raw = str((cfg.get("wake") or {}).get("model_dir") or "").strip()
    return Path(raw).expanduser() if raw else wake.DEFAULT_MODEL_DIR
