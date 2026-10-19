"""Audio plumbing: PulseAudio/PipeWire device discovery and WAV playback.

Jarvis plays through `paplay --device=<sink>` so it is pinned to the chosen
output *without* touching the system default sink (other apps keep their
routing).

Auto-pick order (corrected 2026-10-19 — Brandon hears this PC through
Moonlight, i.e. via Sunshine's virtual sink, so the old "never the Sunshine
sinks" rule sent Jarvis's voice to a path he couldn't hear):

  1. an explicitly configured sink (handled by the caller — a config value
     always wins),
  2. **the system default sink** (`pactl get-default-sink`), whatever kind it
     is — that's where the user actually hears things,
  3. only with no usable default: the first physical sink that says "analog",
     else the first RUNNING physical sink, else the first physical sink.

Virtual sinks (`support.null-audio-sink`: Sunshine's three, the Portal's
`pc_stream`) are legitimate destinations — on this machine the default *is*
one — and are always listed in Settings.
"""

from __future__ import annotations

import re
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

PLAYER = "paplay"
PACTL = "pactl"
_CMD_TIMEOUT = 10


@dataclass(frozen=True)
class Sink:
    """One output device, as pactl describes it."""

    name: str
    description: str
    state: str       # RUNNING / IDLE / SUSPENDED / UNKNOWN
    virtual: bool    # a null sink (Sunshine, Portal's pc_stream, …) — still selectable


def _run(argv: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=_CMD_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None


_FIELD_NAME = re.compile(r"^\s*Name:\s*(\S+)\s*$", re.MULTILINE)
_FIELD_DESC = re.compile(r"^\s*Description:\s*(.*)$", re.MULTILINE)
_FIELD_STATE = re.compile(r"^\s*State:\s*(\S+)\s*$", re.MULTILINE)
_FIELD_FACTORY = re.compile(r'^\s*factory\.name\s*=\s*"([^"]+)"', re.MULTILINE)


def list_sinks() -> list[Sink]:
    """Every output sink pactl knows about, in its listing order."""
    proc = _run([PACTL, "list", "sinks"])
    if proc is None or proc.returncode != 0:
        return []
    sinks: list[Sink] = []
    for block in re.split(r"(?=^Sink #)", proc.stdout, flags=re.MULTILINE):
        name_match = _FIELD_NAME.search(block)
        if not name_match:
            continue
        name = name_match.group(1)
        desc_match = _FIELD_DESC.search(block)
        description = (desc_match.group(1).strip() if desc_match else "") or name
        state_match = _FIELD_STATE.search(block)
        state = state_match.group(1).upper() if state_match else "UNKNOWN"
        factory_match = _FIELD_FACTORY.search(block)
        factory = factory_match.group(1) if factory_match else ""
        label = f"{name} {description}".lower()
        virtual = ("null-audio-sink" in factory) or ("sunshine" in label)
        sinks.append(Sink(name, description, state, virtual))
    return sinks


def system_default_sink_name() -> str | None:
    proc = _run([PACTL, "get-default-sink"])
    if proc is None or proc.returncode != 0:
        return None
    line = proc.stdout.strip().splitlines()
    return line[0].strip() if line else None


def auto_pick(sinks: list[Sink] | None = None) -> Sink | None:
    """The default output: the **system default sink** when there is one (that's
    where the user actually hears things — on this machine the Moonlight path),
    else the physical-sink heuristic (analog → RUNNING → first)."""
    if sinks is None:
        sinks = list_sinks()
    default = system_default_sink_name()
    for sink in sinks:
        if sink.name == default:
            return sink
    physical = [s for s in sinks if not s.virtual]
    for sink in physical:
        if "analog" in sink.name.lower():
            return sink
    for sink in physical:
        if sink.state == "RUNNING":
            return sink
    if physical:
        return physical[0]
    return sinks[0] if sinks else None


# ---- input sources (microphone) ---------------------------------------------

@dataclass(frozen=True)
class Source:
    """One input device, as pactl describes it."""

    name: str
    description: str
    state: str      # RUNNING / IDLE / SUSPENDED / UNKNOWN
    monitor: bool   # a sink's monitor (captures output, not a microphone)


def list_sources() -> list[Source]:
    """Every input source pactl knows about, in its listing order."""
    proc = _run([PACTL, "list", "sources"])
    if proc is None or proc.returncode != 0:
        return []
    sources: list[Source] = []
    for block in re.split(r"(?=^Source #)", proc.stdout, flags=re.MULTILINE):
        name_match = _FIELD_NAME.search(block)
        if not name_match:
            continue
        name = name_match.group(1)
        desc_match = _FIELD_DESC.search(block)
        description = (desc_match.group(1).strip() if desc_match else "") or name
        state_match = _FIELD_STATE.search(block)
        state = state_match.group(1).upper() if state_match else "UNKNOWN"
        monitor = name.endswith(".monitor")
        sources.append(Source(name, description, state, monitor))
    return sources


def system_default_source_name() -> str | None:
    proc = _run([PACTL, "get-default-source"])
    if proc is None or proc.returncode != 0:
        return None
    line = proc.stdout.strip().splitlines()
    return line[0].strip() if line else None


def auto_source(sources: list[Source] | None = None) -> Source | None:
    """The input device "Automatic" means: the system default source, else the
    first real (non-monitor) source."""
    if sources is None:
        sources = list_sources()
    default = system_default_source_name()
    for source in sources:
        if source.name == default:
            return source
    real = [s for s in sources if not s.monitor]
    return real[0] if real else None


def source_display_name(source: Source) -> str:
    text = source.description
    if source.monitor and "monitor" not in text.lower():
        text += " — monitor"
    return text


def display_name(sink: Sink) -> str:
    """Human label for pickers: description, with virtual sinks called out."""
    text = sink.description
    if sink.virtual and "virtual" not in text.lower():
        text += " — virtual"
    return text


def play_file(path: str | Path, sink: str | None = None) -> subprocess.Popen | None:
    """Start playing a WAV asynchronously; returns the paplay process (or None)."""
    argv = [PLAYER]
    if sink:
        argv.append(f"--device={sink}")
    argv.append(str(path))
    try:
        return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None


def ensure_chime(path: str | Path) -> bool:
    """Write the Settings 'Test sound' chime once; True when the file is ready."""
    path = Path(path)
    if path.exists():
        return True
    try:
        import numpy as np

        sample_rate = 48000

        def tone(freq: float, seconds: float):
            t = np.linspace(0.0, seconds, int(sample_rate * seconds), endpoint=False)
            envelope = np.clip(np.minimum(t / 0.012, (seconds - t) / 0.05), 0.0, 1.0)
            return 0.30 * envelope * np.sin(2 * np.pi * freq * t)

        samples = np.concatenate([tone(659.25, 0.16), tone(987.77, 0.34)])  # E5 → B5
        pcm = np.clip(samples * 32767.0, -32768, 32767).astype("<i2")
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(pcm.tobytes())
        return True
    except Exception:
        return False
