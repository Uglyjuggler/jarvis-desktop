"""Kokoro voice output: the voice catalog, the local engine, and the speaking queue.

Jarvis speaks with sherpa-onnx Kokoro — the same local model News Anchor uses.
Brandon's voice is **bm_george** (British male), stored and chosen **by name**,
never by number.

The voice list is **data-driven from the installed model itself**, resolved at
runtime in this order (no code changes to swap models):

  1. `voice_names.txt` / `voice_names.json` in the model folder — a manual
     override ("one name per line", or JSON with optional labels),
  2. the model's own ONNX metadata (`speaker_names` / `id2speaker` /
     `n_speakers`) via a stdlib-only protobuf read of `model.onnx` — this
     machine's `kokoro-en-v0_19` carries its 11 voices there, and so does the
     larger official multi-language build,
  3. a small built-in list for known builds (offline fallback),
  4. generic `voice_0 … voice_N-1` labels sized by the model's own
     `num_speakers` once it is loaded.

Labels are inferred from the Kokoro naming convention when the model doesn't
provide them (first letter = language: a=US, b=UK, e=Spanish, f=French,
h=Hindi, i=Italian, j=Japanese, p=Portuguese, z=Chinese; second letter =
gender).

The shipped model (2026-10-18) is the full official **kokoro-multi-lang-v1_0**
(54 voices, downloaded into this project's `models/` folder — Brandon's call:
size is not a concern on his network). Its `model.onnx` carries all 54 names in
metadata, so the picker lists them with zero hardcoding. The earlier play
`kokoro-en-v0_19` (11 voices) still works by pointing `voice.model_dir` at it;
its catalog was ground-truthed in Phase 3 (model metadata, cross-checked by
pitch analysis: bm_george ≈ 142 Hz, bm_lewis ≈ 106 Hz, af ≈ 194 Hz):

    sid  0 af        1 af_bella   2 af_nicole  3 af_sarah   4 af_sky
    sid  5 am_adam   6 am_michael 7 bf_emma    8 bf_isabella
    sid  9 bm_george 10 bm_lewis

Replies are spoken sentence by sentence as they stream in, and synthesis of
the next sentence overlaps playback of the current one, so Jarvis starts
talking early and keeps a natural cadence. The model loads lazily (~2 s,
~350 MB) on the speech worker; `preload()` warms it at launch.
"""

from __future__ import annotations

import io
import json
import os
import queue
import re
import subprocess
import threading
import wave
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from . import audio

DEFAULT_MODEL_DIR = Path(__file__).resolve().parents[2] / "models" / "kokoro-multi-lang-v1_0"
DEFAULT_VOICE = "bm_george"  # by name; the classic British JARVIS register


@dataclass(frozen=True)
class Voice:
    sid: int
    name: str
    label: str  # "" when unknown


# ---- model-side catalog discovery ------------------------------------------

_LANG = {
    "a": "US",
    "b": "UK",
    "e": "Spanish",
    "f": "French",
    "h": "Hindi",
    "i": "Italian",
    "j": "Japanese",
    "p": "Portuguese",
    "z": "Chinese",
}


def infer_label(name: str) -> str:
    """US female / UK male / … from the Kokoro `[lang][sex]_name` convention."""
    match = re.match(r"^([abefhijpz])([fm])_", name)
    if not match:
        return ""
    lang, sex = match.groups()
    return f"{_LANG[lang]} {'female' if sex == 'f' else 'male'}"


def _read_varint(handle) -> int | None:
    result = shift = 0
    for _ in range(10):
        byte = handle.read(1)
        if not byte:
            return None
        value = byte[0]
        result |= (value & 0x7F) << shift
        if not value & 0x80:
            return result
        shift += 7
    return None


def _skip_field(handle, wire_type: int) -> bool:
    if wire_type == 0:
        return _read_varint(handle) is not None
    if wire_type == 1:
        return bool(handle.read(8))
    if wire_type == 2:
        length = _read_varint(handle)
        if length is None:
            return False
        handle.seek(length, 1)
        return True
    if wire_type == 5:
        return bool(handle.read(4))
    return False


