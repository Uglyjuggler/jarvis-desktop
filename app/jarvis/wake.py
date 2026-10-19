"""Wake word: an always-listening “hey jarvis”, opt-in, entirely on this PC.

openWakeWord (Apache-2.0) — a chain of tiny ONNX networks (mel → embedding →
`hey_jarvis_v0.1`) that score 80 ms frames at 16 kHz. This module runs that
chain on its **own worker thread** over a raw `parec` stream of the
configured voice-input source (the same capture path push-to-talk uses), and
emits `wake(score, wav)` over Qt signals when the phrase is heard. Nothing
leaves the machine.

Phase 5.1: a firing is only a *candidate*. The listener keeps the last few
seconds of mic audio in a small rolling buffer, so when the score fires it can
hand the app the exact moment as a WAV — the app transcribes that snippet with
the local SenseVoice model and only a transcript that really contains the name
(`transcript_has_wake`, spelling wobble allowed) becomes a wake. The same
buffer-feeding loop also estimates the room's noise floor (see
`stt.NoiseFloor`) and exposes it for the recorder's end-of-speech VAD, so
takes end relative to the actual room, loud or quiet.

Honesty rules this module keeps:
  * the model files live in the project's `models/openwakeword/` folder and are
    loaded from there — never from site-packages;
  * the configured source is used exactly as resolved by the caller;
    a stream that dies reports `failed` instead of silently switching mics;
  * `pause()` blocks inference completely (frames are still drained so the
    stream stays real-time) — the UI can then say the truth: not listening.

`hey_jarvis_v0.1` (and the shared mel/embedding models) are byte-identical to
the ones shipped in openwakeword 0.4.0 (the newest release installable on
Python 3.12 — 0.5+ demand `tflite-runtime`, which has no 3.12 wheels); their
sha256 sums are recorded in NOTES.md.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import wave
from collections import deque
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from . import stt

DEFAULT_MODEL_DIR = Path(__file__).resolve().parents[2] / "models" / "openwakeword"
WAKE_MODEL = "hey_jarvis_v0.1.onnx"
MEL_MODEL = "melspectrogram.onnx"
EMBEDDING_MODEL = "embedding_model.onnx"

FRAME_SAMPLES = 1280               # 80 ms at 16 kHz — openWakeWord's frame
FRAME_BYTES = FRAME_SAMPLES * 2    # s16le
HOT_FRAMES = 2                     # frames ≥ threshold in a row before a wake
DEFAULT_THRESHOLD = 0.5            # openWakeWord's own recommendation; tunable
BUFFER_SECONDS = 3.5               # rolling pre-trigger audio kept for verification
POST_ROLL_FRAMES = 3               # extra frames after the trigger (80 ms each)
WAKE_NAME = "jarvis"               # the name the verification transcript must contain


# ---- the wake log (Brandon judges the fix from this file, not a summary) -----

def log_event(
    path: str | Path,
    score: float,
    source: str = "",
    text: str = "",
    accepted: bool = True,
    reason: str = "",
) -> None:
    """Append one candidate (timestamp, score, transcript, verdict) to the log.

    Every candidate is logged — accepted *and* rejected — so false wakes can
    be counted and read, not guessed about. `reason` is "" for accepted
    candidates, otherwise why it was dismissed: no-wake-word / no-audio /
    stt-error / verify-error.
    """
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "score": round(float(score), 3),
            "source": source or "",
            "text": (text or "").strip(),
            "accepted": bool(accepted),
            "reason": reason or "",
        }
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # logging is a nicety; it never takes the app down


def log_summary(path: str | Path) -> tuple[int, int, str]:
    """(accepted wakes, rejected candidates, last timestamp) of the log.

    Phase 5 lines (`{ts, score, source}`) predate verification and were all
    accepted wakes; they count as such.
    """
    accepted, rejected, last = 0, 0, ""
    try:
        for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if not isinstance(entry, dict):
                continue
            if bool(entry.get("accepted", True)):
                accepted += 1
            else:
                rejected += 1
            if entry.get("ts"):
                last = str(entry["ts"])
    except Exception:
        pass
    return accepted, rejected, last


# ---- verification: the name must really be in the moment ---------------------

def _edit_distance_at_most_1(first: str, second: str) -> bool:
    """One substitution, insertion, deletion — or a transposition — at most."""
    if abs(len(first) - len(second)) > 1:
        return False
    if len(first) == len(second):
        diffs = [i for i, (a, b) in enumerate(zip(first, second)) if a != b]
        if len(diffs) <= 1:
            return True
        if len(diffs) == 2 and diffs[1] == diffs[0] + 1:
            i, j = diffs
            return first[i] == second[j] and first[j] == second[i]
        return False
    longer, shorter = (first, second) if len(first) > len(second) else (second, first)
    i = j = 0
    skipped = False
    while i < len(longer) and j < len(shorter):
        if longer[i] == shorter[j]:
            i += 1
            j += 1
            continue
        if skipped:
            return False
        skipped = True
        i += 1
    return True


def transcript_has_wake(text: str) -> bool:
    """True when the transcript really contains the name, wobble allowed.

    Straight substring match would miss STT spelling wobble ("Jervis",
    "Jarvas"); loose matching would accept TV dialogue that merely sounds
    close. This goes word by word: the name itself, or one edit away from it,
    apostrophes stripped ("Jarvis's" → "jarviss" → one edit).
    """
    words = re.findall(r"[a-z']+", (text or "").lower())
    for word in words:
        clean = word.replace("'", "")
        if clean == WAKE_NAME or _edit_distance_at_most_1(clean, WAKE_NAME):
            return True
    return False


# ---- the listener ------------------------------------------------------------

class WakeListener(QObject):
    """“hey jarvis” detection on a worker thread; signals carry everything out.

    `start(source)` begins listening on a concrete source name (the caller
    resolves it — see window._resolve_wake_source). `pause()` / `resume()`
    gate inference (self-trigger protection and recording suppression);
    a detection pauses the listener by itself until `resume()`, so one phrase
    fires once. `stop()` frees the model and the stream.

    A detection is a **candidate**: the signal carries the score and the path
    of the last `BUFFER_SECONDS` of mic audio (written under `buffer_dir`) so
    the app can verify the moment by transcribing it. While listening, the
    same frames feed a trailing-room-floor estimate (`noise_floor_db`) that
    the recorder's end-of-speech VAD starts from.
    """

    ready = pyqtSignal()             # model loaded + stream open: truly listening
    wake = pyqtSignal(float, str)    # candidate: (score, candidate WAV path or "")
    failed = pyqtSignal(str)         # human-readable reason listening stopped

    def __init__(
        self,
        model_dir: str | Path | None = None,
        threshold: float = DEFAULT_THRESHOLD,
        buffer_dir: str | Path | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._dir = Path(model_dir) if model_dir else DEFAULT_MODEL_DIR
        self._threshold = self._clamp(threshold)
        self._buffer_dir = Path(buffer_dir) if buffer_dir else None
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._stop_event = threading.Event()
        self._paused = threading.Event()
        self._lock = threading.Lock()
        self._label = WAKE_MODEL[:-5]  # "hey_jarvis_v0.1" — the prediction key
        self._model = None
        self._frames = 0
        self._max_score = 0.0
        self._wakes = 0
        self._buffer: deque[bytes] = deque(
            maxlen=max(1, int(round(BUFFER_SECONDS * 1000 / 80.0)))
        )
        self._floor = stt.NoiseFloor()
        self.last_error = ""

    # ---- state ---------------------------------------------------------------

    @staticmethod
    def _clamp(value: object) -> float:
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            number = DEFAULT_THRESHOLD
        return max(0.05, min(0.99, number))

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def noise_floor_db(self) -> float | None:
        """The room's trailing noise-floor estimate, or None before it exists."""
        return self._floor.value

    @property
    def stats(self) -> dict:
        """Frames scored, best score, candidates, room floor since the last start()."""
        return {
            "frames": self._frames,
            "max_score": round(self._max_score, 4),
            "wakes": self._wakes,
            "threshold": self._threshold,
            "floor_db": None if self._floor.value is None else round(self._floor.value, 1),
        }

    def set_threshold(self, value: float) -> None:
        self._threshold = self._clamp(value)

    def files_present(self) -> bool:
        return all(
            (self._dir / name).is_file()
            for name in (MEL_MODEL, EMBEDDING_MODEL, WAKE_MODEL)
        )

    # ---- control ---------------------------------------------------------------

    def start(self, source: str) -> bool:
        """Begin listening on `source`; False when it can't even try."""
        if self.running:
            return True
        if not self.files_present():
            self.last_error = f"Wake-word model files not found in {self._dir}"
            return False
        self._stop_event.clear()
        self._paused.clear()
        self._frames = 0
        self._max_score = 0.0
        self._wakes = 0
        self._buffer.clear()
        self._floor.clear()  # a new stream is a new room until frames say otherwise
        self.last_error = ""
        self._thread = threading.Thread(
            target=self._run, args=(source,), name="jarvis-wake", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        """Stop listening and free the model (off means off)."""
        self._stop_event.set()
        self._paused.clear()
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._thread = None
        self._model = None
        self._buffer.clear()
        # The floor estimate is kept: it is the last honest reading of the room
        # (start() resets it) and the checks report it after stop().

    def pause(self) -> None:
        """Stop scoring frames (they are still drained so the stream stays live)."""
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    # ---- the worker ----------------------------------------------------------

    def _build_model(self):
        import warnings

        from openwakeword.model import Model

        with warnings.catch_warnings():
            # openWakeWord asks for a CUDA provider unconditionally; onnxruntime
            # 1.31 says so once per session and runs on CPU regardless. One noisy
            # line about a GPU this machine doesn't use — filtered, not hidden.
            warnings.filterwarnings("ignore", message="Specified provider")
            return Model(
                wakeword_model_paths=[str(self._dir / WAKE_MODEL)],
                melspec_onnx_model_path=str(self._dir / MEL_MODEL),
                embedding_onnx_model_path=str(self._dir / EMBEDDING_MODEL),
            )

    def _open_capture(self, source: str) -> subprocess.Popen:
        """Spawn the raw capture process — a seam for the checks (they feed a
        synthetic stream through the real listener; the app always runs parec)."""
        return subprocess.Popen(
            stt.capture_argv(source),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def _run(self, source: str) -> None:
        try:
            model = self._build_model()
        except Exception as exc:
            self.last_error = (
                f"Couldn't load the wake-word model: {exc.__class__.__name__}: {exc}"
            )
            self.failed.emit(self.last_error)
            return
        try:
            proc = self._open_capture(source)
        except OSError as exc:
            self.last_error = f"Couldn't open the microphone ({exc.__class__.__name__})."
            self.failed.emit(self.last_error)
            return

        self._model = model
        with self._lock:
            self._proc = proc
        self._buffer.clear()
        self.ready.emit()
        hot = 0
        pending_reset = False
        try:
            import numpy as np

            while not self._stop_event.is_set():
                chunk = (
                    proc.stdout.read(FRAME_BYTES) if proc.stdout is not None else b""
                )
                if not chunk:
                    if not self._stop_event.is_set():
                        self.last_error = "The microphone stream stopped."
                        self.failed.emit(self.last_error)
                    break
                if self._paused.is_set():
                    # Drained, not scored and not buffered. Reset on resume: the
                    # phrase may have been spoken into the suppressed window —
                    # and it must not leak into the next candidate's audio.
                    pending_reset = True
                    continue
                if pending_reset:
                    try:
                        model.reset()
                    except Exception:
                        pass
                    self._buffer.clear()
                    self._floor.clear()
                    pending_reset = False
                self._buffer.append(chunk)
                self._floor.feed(stt.frame_level_db(chunk))
                try:
                    frame = np.frombuffer(chunk, dtype="<i2")
                    scores = model.predict(frame)
                    if self._label in scores:
                        score = float(scores[self._label])
                    else:
                        score = float(next(iter(scores.values()), 0.0))
                except Exception as exc:
                    self.last_error = (
                        f"Wake-word inference failed: {exc.__class__.__name__}: {exc}"
                    )
                    self.failed.emit(self.last_error)
                    break
                self._frames += 1
                if score > self._max_score:
                    self._max_score = score
                if score >= self._threshold:
                    hot += 1
                    if hot >= HOT_FRAMES:
                        hot = 0
                        self._wakes += 1
                        self._paused.set()  # fire once until the app resumes us
                        try:
                            model.reset()
                        except Exception:
                            pass
                        pending_reset = False
                        wav_path = self._capture_candidate(proc)
                        self.wake.emit(float(score), wav_path)
                else:
                    hot = 0
        finally:
            with self._lock:
                self._proc = None
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

    # ---- the candidate audio -------------------------------------------------

    def _capture_candidate(self, proc: subprocess.Popen) -> str:
        """Grab a little audio after the trigger, then write the rolling buffer
        out as a WAV for verification. Post-roll frames are buffered but never
        scored; `""` means no WAV could be produced (no buffer dir / no audio)."""
        for _ in range(POST_ROLL_FRAMES):
            if self._stop_event.is_set() or proc.stdout is None:
                break
            more = proc.stdout.read(FRAME_BYTES)
            if not more:
                break
            self._buffer.append(more)
        if self._buffer_dir is None or not self._buffer:
            return ""
        try:
            self._buffer_dir.mkdir(parents=True, exist_ok=True)
            path = self._buffer_dir / "wake_candidate.wav"
            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(stt.SAMPLE_RATE)
                handle.writeframes(b"".join(self._buffer))
            return str(path)
        except Exception:
            return ""
