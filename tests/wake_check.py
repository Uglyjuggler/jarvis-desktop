#!/usr/bin/env python3
"""Phase 5 wake-word check — the real model, real numbers, no play-acting.

    python3 tests/wake_check.py            # offline: does the model fire on
                                           # Kokoro *saying* “Hey jarvis”? (a
                                           # synthetic-voice check — Brandon's
                                           # own voice remains the real test)
                                           # + Phase 5.1: the room-relative
                                           # AutoStop under a loud noise bed,
                                           # the verification name match, and
                                           # the verification latency
    python3 tests/wake_check.py --live 60  # the listener for real, on the
                                           # configured mic, for 60 s: frames,
                                           # best score, wakes, idle CPU, RSS
    python3 tests/wake_check.py --live 90 --verify
                                           # + transcribe each candidate like
                                           # the app does and print the verdict

Nothing is played out loud and nothing leaves the machine. `--live` opens the
real microphone through the same `WakeListener` the app uses; a quiet room is
expected to produce silence-level scores — that is the false-wake baseline.
"""

from __future__ import annotations

import argparse
import io
import math
import os
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import numpy as np  # noqa: E402

from jarvis import audio, config, stt, wake  # noqa: E402
from jarvis.tts import KokoroEngine  # noqa: E402

RATE = 16000


def _rss_mb() -> float:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return -1.0


def _peak_rss_mb() -> float:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return -1.0


def _level_db(samples: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(samples.astype("float64") ** 2))) / 32768.0
    return 20.0 * math.log10(rms) if rms > 0.0 else -120.0


def _to_int16(samples, rate: int) -> np.ndarray:
    if rate != RATE and samples.size:
        target = int(samples.size * RATE / rate)
        samples = np.interp(
            np.linspace(0, samples.size - 1, target), np.arange(samples.size), samples
        )
    return np.clip(np.asarray(samples, dtype="float32") * 32767.0, -32768, 32767).astype("<i2")


def _write_wav(path: Path, samples: np.ndarray, rate: int = RATE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.astype("<i2").tobytes())
    return path


def _max_score(model, samples: np.ndarray) -> tuple[float, int]:
    """(best score, frame index) over 80 ms frames — model state is reset first."""
    model.reset()
    best, at = 0.0, -1
    for index, start in enumerate(range(0, samples.size - 1280 + 1, 1280)):
        out = model.predict(samples[start:start + 1280])
        score = float(next(iter(out.values()), 0.0))
        if score > best:
            best, at = score, index
    return best, at


# ---- the Phase 5.1 AutoStop, measured --------------------------------------

def _run_tracker(samples: np.ndarray, floor_db: float | None = None):
    """Drive the real `_SilenceTracker` frame by frame; (reason, at, heard, floor, threshold)."""
    auto = stt.AutoStop()
    tracker = stt._SilenceTracker(auto, floor_db=floor_db)
    elapsed = 0.0
    for start in range(0, samples.size - 1280 + 1, 1280):
        elapsed += 0.08
        if tracker.feed(samples[start:start + 1280].tobytes()):
            return tracker.reason, elapsed, tracker.heard, tracker.floor_db, tracker.threshold_db
    return "did-not-stop", elapsed, tracker.heard, tracker.floor_db, tracker.threshold_db


def _old_math_stop(samples: np.ndarray):
    """Phase 5's exact absolute-threshold logic (20th percentile of the first
    0.35 s, +10 dB, clamped −55…−35) — kept only for the before/after record."""
    levels: list[float] = []
    threshold: float | None = None
    heard = False
    silence_run = 0.0
    elapsed = 0.0
    for start in range(0, samples.size - 1280 + 1, 1280):
        chunk = samples[start:start + 1280]
        elapsed += 0.08
        level = _level_db(chunk)
        if threshold is None:
            levels.append(level)
            if elapsed < 0.35:
                continue
            floor = float(np.percentile(levels, 20))
            threshold = min(max(floor + 10.0, -55.0), -35.0)
        if level >= threshold:
            heard = True
            silence_run = 0.0
        elif heard:
            silence_run += 0.08
            if silence_run >= 1.0:
                return "silence", elapsed, heard
        if not heard and elapsed >= 6.0:
            return "timeout", elapsed, heard
        if elapsed >= 15.0:
            return "cap", elapsed, heard
    return "did-not-stop", elapsed, heard