def read_onnx_metadata(path: Path) -> dict[str, str]:
    """Best-effort read of the ONNX model's `metadata_props` (key → value).

    Walks the protobuf wire format field by field and seeks past the graph
    bytes, so it's fast even on 300 MB models. Any malformed input yields {} —
    metadata is a bonus, never a requirement.
    """
    metadata: dict[str, str] = {}

    def parse_entry(blob: bytes) -> tuple[str, str]:
        entry = io.BytesIO(blob)
        key = value = ""
        while True:
            tag = _read_varint(entry)
            if tag is None:
                break
            field, wire = tag >> 3, tag & 7
            if field == 1 and wire == 2:
                length = _read_varint(entry)
                key = entry.read(length or 0).decode("utf-8", "ignore")
            elif field == 2 and wire == 2:
                length = _read_varint(entry)
                value = entry.read(length or 0).decode("utf-8", "ignore")
            elif not _skip_field(entry, wire):
                break
        return key, value

    try:
        with open(path, "rb") as handle:
            while True:
                tag = _read_varint(handle)
                if tag is None:
                    break
                field, wire = tag >> 3, tag & 7
                if field == 14 and wire == 2:  # ModelProto.metadata_props
                    length = _read_varint(handle)
                    if length is None:
                        break
                    key, value = parse_entry(handle.read(length))
                    if key:
                        metadata[key] = value
                elif not _skip_field(handle, wire):
                    break
    except Exception:
        pass
    return metadata


def _names_from_metadata(model_dir: Path) -> list[tuple[str, str]] | None:
    """(name, label) pairs in sid order from the model's own ONNX metadata."""
    meta = read_onnx_metadata(model_dir / "model.onnx")
    raw = meta.get("speaker_names")
    if raw:
        names = [chunk.strip() for chunk in raw.split(",") if chunk.strip()]
        if names:
            return [(name, infer_label(name)) for name in names]
    raw = meta.get("id2speaker")
    if raw:
        pairs: list[tuple[int, str]] = []
        for chunk in raw.split(","):
            left, _, right = chunk.partition("->")
            right = right.strip()
            if right:
                try:
                    pairs.append((int(left.strip()), right))
                except ValueError:
                    continue
        if pairs:
            pairs.sort()
            return [(name, infer_label(name)) for _, name in pairs]
    return None


def _names_from_files(model_dir: Path) -> list[tuple[str, str]] | None:
    """Manual override: `voice_names.txt` (name[, label] per line) or `.json`."""
    text_path = model_dir / "voice_names.txt"
    if text_path.is_file():
        entries: list[tuple[str, str]] = []
        for line in text_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            name, _, label = line.partition(",")
            name = name.strip()
            label = label.strip() or infer_label(name)
            if name:
                entries.append((name, label))
        if entries:
            return entries
    json_path = model_dir / "voice_names.json"
    if json_path.is_file():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            data = []
        entries = []
        for item in data if isinstance(data, list) else []:
            if isinstance(item, dict) and item.get("name"):
                name = str(item["name"])
                entries.append((name, str(item.get("label", "")) or infer_label(name)))
            elif isinstance(item, str) and item:
                entries.append((item, infer_label(item)))
        if entries:
            return entries
    return None


# Offline fallback for known builds when the model file can't be read.
_SEED_VOICES: dict[str, list[tuple[str, str]]] = {
    "kokoro-en-v0_19": [
        (name, infer_label(name))
        for name in (
            "af", "af_bella", "af_nicole", "af_sarah", "af_sky",
            "am_adam", "am_michael", "bf_emma", "bf_isabella",
            "bm_george", "bm_lewis",
        )
    ],
}


# ---- engine -----------------------------------------------------------------


