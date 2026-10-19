"""Speech-to-text: recording + local SenseVoice transcription.

Hold the mic button (or say the wake word, Phase 5) → `parec` streams from the
chosen input source ("Automatic" = the system default source) → the WAV is
transcribed **on a worker thread** by sherpa-onnx SenseVoice (int8,
non-autoregressive — the fastest accurate English option on this CPU: a short
utterance decodes in well under a second) → the text is delivered via Qt
signals and lands in the chat exactly like typed words.

One capture path serves both: the recorder reads parec's stdout on its own
thread (Phase 5 needed the audio *as it arrives* for end-of-speech detection,
which a file being written by parecord can't give), writes the WAV itself, and
optionally runs an energy-VAD `AutoStop` that ends the take after the silence
tail — the wake word's "speak your question" mode. The silence line is always
relative to the room's own measured noise floor (Phase 5.1), never an absolute
level. Push-to-talk is unchanged from the outside: `start()` … `stop()`
returns the WAV.

Nothing here touches the network and nothing is ever invented: an empty or
failed transcription is reported as such.

The model lives in the project's `models/` folder (git-ignored) like the
Kokoro voice; `stt.model_dir` in config points elsewhere if ever wanted.
"""

from __future__ import annotations

import math
import os
import subprocess
import threading
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

PAREC = "parec"
DEFAULT_MODEL_DIR = Path(__file__).resolve().parents[2] / "models" / "sense-voice"
SAMPLE_RATE = 16000  # what the model wants; parec resamples for us
MIN_SECONDS = 0.35   # shorter holds are "didn't catch that", not transcriptions
FRAME_BYTES = 2560   # 1280 samples = 80 ms at 16 kHz — one read at a time


# ---- recording ---------------------------------------------------------------

def capture_argv(source: str | None) -> list[str]:
    """`parec` streaming raw s16le mono 16 kHz on stdout.

    One capture path for push-to-talk and the wake word: the recorder's reader
    thread writes the WAV itself, so an end-of-speech VAD can watch the audio
    as it arrives.
    """
    argv = [
        PAREC,
        "--format=s16le",
        f"--rate={SAMPLE_RATE}",
        "--channels=1",
        # Default target latency buffers ~1 s that is lost when the capture is
        # stopped — measured on this box 2026-10-19 (5 s hold → 4.0 s WAV at
        # default, 4.9 s at 100 ms). 100 ms keeps the tail and still leaves
        # plenty of headroom against dropouts on PipeWire.
        "--latency-msec=100",
    ]
    if source:
        argv.append(f"--device={source}")
    return argv


def wav_seconds(path: str | Path) -> float:
    """Duration of a 16-bit PCM WAV, or 0.0 when it is unreadable."""
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate() or 1
            return handle.getnframes() / float(rate)
    except Exception:
        return 0.0


def discard(path: str | Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def frame_level_db(chunk: bytes) -> float:
    """RMS level of a raw s16le chunk in dBFS (−120 when there is nothing)."""
    import numpy as np

    samples = np.frombuffer(chunk, dtype="<i2").astype("float32")
    if samples.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(samples * samples))) / 32768.0
    return 20.0 * math.log10(rms) if rms > 0.0 else -120.0


class NoiseFloor:
    """A trailing low-percentile estimate of the room's noise floor, in dBFS.

    Speech comes in bursts, so the quiet end of the recent frame levels is the
    room: the floor is the `percentile`-th percentile of the last
    `window_seconds` of levels. It falls the moment quieter frames arrive and
    rises only as loud frames age out — no absolute calibration, so a loud
    room (TV + speakers bleeding into the mic at ≈ −19 dBFS, measured through
    Brandon's laptop mic 2026-10-19) is as usable as a quiet one. `value` is
    None until enough frames exist; `seed()` starts from an estimate taken
    elsewhere (the wake listener watches the same room before a take).
    """

    def __init__(
        self, window_seconds: float = 10.0, percentile: float = 10.0, min_frames: int = 5
    ) -> None:
        self._percentile = percentile
        self._min_frames = max(1, int(min_frames))
        self._levels: deque[float] = deque(
            maxlen=max(self._min_frames, int(round(window_seconds * 1000 / 80.0)))
        )
        self.value: float | None = None

    def clear(self) -> None:
        self._levels.clear()
        self.value = None

    def seed(self, floor_db: float) -> None:
        """Begin from a floor measured elsewhere; real frames refine it."""
        self._levels.clear()
        self._levels.append(float(floor_db))
        self.value = float(floor_db)

    def feed(self, level_db: float) -> float | None:
        import numpy as np

        self._levels.append(level_db)
        if len(self._levels) >= self._min_frames:
            self.value = float(np.percentile(self._levels, self._percentile))
        return self.value