def _stream_integration(
    model_dir: Path, clip: np.ndarray, stt_engine: stt.SttEngine, scratch: Path
) -> int:
    """Feed a synthesized phrase through the real `WakeListener` with a fake
    parec (raw WAV bytes on stdout) — exercises the Phase 5.1 listener half
    end to end (rolling buffer → candidate WAV → signal) without a
    microphone, then transcribes the candidate exactly like the app does."""
    from PyQt6.QtCore import QCoreApplication

    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    data = np.concatenate([
        np.zeros(int(0.5 * RATE), dtype="<i2"),
        clip,
        np.zeros(int(0.5 * RATE), dtype="<i2"),
    ]).tobytes()

    class _FakeProc:
        def __init__(self, payload: bytes) -> None:
            self.stdout = io.BytesIO(payload)

        def poll(self):
            return None

        def terminate(self) -> None:
            pass

        def wait(self, timeout=None) -> int:
            return 0

        def kill(self) -> None:
            pass

    listener = wake.WakeListener(
        model_dir=model_dir, threshold=0.5, buffer_dir=scratch / "candidate"
    )
    captured: list[tuple[float, str]] = []
    listener.wake.connect(lambda score, wav: captured.append((score, wav)))
    real_open = wake.WakeListener._open_capture
    wake.WakeListener._open_capture = lambda self, source: _FakeProc(data)  # type: ignore[assignment]
    try:
        if not listener.start("fake-mic"):
            print(f"  FAIL: the listener refused to start: {listener.last_error}")
            return 1
        thread = listener._thread
        deadline = time.time() + 30
        while thread is not None and thread.is_alive() and time.time() < deadline:
            app.processEvents()
            time.sleep(0.01)
        app.processEvents()  # deliver the queued signals
    finally:
        wake.WakeListener._open_capture = real_open  # type: ignore[assignment]
        listener.stop()

    if not captured:
        print(f"  FAIL: the synthesized phrase never produced a candidate "
              f"({listener.last_error or 'stream ended quietly'})")
        return 1
    failures = 0
    for score, wav in captured:
        size = Path(wav).stat().st_size / (RATE * 2) if wav else 0.0
        print(f"  fired: score {score:.3f} → candidate {wav} ({size:.1f}s captured)")
        t0 = time.time()
        text = stt_engine.transcribe(wav)
        elapsed = time.time() - t0
        accepted = bool(text) and wake.transcript_has_wake(text or "")
        print(
            f"  verify: “{(text or '').strip()}” in {elapsed:.2f}s — "
            f"{'ACCEPTED' if accepted else 'rejected'}"
        )
        if not accepted:
            failures += 1
    return failures