class KokoroEngine:
    """The sherpa-onnx Kokoro model: loaded once, used from the speech worker."""

    def __init__(self, model_dir: Path | str = DEFAULT_MODEL_DIR) -> None:
        self._dir = Path(model_dir)
        self._tts = None
        self._lock = threading.Lock()
        self._discover_lock = threading.Lock()
        self._discovered: list[Voice] | None = None
        self.last_error = ""
        self.on_loaded = None  # callable, set by Speaker (catalog changed)

    # -- data-driven catalog (works before the model is loaded) -------------

    def _discover(self) -> list[Voice]:
        with self._discover_lock:
            if self._discovered is not None:
                return self._discovered
            entries = _names_from_files(self._dir)
            if entries is None:
                entries = _names_from_metadata(self._dir)
            if entries is None:
                entries = _SEED_VOICES.get(self._dir.name)
            voices = [
                Voice(sid=index, name=name, label=label)
                for index, (name, label) in enumerate(entries or [])
            ]
            self._discovered = voices
            return voices

    @property
    def num_speakers(self) -> int | None:
        tts = self._tts  # atomic read; assignment happens on the load thread
        if tts is None:
            return None
        try:
            return int(tts.num_speakers)
        except Exception:
            return None

    def catalog(self) -> list[Voice] | None:
        """The installed model's voices, reconciled with its real speaker count."""
        voices = list(self._discover())
        count = self.num_speakers
        if count is None:
            return voices or None
        if len(voices) > count:
            voices = voices[:count]
        while len(voices) < count:
            sid = len(voices)
            voices.append(Voice(sid, f"voice_{sid}", "unnamed speaker"))
        return voices or None

    def sid_for_name(self, name: str) -> int | None:
        for voice in self.catalog() or []:
            if voice.name == name:
                return voice.sid
        return None

    # -- model ---------------------------------------------------------------

    def _files_present(self) -> bool:
        return all(
            (self._dir / name).exists()
            for name in ("model.onnx", "voices.bin", "tokens.txt", "espeak-ng-data")
        )

    def load(self) -> bool:
        """Idempotent and thread-safe; the slow call (keep it off the UI thread)."""
        loaded_now = False
        with self._lock:
            if self._tts is not None:
                return True
            if not self._files_present():
                self.last_error = f"Kokoro model not found in {self._dir}"
                return False
            try:
                import sherpa_onnx

                # Multi-lang builds ship a jieba dict (Chinese) and lexicons
                # (US English, Chinese — the official sherpa-onnx pairing);
                # use them when present. Single-lang builds don't have these.
                extra: dict[str, str] = {}
                dict_dir = self._dir / "dict"
                if dict_dir.is_dir():
                    extra["dict_dir"] = str(dict_dir)
                lexicons = [
                    path
                    for path in (
                        self._dir / "lexicon-us-en.txt",
                        self._dir / "lexicon-zh.txt",
                    )
                    if path.is_file()
                ]
                if lexicons:
                    extra["lexicon"] = ",".join(str(path) for path in lexicons)

                config = sherpa_onnx.OfflineTtsConfig(
                    model=sherpa_onnx.OfflineTtsModelConfig(
                        kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                            model=str(self._dir / "model.onnx"),
                            voices=str(self._dir / "voices.bin"),
                            tokens=str(self._dir / "tokens.txt"),
                            data_dir=str(self._dir / "espeak-ng-data"),
                            **extra,
                        ),
                        # Measured on this 6-core Xeon: 6 threads is the sweet
                        # spot (RTF ≈ 0.65); hyperthreads made it slower.
                        num_threads=max(2, (os.cpu_count() or 4) // 2),
                        provider="cpu",
                    ),
                )
                self._tts = sherpa_onnx.OfflineTts(config)
                loaded_now = True
            except Exception as exc:
                self.last_error = f"Couldn't load Kokoro: {exc.__class__.__name__}: {exc}"
                return False
        if loaded_now and self.on_loaded is not None:
            self.on_loaded()  # now the real speaker count is known
        return True

    def synthesize(self, text: str, sid: int, speed: float):
        """(float32 samples, sample_rate) or None on failure."""
        if not self.load():
            return None
        try:
            import numpy as np

            result = self._tts.generate(text, sid=int(sid), speed=float(speed))
            samples = np.asarray(result.samples, dtype="float32")
            if samples.size == 0:
                return None
            return samples, int(result.sample_rate)
        except Exception as exc:
            self.last_error = f"Speech synthesis failed: {exc.__class__.__name__}: {exc}"
            return None


# ---- spoken-text helpers ----------------------------------------------------

_SENTENCE_END = re.compile(r"[.!?…]+(?=\s)")
_MARKUP = re.compile(r"[*_`#]+")
_BULLET = re.compile(r"^\s*[-•]\s*")
_SENTENCE_MIN = 16  # "Mr." / "3." stay attached to what follows


# Spoken respellings: words the model pronounces unlike the local saying.
# Frankfort, KY is said "Frank-furt" (silent T); the respelling makes
# Kokoro's grapheme rules land on that sound. Speech only — never display.
_PRONUNCIATIONS = (
    (re.compile(r"\bFrankfort\b"), "Frankfurt"),
    (re.compile(r"\bfrankfort\b"), "frankfurt"),
)