@dataclass
class AutoStop:
    """End-of-speech tuning for wake recordings (an energy VAD, no model).

    End-of-speech is judged **relative to the room, never against an absolute
    level**: speech is anything `margin_db` above the trailing noise-floor
    estimate (see `NoiseFloor`), and once speech has been heard the take ends
    after `silence_seconds` back at the floor. The floor stops learning the
    moment speech starts, so a voice can't drag the line up with it. A take
    also ends after `no_speech_seconds` if nothing was ever heard, and at
    `max_seconds` no matter what.
    """

    silence_seconds: float = 1.0
    max_seconds: float = 15.0
    no_speech_seconds: float = 6.0
    margin_db: float = 8.0              # speech = floor + this (dB)
    floor_window_seconds: float = 10.0  # trailing window the floor watches
    floor_percentile: float = 10.0      # the quiet end of that window


class _SilenceTracker:
    """Frame-by-frame energy VAD; `feed()` returns True when the take must end.

    `floor_db` (the wake listener's current estimate for the same room) seeds
    the floor so a take can judge its very first frame; without a seed the
    floor simply builds up over the first few frames.
    """

    def __init__(self, auto: AutoStop, floor_db: float | None = None) -> None:
        self._auto = auto
        self._floor = NoiseFloor(auto.floor_window_seconds, auto.floor_percentile)
        if floor_db is not None:
            self._floor.seed(floor_db)
        self.floor_db: float | None = self._floor.value
        self.threshold_db: float | None = (
            None if self.floor_db is None else self.floor_db + auto.margin_db
        )
        self.heard = False
        self.reason = ""  # silence | timeout | cap
        self._silence_run = 0.0
        self._elapsed = 0.0

    def feed(self, chunk: bytes) -> bool:
        dt = (len(chunk) // 2) / float(SAMPLE_RATE)
        if dt <= 0.0:
            return False
        level_db = frame_level_db(chunk)
        self._elapsed += dt

        floor = self._floor.value
        if floor is not None:
            self.floor_db = floor
            self.threshold_db = floor + self._auto.margin_db
            if level_db >= self.threshold_db:
                self.heard = True
                self._silence_run = 0.0
            elif self.heard:
                self._silence_run += dt
                if self._silence_run >= self._auto.silence_seconds:
                    self.reason = "silence"
                    return True
        if not self.heard:
            # Only the quiet part of the take teaches the floor; once speech
            # starts the line is frozen so the voice can't pull it upward.
            self._floor.feed(level_db)

        if not self.heard and self._elapsed >= self._auto.no_speech_seconds:
            self.reason = "timeout"
            return True
        if self._elapsed >= self._auto.max_seconds:
            self.reason = "cap"
            return True
        return False


class Recorder:
    """One `parec` capture at a time — push-to-talk and the wake word share it.

    The audio is read from parec's stdout on this class's own thread, so the
    reader can watch the level and stop itself at end of speech (`auto_stop`),
    then write the WAV and tell the caller through `on_auto_stop` — called on
    that thread (the wake flow marshals it to the UI with a Qt signal). Without
    `auto_stop` this is still the Phase 4 press-and-hold recorder: `stop()`
    finalizes and returns the file. `last_reason` says how the take ended.
    """

    def __init__(self, directory: Path | str) -> None:
        self._dir = Path(directory)
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self._path = self._dir / "input.wav"
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop_requested = False
        self._final_path: Path | None = None
        self._auto: AutoStop | None = None
        self._on_auto_stop = None
        self._floor_db: float | None = None
        self.last_reason = ""           # manual | silence | timeout | cap | eof
        self.last_heard_speech = False
        self.last_floor_db: float | None = None      # the room estimate the VAD used
        self.last_threshold_db: float | None = None  # floor + margin at the end

    @property
    def recording(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def start(
        self,
        source: str | None = None,
        auto_stop: AutoStop | None = None,
        on_auto_stop=None,
        floor_db: float | None = None,
    ) -> bool:
        """Begin recording; False when parec can't be launched.

        `floor_db` is an optional head start for the VAD's room estimate (the
        wake flow passes the listener's current one — it has been watching the
        same room since before the take began).
        """
        if self.recording:
            return False
        discard(self._path)
        self._stop_requested = False
        self._final_path = None
        self._auto = auto_stop
        self._on_auto_stop = on_auto_stop if auto_stop is not None else None
        self._floor_db = floor_db if auto_stop is not None else None
        self.last_reason = ""
        self.last_heard_speech = False
        self.last_floor_db = None
        self.last_threshold_db = None
        try:
            proc = subprocess.Popen(
                capture_argv(source),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            self._auto = None
            self._on_auto_stop = None
            return False
        self._proc = proc
        self._thread = threading.Thread(
            target=self._read_loop, args=(proc,), name="jarvis-record", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> Path | None:
        """Stop recording; the WAV path, or None when nothing was captured."""
        self._stop_requested = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=4)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        path = self._final_path
        self._final_path = None
        self._proc = None
        if thread is None or not thread.is_alive():
            self._thread = None
        return path

    # ---- the reader thread ---------------------------------------------------

    def _read_loop(self, proc: subprocess.Popen) -> None:
        frames: list[bytes] = []
        tracker = (
            _SilenceTracker(self._auto, self._floor_db)
            if self._auto is not None
            else None
        )
        reason = "manual"
        auto_fired = False
        try:
            while True:
                if self._stop_requested:
                    reason = "manual"
                    break
                chunk = proc.stdout.read(FRAME_BYTES) if proc.stdout is not None else b""
                if not chunk:
                    reason = "manual" if self._stop_requested else "eof"
                    break
                frames.append(chunk)
                if tracker is not None and tracker.feed(chunk):
                    reason = tracker.reason
                    auto_fired = True
                    break
        except Exception:
            reason = "eof"
        finally:
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            if frames:
                written = self._write_wav(frames)
                if written is not None:
                    self._final_path = written
            self.last_reason = reason
            self.last_heard_speech = bool(tracker is not None and tracker.heard)
            self.last_floor_db = tracker.floor_db if tracker is not None else None
            self.last_threshold_db = tracker.threshold_db if tracker is not None else None
        callback = self._on_auto_stop if auto_fired else None
        self._auto = None
        if callback is not None:
            try:
                callback()
            except Exception:
                pass

    def _write_wav(self, frames: list[bytes]) -> Path | None:
        try:
            with wave.open(str(self._path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(SAMPLE_RATE)
                handle.writeframes(b"".join(frames))
            return self._path
        except Exception:
            return None


# ---- transcription -----------------------------------------------------------

class SttEngine:
    """Lazily-loaded sherpa-onnx SenseVoice recognizer (worker-thread use)."""

    def __init__(self, model_dir: Path | str = DEFAULT_MODEL_DIR, language: str = "en") -> None:
        self._dir = Path(model_dir)
        self._language = language
        self._rec = None
        self._load_lock = threading.Lock()
        self._use_lock = threading.Lock()  # one decode at a time
        self.last_error = ""

    def _model_path(self) -> Path | None:
        for name in ("model.int8.onnx", "model.onnx"):
            candidate = self._dir / name
            if candidate.is_file():
                return candidate
        return None

    def files_present(self) -> bool:
        return self._model_path() is not None and (self._dir / "tokens.txt").is_file()

    def load(self) -> bool:
        """Idempotent, thread-safe, and slow — never call on the UI thread."""
        with self._load_lock:
            if self._rec is not None:
                return True
            model = self._model_path()
            tokens = self._dir / "tokens.txt"
            if model is None or not tokens.is_file():
                self.last_error = f"Speech model not found in {self._dir}"
                return False
            try:
                import sherpa_onnx

                self._rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=str(model),
                    tokens=str(tokens),
                    num_threads=max(2, (os.cpu_count() or 4) // 2),
                    language=self._language,
                    use_itn=True,  # punctuation + number normalization in one pass
                )
            except Exception as exc:
                self.last_error = f"Couldn't load the speech model: {exc.__class__.__name__}: {exc}"
                return False
        return True

    def transcribe(self, path: Path | str) -> str | None:
        """Text for the WAV, ``""`` for silence, None on failure (see last_error)."""
        if not self.load():
            return None
        try:
            import numpy as np

            with wave.open(str(path), "rb") as handle:
                rate = handle.getframerate()
                channels = handle.getnchannels()
                frames = handle.readframes(handle.getnframes())
            if not frames:
                return ""
            samples = np.frombuffer(frames, dtype="<i2").astype("float32") / 32768.0
            if channels > 1:
                samples = samples.reshape(-1, channels).mean(axis=1)
            if rate != SAMPLE_RATE and samples.size:
                # the recorder asks for 16 kHz; this only smooths odd inputs (test wavs).
                target = int(samples.size * SAMPLE_RATE / rate)
                samples = np.interp(
                    np.linspace(0, samples.size - 1, target), np.arange(samples.size), samples
                ).astype("float32")

            with self._use_lock:
                stream = self._rec.create_stream()
                stream.accept_waveform(SAMPLE_RATE, samples)
                self._rec.decode_stream(stream)
                text = stream.result.text
            return (text or "").strip()
        except Exception as exc:
            self.last_error = f"Transcription failed: {exc.__class__.__name__}: {exc}"
            return None


class TranscribeWorker(QObject):
    """Runs one transcription on a daemon thread; Qt queues the result back."""

    finished = pyqtSignal(str)   # the transcript ("" when nothing was heard)
    failed = pyqtSignal(str)     # human-readable error

    def __init__(self, engine: SttEngine, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._engine = engine
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    def start(self, wav_path: Path | str) -> bool:
        if self._busy:
            return False
        self._busy = True
        threading.Thread(
            target=self._run,
            args=(Path(wav_path),),
            name="jarvis-stt",
            daemon=True,
        ).start()
        return True

    def _run(self, path: Path) -> None:
        try:
            text = self._engine.transcribe(path)
            if text is None:
                self.failed.emit(self._engine.last_error or "Transcription failed.")
            else:
                self.finished.emit(text)
        except Exception as exc:  # never let the worker die silently
            self.failed.emit(f"Transcription failed ({exc.__class__.__name__}).")
        finally:
            self._busy = False
            discard(path)
