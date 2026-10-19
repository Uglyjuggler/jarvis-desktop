#!/usr/bin/env python3
"""Phase 3 voice check: load the installed Kokoro model for real, resolve the
configured voice **by name**, synthesize the preview line, and report timing +
a rough pitch sanity signal. `--play` also speaks it on the app's auto-picked
output — the system default sink (on this PC that is the Moonlight path) —
or `--sink=<pactl name>`.

    python3 tests/voice_check.py                 # synth only
    python3 tests/voice_check.py --play          # speak it (brief; audible)
    python3 tests/voice_check.py --play --sink=alsa_output.pci-0000_00_1b.0.analog-stereo
    python3 tests/voice_check.py --text "One two three."

This never touches appdata; the sample WAV is written to /tmp/opencode/.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

from jarvis import audio, config, tts  # noqa: E402


def estimate_pitch(samples, sample_rate: int) -> float:
    """Crude FFT peak (Hz) of the loudest second — enough to spot a voice swap."""
    import numpy as np

    window = sample_rate
    best, peak = None, 0.0
    for start in range(0, max(1, len(samples) - window), window // 2):
        chunk = samples[start : start + window]
        energy = float(np.mean(chunk ** 2))
        if energy > peak:
            peak, best = energy, chunk
    if best is None:
        return 0.0
    spectrum = np.abs(np.fft.rfft(best * np.hanning(len(best))))
    freqs = np.fft.rfftfreq(len(best), 1 / sample_rate)
    band = (freqs > 60) & (freqs < 420)
    if not band.any():
        return 0.0
    return float(freqs[band][int(np.argmax(spectrum[band]))])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--play", action="store_true", help="play the sample (audible)")
    parser.add_argument("--sink", default="", help="pactl sink name (default: auto-pick)")
    parser.add_argument("--text", default="", help="line to speak (default: preview_text)")
    parser.add_argument("--voice", default="", help="voice by name (default: config voice)")
    parser.add_argument(
        "--out",
        default="/tmp/opencode/jarvis_voice_check.wav",
        help="where to write the sample WAV",
    )
    args = parser.parse_args()

    cfg = config.load()
    model_dir = config.model_dir(cfg)
    print(f"model dir : {model_dir}")
    if not model_dir.is_dir():
        print("FAIL: model dir missing — set voice.model_dir in appdata/config.json")
        return 1

    engine = tts.KokoroEngine(model_dir)
    t0 = time.time()
    if not engine.load():
        print(f"FAIL: {engine.last_error}")
        return 1
    print(f"loaded    : {time.time() - t0:.2f}s · {engine.num_speakers} speakers")

    catalog = engine.catalog() or []
    print(f"catalog   : {len(catalog)} voices (from the model's own metadata)")
    for voice in catalog:
        print(f"              {voice.sid:2d}  {voice.name:14s} {voice.label}")

    name = args.voice or cfg["voice"]["voice"]
    sid = engine.sid_for_name(name)
    if sid is None:
        print(f"FAIL: '{name}' is not in this model's catalog")
        return 1
    print(f"voice     : {name} → sid {sid} (resolved by NAME, never stored as a number)")

    text = args.text or str(cfg["voice"]["preview_text"])
    speed = float(cfg["voice"]["speed"])
    t0 = time.time()
    result = engine.synthesize(text, sid, speed)
    if result is None:
        print(f"FAIL: synthesis — {engine.last_error}")
        return 1
    samples, sample_rate = result
    synth_s = time.time() - t0
    audio_s = len(samples) / sample_rate
    print(f"synthesized: {audio_s:.2f}s audio in {synth_s:.2f}s (RTF {synth_s / max(audio_s, 1e-6):.2f}, speed {speed:.1f}×)")

    pitch = estimate_pitch(samples, sample_rate)
    register = "male" if len(name) > 1 and name[1] == "m" else ("female" if len(name) > 1 and name[1] == "f" else "?")
    lo, hi = (80.0, 220.0) if register == "male" else (150.0, 360.0)
    within = lo <= pitch <= hi
    print(f"pitch     : ~{pitch:.0f} Hz ({register} register expected {lo:.0f}–{hi:.0f} Hz) → {'ok' if within else 'WARN'}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not tts.write_wav(out, samples, sample_rate):
        print(f"FAIL: could not write {out}")
        return 1
    print(f"wrote     : {out}")

    if args.play:
        picked = audio.auto_pick()
        sink = args.sink or (picked.name if picked else None)
        label = f"{sink}" if sink else "system default"
        if picked and not args.sink:
            label += f" ({audio.display_name(picked)})"
        proc = audio.play_file(out, sink)
        if proc is None:
            print(f"FAIL: could not start playback (is paplay available?)")
            return 1
        proc.wait()
        print(f"played on : {label}")

    print("voice check ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