def offline() -> int:
    failures = 0
    cfg = config.load()
    model_dir = config.wake_model_dir(cfg)
    print(f"model dir : {model_dir}")
    for name in (wake.MEL_MODEL, wake.EMBEDDING_MODEL, wake.WAKE_MODEL):
        path = model_dir / name
        print(f"  {name:24s} {'present' if path.is_file() else 'MISSING':8s} "
              f"{(path.stat().st_size / 1024 / 1024 if path.is_file() else 0):.2f} MB")

    listener = wake.WakeListener(model_dir=model_dir, threshold=0.5)
    t0 = time.time()
    model = listener._build_model()  # the app's own build path (project-local files)
    print(f"load      : {time.time() - t0:.2f}s  (threshold {listener.threshold})")

    lead = np.zeros(int(0.5 * RATE), dtype="<i2")  # let the model warm up first

    silence = np.concatenate([lead, np.zeros(int(3.0 * RATE), dtype="<i2")])
    best, _at = _max_score(model, silence)
    print(f"silence   : max {best:.4f}")

    engine = KokoroEngine(config.model_dir(cfg))
    if not engine.load():
        print(f"FAIL: Kokoro — {engine.last_error}")
        return 1
    george_clip = None
    for voice in ("bm_george", "am_michael", "af_alloy"):
        sid = engine.sid_for_name(voice)
        if sid is None:
            print(f"voice     : {voice} — not in the installed model, skipped")
            continue
        result = engine.synthesize("Hey jarvis.", sid, 1.0)
        if result is None:
            print(f"voice     : {voice} — synthesis failed: {engine.last_error}")
            continue
        clip = _to_int16(*result)
        if voice == "bm_george":
            george_clip = clip
        best, at = _max_score(model, np.concatenate([lead, clip]))
        verdict = "FIRES" if best >= listener.threshold else "below threshold"
        print(
            f"“Hey jarvis.” ({voice:10s}): max {best:.4f} at frame {at:3d} — {verdict}"
        )

    # ---- Phase 5.1, issue 1: end-of-speech is relative to the room ----------
    print()
    print("autostop — end of speech is judged against the room's own floor:")
    if george_clip is not None:
        spoken = np.concatenate([george_clip, np.zeros(int(2.5 * RATE), dtype="<i2")])
        reason, at, heard, floor, threshold = _run_tracker(spoken)
        clip_end = george_clip.size / RATE
        print(
            f"  quiet room: speech {clip_end:.2f}s + 2.5s tail → stopped at {at:.2f}s "
            f"— {reason}, heard {heard}, floor {floor:.1f} dBFS, line {threshold:.1f} dBFS"
        )
        if reason != "silence" or at - clip_end > 2.0:
            print("  FAIL: the quiet-room take did not end shortly after the speech")
            failures += 1
    reason, at, heard, _floor, _threshold = _run_tracker(np.zeros(int(8 * RATE), dtype="<i2"))
    print(f"  silence only → stopped at {at:.2f}s — {reason}, heard {heard} (the 6 s guard)")
    if reason != "timeout" or heard:
        print("  FAIL: digital silence must end by the nothing-heard timeout")
        failures += 1

    if george_clip is not None:
        # Brandon's measured room: a steady ≈ −19 dBFS bed (TV + open speakers
        # into the laptop mic). Speech sits ~10 dB above it, like a voice.
        rng = np.random.default_rng(7)
        bed_rms = 10 ** (-19.0 / 20.0)
        clip_rms = float(np.sqrt(np.mean(george_clip.astype("float64") ** 2))) / 32768.0
        gain = (bed_rms * 10 ** (10.0 / 20.0)) / max(clip_rms, 1e-9)
        speech = (george_clip.astype("float32") * gain)
        lead_n = int(0.4 * RATE)
        tail_n = int(3.0 * RATE)
        noise = rng.standard_normal(lead_n + speech.size + tail_n) * bed_rms * 32768.0
        audio = noise.copy()
        audio[lead_n:lead_n + speech.size] += speech
        mix = np.clip(audio, -32768, 32767).astype("<i2")
        speech_end = (lead_n + speech.size) / RATE
        bed_measured = _level_db(mix[:lead_n])
        speech_measured = _level_db(mix[lead_n:lead_n + speech.size])
        print(
            f"  loud room: bed {bed_measured:.1f} dBFS measured, speech {speech_measured:.1f} dBFS "
            f"(+{speech_measured - bed_measured:.1f} dB), speech ends at {speech_end:.2f}s"
        )
        for label, seed in (("unseeded", None), ("seeded by listener", -19.0)):
            reason, at, heard, floor, threshold = _run_tracker(mix, seed)
            late = at - speech_end
            print(
                f"  loud room, {label:18s}: stopped at {at:.2f}s = speech end "
                f"+{late:.2f}s — {reason}, heard {heard}, floor {floor:.1f}, line {threshold:.1f}"
            )
            if reason != "silence" or late > 2.0 or not heard:
                print("  FAIL: the take must end within ~2 s of the speech, not at the cap")
                failures += 1
        old_reason, old_at, old_heard = _old_math_stop(mix)
        print(
            f"  loud room, {str('old Phase 5 math'):18s}: stopped at {old_at:.2f}s — {old_reason}, "
            f"heard {old_heard} (the reported bug: the −35 dBFS clamp sits under the bed)"
        )

    # ---- Phase 5.1, issue 2: the transcript must contain the name -----------
    print()
    print("verification — the name match (one edit of wobble allowed):")
    cases = [
        ("hey jarvis", True),
        ("Hey, Jarvis!", True),
        ("hey jervis", True),
        ("Jarviss?", True),
        ("Jarvis's", True),
        ("hey javis", True),
        ("hey, Travis", False),
        ("Hey, Bart's.", False),
        ("Charles is here", False),
        ("", False),
    ]
    for text, expected in cases:
        got = wake.transcript_has_wake(text)
        mark = "ok " if got == expected else "FAIL"
        if got != expected:
            failures += 1
        print(f"  [{mark}] {text!r:24s} → {got}")

    if george_clip is not None:
        # The engine the app uses for verification, loaded for real.
        stt_engine = stt.SttEngine(config.stt_model_dir(cfg))
        t0 = time.time()
        loaded = stt_engine.load()
        load_seconds = time.time() - t0
        if not loaded:
            print(f"FAIL: SenseVoice — {stt_engine.last_error}")
            failures += 1
        else:
            print()
            print("listener integration — the real listener over a synthetic parec stream:")
            failures += _stream_integration(
                model_dir, george_clip, stt_engine, Path("/tmp/opencode/wake_stream")
            )

            # Verification latency: the exact snippet shape the app transcribes
            # (the rolling buffer's last seconds), through the real engine.
            candidate = np.concatenate([
                george_clip,
                np.zeros(int(1.4 * RATE), dtype="<i2"),
            ])
            candidate_path = _write_wav(
                Path("/tmp/opencode/wake_candidate_check.wav"), candidate
            )
            t0 = time.time()
            text = stt_engine.transcribe(candidate_path)
            cold = time.time() - t0
            t0 = time.time()
            text2 = stt_engine.transcribe(candidate_path)
            warm = time.time() - t0
            accepted = bool(text) and wake.transcript_has_wake(text or "")
            print()
            print(
                f"verify    : {candidate.size / RATE:.2f}s candidate → “{text}” "
                f"in {warm:.2f}s warm ({load_seconds:.2f}s model load, {cold:.2f}s first call)"
            )
            print(f"            accepted: {accepted} (name really in the transcript)")
            if not accepted or (text2 or "").strip() != (text or "").strip():
                print("FAIL: the real engine must find the name in the real candidate")
                failures += 1

    negative = model_dir.parent / "sense-voice" / "test_wavs" / "en.wav"
    if negative.is_file():
        with wave.open(str(negative), "rb") as handle:
            pcm = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2")
        best, _at = _max_score(model, np.concatenate([lead, pcm]))
        print(f"negative control (speech, no wake phrase): max {best:.4f}")

    print(
        "note      : synthetic voice only — Kokoro is not Brandon; the acceptance\n"
        "            test is him saying it from the laptop mic."
    )
    if failures:
        print(f"FAIL: {failures} check(s) failed")
        return 1
    return 0