def clean_for_speech(text: str) -> str:
    """Strip light markdown so it isn't read aloud as symbols."""
    text = _MARKUP.sub("", text).strip()
    for pattern, spoken in _PRONUNCIATIONS:
        text = pattern.sub(spoken, text)
    return text


def _take_sentences(buffer: str) -> tuple[list[str], str]:
    """Split off complete sentences; the tail (no trailing space yet) stays."""
    pieces: list[str] = []
    start = 0
    for match in _SENTENCE_END.finditer(buffer):
        end = match.end()
        if end - start < _SENTENCE_MIN:
            continue
        sentence = _BULLET.sub("", buffer[start:end].strip())
        if sentence:
            pieces.append(sentence)
        start = end
    return pieces, buffer[start:].lstrip()


@dataclass(frozen=True)
class _Job:
    text: str
    voice_name: str
    speed: float
    sink: str | None
    generation: int


_UNSET = object()


def write_wav(path: str | Path, samples, sample_rate: int) -> bool:
    """Kokoro float samples → a 16-bit mono WAV."""

    def _write() -> bool:
        import numpy as np

        pcm = np.clip(np.asarray(samples, dtype="float32") * 32767.0, -32768, 32767)
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(int(sample_rate))
            handle.writeframes(pcm.astype("<i2").tobytes())
        return True

    try:
        return _write()
    except Exception:
        return False


