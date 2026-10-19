#!/usr/bin/env python3
"""Phase 4 speech-input check: load the installed SenseVoice model for real and
transcribe — the bundled English test clip (default), the actual microphone
(`--mic 4`), or a Kokoro-synthesized line (`--roundtrip`: a known script read
back to prove the text → voice → text loop without a microphone).

    python3 tests/stt_check.py                 # bundled en.wav
    python3 tests/stt_check.py --mic 4         # record 4 s from the configured/default mic
    python3 tests/stt_check.py --autostop      # wake-word mode: record until ~1 s of silence
    python3 tests/stt_check.py --roundtrip     # Kokoro speaks → SenseVoice reads
    python3 tests/stt_check.py --source alsa_input.usb-3142_Fifine_Microphone-00.mono-fallback --mic 3

This never touches appdata; WAVs land in /tmp/opencode/.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

from jarvis import audio, config, stt, tts  # noqa: E402

SCRATCH = Path("/tmp/opencode")


def _normalize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mic", type=float, default=0.0, help="record this many seconds first")
    parser.add_argument("--autostop", action="store_true", help="record until end of speech (the wake-word capture mode)")
    parser.add_argument("--source", default="", help="pactl source name (default: configured/auto)")
    parser.add_argument("--roundtrip", action="store_true", help="speak a known line with Kokoro, then read it back")
    parser.add_argument("--model-dir", default="", help="STT model folder (default: config)")
    args = parser.parse_args()

    cfg = config.load()
    model_dir = Path(args.model_dir) if args.model_dir else config.stt_model_dir(cfg)
    language = str((cfg.get("stt") or {}).get("language", "en"))
    print(f"model dir : {model_dir}")
    print(f"language  : {language or 'auto'} · threads {max(2, (os.cpu_count() or 4) // 2)}")

    engine = stt.SttEngine(model_dir, language=language)
    t0 = time.time()
    if not engine.load():
        print(f"FAIL: {engine.last_error}")
        return 1
    print(f"loaded    : {time.time() - t0:.2f}s")

    wav_path: Path
    reference = ""
    if args.autostop:
        source = args.source or (cfg.get("stt") or {}).get("source") or (audio.auto_source().name if audio.auto_source() else None)
        print(f"source    : {source or 'system default'}")
        recorder = stt.Recorder(SCRATCH / "stt-autostop")
        auto = stt.AutoStop(max_seconds=args.mic if args.mic > 0 else 12.0)
        if not recorder.start(source, auto_stop=auto):
            print("FAIL: could not start the recorder (parec)")
            return 1
        print(f"recording : stop-on-silence, max {auto.max_seconds:.0f}s … speak now")
        deadline = time.time() + auto.max_seconds + 3.0
        while recorder.recording and time.time() < deadline:
            time.sleep(0.1)
        wav_path = recorder.stop() or Path("/nonexistent")
        reason = recorder.last_reason
        heard = recorder.last_heard_speech
        print(f"ended     : {reason} · speech heard: {heard}")
        if reason == "eof" or not wav_path.exists():
            print("FAIL: no audio captured")
            return 1
        if not heard and reason == "timeout":
            print("ended     : nothing above the room noise floor — nothing to transcribe (this is not a failure, nobody spoke)")
            return 0
    elif args.mic > 0:
        source = args.source or (cfg.get("stt") or {}).get("source") or (audio.auto_source().name if audio.auto_source() else None)
        print(f"source    : {source or 'system default'}")
        recorder = stt.Recorder(SCRATCH / "stt-live")
        if not recorder.start(source):
            print("FAIL: could not start the recorder (parec)")
            return 1
        print(f"recording : {args.mic:.1f}s … speak now")
        time.sleep(args.mic)
        wav_path = recorder.stop() or Path("/nonexistent")
        if not wav_path.exists():
            print("FAIL: no audio captured")
            return 1
    elif args.roundtrip:
        voice_engine = tts.KokoroEngine(config.model_dir(cfg))
        if not voice_engine.load():
            print(f"FAIL: voice model — {voice_engine.last_error}")
            return 1
        voice = str(cfg["voice"]["voice"])
        sid = voice_engine.sid_for_name(voice)
        if sid is None:
            print(f"FAIL: voice '{voice}' not in the installed model")
            return 1
        reference = "Please turn off the living room lights and play some quiet music."
        result = voice_engine.synthesize(reference, sid, float(cfg["voice"]["speed"]))
        if result is None:
            print(f"FAIL: synthesis — {voice_engine.last_error}")
            return 1
        samples, rate = result
        wav_path = SCRATCH / "stt_roundtrip.wav"
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        if not tts.write_wav(wav_path, samples, rate):
            print(f"FAIL: could not write {wav_path}")
            return 1
        print(f"spoken    : “{reference}” ({voice}, {len(samples)/rate:.1f}s at {rate} Hz)")
    else:
        wav_path = model_dir / "test_wavs" / "en.wav"
        if not wav_path.exists():
            print(f"FAIL: {wav_path} missing")
            return 1
        print(f"input     : {wav_path} (bundled test clip)")

    seconds = stt.wav_seconds(wav_path)
    t0 = time.time()
    text = engine.transcribe(wav_path)
    elapsed = time.time() - t0
    if text is None:
        print(f"FAIL: {engine.last_error}")
        return 1
    rtf = elapsed / max(seconds, 1e-6)
    print(f"transcript: “{text}”")
    print(f"timing    : {seconds:.2f}s audio in {elapsed:.2f}s (RTF {rtf:.2f})")
    if not text.strip():
        print("FAIL: empty transcript")
        return 1
    if reference:
        want, got = _normalize(reference), _normalize(text)
        hits = sum(1 for word in want if word in got)
        print(f"similarity: {hits}/{len(want)} reference words present ({100*hits/len(want):.0f}%)")
    print("stt check ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