def live(seconds: float, threshold: float, source: str, warmup: float, verify: bool) -> int:
    from PyQt6.QtCore import QCoreApplication, QTimer

    app = QCoreApplication(sys.argv)
    cfg = config.load()
    if not source:
        source = (cfg.get("stt") or {}).get("source") or ""
    if not source:
        picked = audio.auto_source()
        source = picked.name if picked else ""
    if not source:
        print("FAIL: no input source")
        return 1
    print(f"source    : {source}")
    print(f"model dir : {config.wake_model_dir(cfg)}")
    print(f"threshold : {threshold}")

    engine = None
    if verify:
        engine = stt.SttEngine(config.stt_model_dir(cfg))
        t0 = time.time()
        if not engine.load():
            print(f"FAIL: SenseVoice — {engine.last_error}")
            return 1
        print(f"verify    : SenseVoice loaded in {time.time() - t0:.2f}s "
              f"(each candidate is transcribed before it counts)")

    baseline_mb = _rss_mb()
    cpu_before = os.times()
    listener = wake.WakeListener(model_dir=config.wake_model_dir(cfg), threshold=threshold)
    events: list[tuple[float, float]] = []

    def _verdict(score: float, wav: str) -> None:
        events.append((time.time(), score))
        stamp = time.strftime("%H:%M:%S")
        if not verify:
            print(f"candidate : {stamp} — {score:.3f} (not verified; listener stays paused)")
            return
        t0 = time.time()
        text = engine.transcribe(wav) if wav else None
        elapsed = time.time() - t0
        accepted = bool(text) and wake.transcript_has_wake(text or "")
        print(
            f"candidate : {stamp} — {score:.3f} → “{(text or '').strip()}” "
            f"in {elapsed:.2f}s — {'ACCEPTED' if accepted else 'rejected'}"
        )
        QTimer.singleShot(1500, listener.resume)  # the app's post-wake cool-down

    listener.wake.connect(_verdict)
    listener.ready.connect(
        lambda: print(f"ready     : {time.strftime('%H:%M:%S')} · RSS {_rss_mb():.0f} MB")
    )
    listener.failed.connect(lambda message: print(f"failed    : {message}"))
    if not listener.start(source):
        print(f"FAIL: {listener.last_error}")
        return 1

    # A steady-state sample excludes model load and first-frame warm-up.
    cpu_mid: list[object] = []
    if 0 < warmup < seconds:
        QTimer.singleShot(int(warmup * 1000), lambda: cpu_mid.append(os.times()))
    QTimer.singleShot(int(seconds * 1000), app.quit)
    app.exec()
    listener.stop()
    cpu_after = os.times()
    stats = listener.stats

    cpu_seconds = (cpu_after.user - cpu_before.user) + (
        cpu_after.system - cpu_before.system
    )
    peak_mb = _peak_rss_mb()
    print(f"window    : {seconds:.0f}s")
    print(f"frames    : {stats['frames']} scored ({stats['frames'] * 0.08:.1f}s of audio)")
    print(f"max score : {stats['max_score']} (threshold {stats['threshold']})")
    print(f"floor     : {stats['floor_db']} dBFS (trailing 10 s room estimate)")
    print(f"wakes     : {len(events)}")
    for when, score in events:
        print(f"            {time.strftime('%H:%M:%S', time.localtime(when))} — {score:.3f}")
    print(
        f"CPU total : {cpu_seconds:.2f}s over {seconds:.0f}s "
        f"= {100.0 * cpu_seconds / max(seconds, 1e-9):.1f}% of one core (includes load)"
    )
    if cpu_mid:
        mid = cpu_mid[0]
        steady = (cpu_after.user - mid.user) + (cpu_after.system - mid.system)
        span = seconds - warmup
        print(
            f"CPU steady: {steady:.2f}s over {span:.0f}s "
            f"= {100.0 * steady / max(span, 1e-9):.1f}% of one core "
            f"(after {warmup:.0f}s warm-up)"
        )
    print(
        f"RSS       : {_rss_mb():.0f} MB now, peak {peak_mb:.0f} MB "
        f"(baseline {baseline_mb:.0f} MB, listener +{peak_mb - baseline_mb:.0f} MB peak)"
    )
    print("note      : real mic, real idle room — every candidate listed above is a real\n"
            "            detection (or a false one) from this window.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", type=float, default=0.0, help="listen for this many seconds")
    parser.add_argument("--threshold", type=float, default=wake.DEFAULT_THRESHOLD)
    parser.add_argument("--source", default="", help="pactl source name (default: configured/auto)")
    parser.add_argument(
        "--warmup", type=float, default=15.0,
        help="seconds excluded from the steady-state CPU sample (default 15)",
    )
    parser.add_argument(
        "--verify", action="store_true",
        help="with --live: transcribe each candidate like the app does and print the verdict",
    )
    args = parser.parse_args()
    if args.live > 0:
        return live(args.live, args.threshold, args.source, args.warmup, args.verify)
    return offline()


if __name__ == "__main__":
    raise SystemExit(main())