class Speaker(QObject):
    """One worker thread: synthesize each queued sentence, play through paplay.

    Streaming: `feed()` during deltas collects complete sentences; `flush()`
    speaks the remainder when the reply ends. `speak()` (Preview) cancels and
    speaks now — it works even while spoken replies are toggled off.
    `cancel()` stops playback instantly; a new question calls it first.

    Voices are chosen by NAME; the sid is resolved against the installed
    model's catalog when the sentence is synthesized.
    """

    busy_changed = pyqtSignal(bool)
    catalog_changed = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, tts_dir: Path, engine: KokoroEngine | None = None, parent=None) -> None:
        super().__init__(parent)
        self._dir = Path(tts_dir)
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        # Fresh start: drop our own temp audio from a previous run (never user files).
        try:
            for stale in self._dir.glob("*.wav"):
                stale.unlink(missing_ok=True)
        except OSError:
            pass

        self._engine = engine or KokoroEngine()
        self._engine.on_loaded = self._notify_catalog
        self._queue: queue.Queue[_Job] = queue.Queue()
        self._lock = threading.Lock()
        self._generation = 0
        self._inflight = 0
        self._busy = False
        self._proc: subprocess.Popen | None = None
        self._proc_path: Path | None = None
        self._buffer = ""
        self._enabled = True
        self._voice = DEFAULT_VOICE
        self._speed = 1.0
        self._sink: str | None = None
        self._sequence = 0
        self._warned = False

        self._worker = threading.Thread(target=self._loop, name="jarvis-tts", daemon=True)
        self._worker.start()

    # ---- catalog ------------------------------------------------------------

    def catalog(self):
        """The installed model's voices (see KokoroEngine.catalog)."""
        return self._engine.catalog()

    def _notify_catalog(self) -> None:
        self.catalog_changed.emit()

    def preload(self) -> None:
        """Warm the model (blocking; call from a background thread at launch)."""
        self._engine.load()

    # ---- configuration ----------------------------------------------------

    def configure(
        self,
        enabled: bool | None = None,
        voice: str | None = None,
        speed: float | None = None,
        sink=_UNSET,
    ) -> None:
        """Apply new settings; later sentences pick them up immediately."""
        stop_now = False
        with self._lock:
            if enabled is not None:
                self._enabled = bool(enabled)
                if not self._enabled:
                    stop_now = True
            if voice is not None:
                self._voice = voice
            if speed is not None:
                self._speed = max(0.5, min(2.0, float(speed)))
            if sink is not _UNSET:
                self._sink = sink
        if stop_now:
            self.cancel()

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._inflight > 0

    # ---- streaming input ---------------------------------------------------

    def feed(self, piece: str) -> None:
        if not piece:
            return
        sentences: list[str] = []
        with self._lock:
            if not self._enabled:
                return
            self._buffer += piece
            sentences, self._buffer = _take_sentences(self._buffer)
        for sentence in sentences:
            self._enqueue_markup(sentence)

    def flush(self) -> None:
        """Reply finished — speak whatever is left in the buffer."""
        with self._lock:
            if not self._enabled:
                self._buffer = ""
                return
            remainder, self._buffer = self._buffer.strip(), ""
        if remainder:
            self._enqueue_markup(remainder)

    def speak(self, text: str, voice: str | None = None, speed: float | None = None) -> None:
        """Speak now (voice preview): drop the queue, then speak this."""
        self.cancel()
        text = clean_for_speech(text)
        if not text:
            return
        with self._lock:
            chosen_speed = self._speed if speed is None else max(0.5, min(2.0, float(speed)))
        self._enqueue(text, voice_name=voice, speed=chosen_speed)

    # ---- control -----------------------------------------------------------

    def cancel(self) -> None:
        """Stop immediately: drop the queue, kill the current playback."""
        dropped = 0
        proc = None
        path = None
        changed = False
        busy = False
        with self._lock:
            self._generation += 1
            self._buffer = ""
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
                self._queue.task_done()
                dropped += 1
            proc = self._proc
            path = self._proc_path
            self._proc = None
            self._proc_path = None
            self._inflight = max(0, self._inflight - dropped - (1 if proc else 0))
            busy = self._inflight > 0
            changed = busy != self._busy
            self._busy = busy
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass
        if path is not None:
            self._unlink(path)
        if changed:
            self.busy_changed.emit(busy)

    # ---- internals ---------------------------------------------------------

    def _enqueue_markup(self, raw: str) -> None:
        text = clean_for_speech(raw)
        if text:
            self._enqueue(text)

    def _enqueue(self, text: str, voice_name: str | None = None, speed: float | None = None) -> None:
        with self._lock:
            job = _Job(
                text=text,
                voice_name=voice_name or self._voice,
                speed=self._speed if speed is None else speed,
                sink=self._sink,
                generation=self._generation,
            )
            self._queue.put(job)
        self._add_inflight(1)

    def _add_inflight(self, delta: int) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight + delta)
            busy = self._inflight > 0
            changed = busy != self._busy
            self._busy = busy
        if changed:
            self.busy_changed.emit(busy)

    def _job_current(self, job: _Job) -> bool:
        with self._lock:
            return job.generation == self._generation

    def _next_path(self) -> Path:
        with self._lock:
            self._sequence += 1
            return self._dir / f"speech_{self._sequence:05d}.wav"

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass

    def _warn_once(self, message: str) -> None:
        with self._lock:
            if self._warned:
                return
            self._warned = True
        self.error.emit(message)

    def _settle_playback(self) -> None:
        """Wait for the previous paplay to finish, clean its file, count it done."""
        with self._lock:
            proc, path = self._proc, self._proc_path
            self._proc = self._proc_path = None
        if proc is None:
            return
        try:
            proc.wait()
        except Exception:
            pass
        if path is not None:
            self._unlink(path)
        self._add_inflight(-1)

    def _loop(self) -> None:
        while True:
            job = self._queue.get()
            started_playing = False
            try:
                if not self._job_current(job):
                    continue
                if not self._engine.load():
                    self._warn_once(self._engine.last_error or "Speech failed.")
                    continue
                sid = self._engine.sid_for_name(job.voice_name)
                result = self._engine.synthesize(job.text, 0 if sid is None else sid, job.speed)
                if result is None:
                    self._warn_once(self._engine.last_error or "Speech failed.")
                    continue
                if not self._job_current(job):
                    continue
                samples, sample_rate = result
                path = self._next_path()
                if not write_wav(path, samples, sample_rate):
                    continue
                # Previous sentence finishes before this one starts; its synth
                # (just done) overlapped that playback, so there's no gap.
                self._settle_playback()
                if not self._job_current(job):
                    self._unlink(path)
                    continue
                proc = audio.play_file(path, job.sink)
                if proc is None:
                    self._unlink(path)
                    self._warn_once("Couldn't play speech — is paplay available?")
                    continue
                with self._lock:
                    self._proc = proc
                    self._proc_path = path
                started_playing = True
                if self._queue.empty():
                    # Nothing to synthesize ahead — wait the playback out now.
                    self._settle_playback()
            finally:
                self._queue.task_done()
                if not started_playing:
                    self._add_inflight(-1)
