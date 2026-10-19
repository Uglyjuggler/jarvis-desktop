#!/usr/bin/env python3
"""Offscreen smoke test for the Jarvis Desktop UI — no model synthesis, no mic.

Constructs the real window, drives the render paths (health report, transcript
turns, streaming bubble, error notice, new session), and checks the Phase 3–6
pieces: the data-driven voice catalog (from the installed model's own
metadata), config persistence + healing, the Settings dialog wiring (voice /
output / input / wake word / tools), the push-to-talk routing — with recording
and the transcription worker stubbed — the wake-word state machine with the
listener itself stubbed (`wake.WakeListener` is replaced before the window is
built: no model load, no microphone, and deliberately no fake wake results —
stubbed is stubbed), and the Phase 6 read-only tools (the real router decides;
the lookup result is injected through a seam — the real Open-Meteo calls live
in tests/tools_check.py, never faked here). Phase 5.1 candidate verification is
wired through an injected verdict — the real engine's decode inside that path
is measured in tests/wake_check.py, never faked here. Nothing audible or
microphone-shaped is faked as a result. State, config, and the appdata runtime
folders are isolated into a temp dir — real appdata is never touched.

    QT_QPA_PLATFORM=offscreen python3 tests/smoke.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import wave
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

from PyQt6.QtCore import QObject, QTimer, pyqtSignal  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from jarvis import audio, config, models, state, stt, tools, wake  # noqa: E402
from jarvis.ui.window import JarvisWindow  # noqa: E402


def _write_silent_wav(path: Path, seconds: float) -> Path:
    """A real (silent) 16 kHz WAV so duration gates run on honest data."""
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * int(16000 * seconds))
    return path


class FakeWakeListener(QObject):
    """Stands in for `wake.WakeListener` in every offscreen run.

    Records start/stop/pause/resume and the threshold; emits nothing by
    itself — this suite tests wiring, never wake detection. Phase 5.1: the
    `wake` signal carries (score, candidate WAV path) and `noise_floor_db`
    stands in for the room estimate the take can be seeded with.
    """

    ready = pyqtSignal()
    wake = pyqtSignal(float, str)
    failed = pyqtSignal(str)

    def __init__(self, model_dir=None, threshold=0.5, buffer_dir=None, parent=None) -> None:
        super().__init__(parent)
        self.model_dir = model_dir
        self.buffer_dir = buffer_dir
        self.threshold = threshold
        self.running = False
        self.paused = False
        self.started_with = "not started"
        self.noise_floor_db = None
        self.last_error = ""

    def start(self, source: str) -> bool:
        self.running = True
        self.paused = False
        self.started_with = source
        self.ready.emit()
        return True

    def stop(self) -> None:
        self.running = False
        self.paused = False

    def pause(self) -> None:
        if self.running:
            self.paused = True

    def resume(self) -> None:
        self.paused = False

    def set_threshold(self, value: float) -> None:
        self.threshold = value


def main() -> int:
    # ---- isolation: state + config + appdata live in a temp dir ------------
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-smoke-"))
    state.STATE_PATH = tmp / "state.json"
    config.CONFIG_PATH = tmp / "config.json"
    state.PROJECT_ROOT = tmp  # appdata/tts, appdata/stt, chime… all land here

    # ---- config defaults (Phase 3: settings live in config, not code) ------
    cfg = config.load()
    assert not config.CONFIG_PATH.exists(), "load() must not write; save() does"
    assert cfg["voice"]["voice"] == "bm_george", "the default voice is bm_george, by name"
    assert cfg["voice"]["enabled"] is True and cfg["voice"]["speed"] == 1.0
    assert cfg["assistant"]["name"] == "Jarvis"
    assert config.model_dir(cfg).name == "kokoro-multi-lang-v1_0", "default model is the project's own"

    # Phase 4: the speech-input section — defaults, and healing of a bad shape.
    assert cfg["stt"] == {"model_dir": "", "source": None, "language": "en"}
    assert config.stt_model_dir(cfg).name == "sense-voice", "default STT model is the project's own"

    # Phase 5: the wake-word section — off by default, same healing discipline.
    assert cfg["wake"] == {
        "enabled": False,
        "threshold": 0.5,
        "model_dir": "",
        "silence_seconds": 1.0,
        "speech_margin_db": 8.0,
    }
    assert config.wake_model_dir(cfg).name == "openwakeword", "default wake model is the project's own"
    config.CONFIG_PATH.write_text(
        json.dumps(
            {"stt": {"source": 42, "language": 17}, "voice": "nonsense", "wake": {"enabled": "yes", "threshold": 12}}
        ),
        encoding="utf-8",
    )
    healed = config.load()
    assert healed["stt"]["source"] is None, "a non-string mic device heals to Automatic"
    assert healed["stt"]["language"] == "en", "a non-string language heals to the default"
    assert healed["voice"]["voice"] == "bm_george", "a broken voice section heals too"
    assert healed["wake"]["enabled"] is False, "a non-bool wake toggle heals to off"
    assert healed["wake"]["threshold"] == 0.9, "an out-of-range trigger clamps to 0.9"
    config.CONFIG_PATH.write_text(json.dumps({"wake": {"threshold": "abc"}}), encoding="utf-8")
    assert config.load()["wake"]["threshold"] == 0.5, "a non-numeric trigger heals to 0.5"
    config.CONFIG_PATH.write_text(
        json.dumps({"wake": {"silence_seconds": 99, "speech_margin_db": "loud"}}),
        encoding="utf-8",
    )
    healed = config.load()
    assert healed["wake"]["silence_seconds"] == 3.0, "an absurd hang time clamps to 3 s"
    assert healed["wake"]["speech_margin_db"] == 8.0, "a non-numeric margin heals to 8 dB"

    # Phase 6: the read-only tools section — on by default, healed like the rest.
    assert cfg["tools"] == {
        "enabled": True,
        "place": "Frankfort, Kentucky",
        "unit": "fahrenheit",
    }, "tools default to on, Frankfort, Fahrenheit"
    config.CONFIG_PATH.write_text(
        json.dumps({"tools": {"enabled": 3, "place": 7, "unit": "Kelvin"}}), encoding="utf-8"
    )
    healed = config.load()
    assert healed["tools"] == {
        "enabled": True,
        "place": "Frankfort, Kentucky",
        "unit": "fahrenheit",
    }, "a broken tools section heals to stock"
    config.CONFIG_PATH.write_text(
        json.dumps({"tools": {"enabled": False, "unit": "Celsius"}}), encoding="utf-8"
    )
    healed = config.load()
    assert healed["tools"]["enabled"] is False and healed["tools"]["unit"] == "celsius", (
        "a deliberate off + Celsius survives, case-folded where needed"
    )

    cfg["voice"]["enabled"] = False  # no model preload in a UI smoke test
    config.save(cfg)
    assert config.CONFIG_PATH.exists(), "save() must create the file"

    # The wake listener is stubbed for the whole run: no model load, no mic.
    wake.WakeListener = FakeWakeListener

    app = QApplication([])
    window = JarvisWindow()
    window.show()
    # The suite drives model health by hand (window._on_health stubs below).
    # The window's real poller would otherwise overwrite those states with
    # this machine's actual ones mid-run - its first cycle can be queued
    # behind heavy model loads and land minutes later, making verdicts
    # depend on which models happen to be up (the wake-section send assert
    # raced exactly that). Hermetic beats lucky: unhook + stop the poller.
    window.health.updated.disconnect(window._on_health)
    window.health.stop()

    # ---- branding comes from config ----------------------------------------
    assert window.windowTitle() == "Jarvis"
    assert window.title_label.text() == "Jarvis"
    assert window.idle._greeting.text() == "How can I help?"

    # The wake word is off by default: no badge, and the stub listener is cold.
    assert window.wake_pill.isHidden(), "no wake indicator when the wake word is off"
    assert isinstance(window.wake, FakeWakeListener) and not window.wake.running
    assert window.wake.threshold == 0.5

    # ---- the voice catalog is data-driven from the installed model ---------
    catalog = window.speaker.catalog()
    assert catalog, "the installed model must yield a catalog (ONNX metadata)"
    names = [voice.name for voice in catalog]
    assert len(names) == len(set(names)), "voice names must be unique"
    assert len(catalog) == 54, f"kokoro-multi-lang-v1_0 has 54 voices, got {len(catalog)}"
    assert names[0] == "af_alloy" and "bm_george" in names
    george = catalog[names.index("bm_george")]
    assert george.label == "UK male", "labels are inferred from the Kokoro naming scheme"

    # ---- audio sinks: the auto-pick follows the system default (Phase 4) ---
    default_sink = audio.system_default_sink_name()
    picked = audio.auto_pick()
    if default_sink:
        assert picked is not None and picked.name == default_sink, (
            "auto-pick must follow the system default output (whatever path the user hears)"
        )

    # The installed SenseVoice model is found, and the capture argv is built per device.
    assert window.stt_engine.files_present(), "the SenseVoice model is installed in models/"
    argv = stt.capture_argv("mic-x")
    assert argv[0] == "parec" and "--device=mic-x" in argv
    assert "--latency-msec=100" in argv, "the 100 ms latency pin keeps the tail (Phase 4)"
    auto_argv = stt.capture_argv(None)
    assert not any(str(a).startswith("--device") for a in auto_argv), (
        "Automatic records from the system default source (no --device pin)"
    )

    # Offline state (the normal state right now — models stay off unless started).
    window._on_health({m.key: "off" for m in models.MODELS})
    assert not window.send_btn.isEnabled(), "send must be disabled with no model live"
    assert not window.notice_wrap.isHidden(), "the 'no model' notice must show"

    # No-model chip click stages the text instead of losing it.
    window._on_chip("Set a 10-minute timer")
    assert window.composer.toPlainText() == "Set a 10-minute timer"
    window.composer.clear()

    # Render a session: user chip, full assistant answer, streaming bubble, notice.
    window.messages = [{"role": "user", "content": "Hello there"}]
    window._render_session()
    window.transcript.add_assistant("Hi! I'm Jarvis, running locally. How can I help?")
    window.transcript.begin_assistant()
    window._on_delta("Streaming pi")
    window._on_delta("eces arrive one by one.")
    window._on_finished("Streaming pieces arrive one by one.")
    assert window.messages[-1]["content"] == "Streaming pieces arrive one by one."
    window.transcript.add_notice("A test notice line.")

    # A live model enables sending.
    window._select_model("buster-ai")
    window._on_health({**{m.key: "off" for m in models.MODELS}, "buster-ai": "running"})
    assert window.send_btn.isEnabled(), "send must be enabled with a live selected model"
    assert window.notice_wrap.isHidden(), "notice must hide when the selected model is live"

    # Models panel: opens on demand, mirrors systemd states, locks while busy.
    assert window.panel_wrap.isHidden(), "panel starts hidden"
    window.models_btn.setChecked(True)  # opens it (the poller only reads states)
    assert not window.panel_wrap.isHidden(), "panel shows when toggled on"
    window.panel.set_states(
        {"buster-ai": "active", "buster-14b": "failed"}
    )
    rows = window.panel.rows
    assert not rows["buster-ai"].start_btn.isEnabled() and rows["buster-ai"].stop_btn.isEnabled()
    assert rows["buster-14b"].start_btn.isEnabled() and rows["buster-14b"].stop_btn.isEnabled()
    window.panel.set_busy(True, "Starting Qwen3-8B…")
    assert all(
        not row.start_btn.isEnabled() and not row.stop_btn.isEnabled()
        for row in rows.values()
    ), "every control locks while an operation runs"
    window.panel.set_busy(False, "Qwen3-8B is starting.")
    assert rows["buster-14b"].start_btn.isEnabled(), "controls come back after the operation"
    window.models_btn.setChecked(False)
    assert window.panel_wrap.isHidden(), "panel hides when toggled off"

    # ---- Settings dialog: data-driven voices, name-based persistence -------
    window._open_settings()
    dialog = window._settings_dialog
    assert dialog is not None and dialog.isVisible(), "Settings opens"
    from PyQt6.QtWidgets import QScrollArea

    assert dialog.findChild(QScrollArea) is not None, "settings cards scroll (short screens)"
    assert dialog.voice_combo.count() == 54, "every voice of the installed model is listed"
    assert dialog.current_voice() == "bm_george", "the configured voice is pre-selected"
    assert dialog.current_speed() == 1.0
    assert dialog.current_sink_name() is None, "default output is Automatic"
    assert dialog.sink_combo.itemData(0) == "" and "Automatic" in dialog.sink_combo.itemText(0)
    assert dialog.current_source_name() is None, "default input is Automatic"
    assert dialog.source_combo.itemData(0) == "" and "Automatic" in dialog.source_combo.itemText(0)
    source_data = [str(dialog.source_combo.itemData(i)) for i in range(dialog.source_combo.count())]
    assert all(".monitor" not in name for name in source_data), (
        "monitor sources capture output, not a microphone — they are not offered"
    )

    # Picking a microphone persists BY NAME; Automatic clears it back.
    fifine = "alsa_input.usb-3142_Fifine_Microphone-00.mono-fallback"
    index = dialog.source_combo.findData(fifine)
    if index >= 0:  # environment-dependent: this is Brandon's actual box
        dialog.source_combo.setCurrentIndex(index)
        assert window.config["stt"]["source"] == fifine
        assert json.loads(config.CONFIG_PATH.read_text())["stt"]["source"] == fifine
    dialog.source_combo.setCurrentIndex(0)
    assert window.config["stt"]["source"] is None, "Automatic clears the pin"

    # Phase 5: the wake-word card — off by default, and every control persists.
    assert not dialog.wake_check.isChecked(), "the wake word is off by default"
    assert "No wakes logged" in dialog.wake_count.text()
    dialog.wake_check.setChecked(True)
    assert window.config["wake"]["enabled"] is True
    assert json.loads(config.CONFIG_PATH.read_text())["wake"]["enabled"] is True
    assert window.wake.running and not window.wake.paused, "the (stub) listener starts"
    assert not window.wake_pill.isHidden() and "Listening for" in window.wake_pill.text()
    dialog.wake_slider.setValue(65)
    assert abs(window.config["wake"]["threshold"] - 0.65) < 1e-6
    assert abs(window.wake.threshold - 0.65) < 1e-6, "the listener receives the new trigger"
    assert json.loads(config.CONFIG_PATH.read_text())["wake"]["threshold"] == 0.65

    # Phase 5.1: the room-relative take controls — sane defaults, and they heal
    # into the config like everything else (tuned without touching code).
    assert dialog.wake_silence.value() == 100, "hang time defaults to 1.00 s"
    assert dialog.wake_margin.value() == 8, "speech margin defaults to +8 dB"
    dialog.wake_silence.setValue(150)
    assert abs(window.config["wake"]["silence_seconds"] - 1.5) < 1e-6
    assert json.loads(config.CONFIG_PATH.read_text())["wake"]["silence_seconds"] == 1.5
    dialog.wake_margin.setValue(10)
    assert abs(window.config["wake"]["speech_margin_db"] - 10.0) < 1e-6
    assert json.loads(config.CONFIG_PATH.read_text())["wake"]["speech_margin_db"] == 10.0

    # Phase 6: the tools card — master toggle on by default; place + unit persist.
    assert dialog.tools_check.isChecked(), "tools default on (Brandon asked for them)"
    assert dialog.tools_place.text() == "Frankfort, Kentucky"
    assert dialog.tools_unit.currentData() == "fahrenheit"
    dialog.tools_place.setText("Lexington, Kentucky")
    dialog.tools_place.editingFinished.emit()
    assert window.config["tools"]["place"] == "Lexington, Kentucky"
    assert json.loads(config.CONFIG_PATH.read_text())["tools"]["place"] == "Lexington, Kentucky"
    dialog.tools_unit.setCurrentIndex(dialog.tools_unit.findData("celsius"))
    assert window.config["tools"]["unit"] == "celsius"
    assert json.loads(config.CONFIG_PATH.read_text())["tools"]["unit"] == "celsius"
    dialog.tools_place.setText("Frankfort, Kentucky")  # back to stock for the flow tests
    dialog.tools_place.editingFinished.emit()
    dialog.tools_unit.setCurrentIndex(dialog.tools_unit.findData("fahrenheit"))
    assert window.config["tools"] == {
        "enabled": True,
        "place": "Frankfort, Kentucky",
        "unit": "fahrenheit",
    }

    # Picking another voice persists BY NAME and reaches the speaker.
    dialog.voice_combo.setCurrentIndex(dialog.voice_combo.findData("bm_lewis"))
    assert window.config["voice"]["voice"] == "bm_lewis"
    assert window.speaker._voice == "bm_lewis"
    assert json.loads(config.CONFIG_PATH.read_text())["voice"]["voice"] == "bm_lewis"

    # Speed slider persists and reaches the speaker.
    dialog.speed_slider.setValue(150)
    assert abs(window.config["voice"]["speed"] - 1.5) < 1e-6
    assert abs(window.speaker._speed - 1.5) < 1e-6
    dialog.speed_slider.setValue(100)

    # The on/off toggle persists and reaches the speaker. (Stub the preload so
    # this stays a UI test — no 350 MB model load offscreen.)
    window._preload_speech = lambda: None
    dialog.enabled_check.setChecked(True)
    assert window.config["voice"]["enabled"] is True and window.speaker._enabled is True
    dialog.enabled_check.setChecked(False)
    assert window.config["voice"]["enabled"] is False and window.speaker._enabled is False

    # A voice the installed model does NOT have falls back to its first voice.
    window.config["voice"]["voice"] = "does_not_exist"
    config.save(window.config)
    window._open_settings()  # reload from config + catalog
    assert dialog.current_voice() == "af_alloy", "missing voice falls back to the first installed one"
    assert window.config["voice"]["voice"] == "af_alloy", "the fallback is persisted"

    # The catalog-refresh path (fires when the model finishes loading) keeps picks.
    dialog.voice_combo.setCurrentIndex(dialog.voice_combo.findData("bm_george"))
    assert window.config["voice"]["voice"] == "bm_george"
    window._on_catalog_changed()
    assert dialog.current_voice() == "bm_george", "a catalog refresh keeps the current pick"

    # The Settings 'Test sound' chime can be prepared (playback is not exercised offscreen).
    chime = tmp / "chime.wav"
    assert audio.ensure_chime(chime) and chime.exists(), "the test chime writes"

    # ---- push-to-talk wiring (recorder + transcription stubbed; no mic, no
    # model load offscreen — nothing audible or microphone-shaped is faked) --
    class FakeRecorder:
        """Records the calls; `stop()` returns a real silent WAV."""

        def __init__(self, path: Path) -> None:
            self._path = path
            self.recording = False
            self.started_with = "not started"

        def start(self, source):
            self.recording = True
            self.started_with = source
            return True

        def stop(self):
            self.recording = False
            return self._path

    long_wav = _write_silent_wav(tmp / "fake_input.wav", 1.0)
    window.recorder = FakeRecorder(long_wav)
    window._warm_stt = lambda: None  # never load a 240 MB model in a UI smoke test
    transcribed: list[Path] = []
    window._transcribe_file = lambda path: transcribed.append(path)

    window._on_health({m.key: "off" for m in models.MODELS})  # no model live
    window._on_mic_pressed()
    assert window._mic_state == "recording"
    assert window.idle.mic.recording and window.talk_btn.recording, "both mics show recording"
    assert "Listening" in window.idle.sub.text()
    assert window.recorder.started_with is None or isinstance(window.recorder.started_with, str)

    window._on_mic_released()
    assert window._mic_state == "transcribing"
    assert window.idle.mic.busy and window.talk_btn.busy, "both mics show transcribing"
    assert transcribed == [long_wav], "the release hands the WAV to the worker"

    # No model live: the transcript is staged in the box, never lost.
    window._on_stt_finished("hello from the microphone")
    assert window._mic_state == "idle"
    assert not window.idle.mic.busy and not window.talk_btn.busy
    assert "release to send" in window.idle.sub.text(), "the caption returns to idle"
    assert window.composer.toPlainText() == "hello from the microphone"
    assert "No model is running" in window.statusBar().currentMessage()
    window.composer.clear()

    # With a live model the transcript goes out exactly like typed words.
    window._on_health({**{m.key: "off" for m in models.MODELS}, "buster-ai": "running"})
    sent: list[str] = []
    real_send = window.send_message
    window.send_message = lambda t: sent.append(t)  # capture instead of hitting HTTP
    window._on_stt_finished("turn on the kitchen lights")
    assert sent == ["turn on the kitchen lights"]
    window.send_message = real_send

    # An empty transcript is reported — never sent, never invented.
    window._on_stt_finished("   ")
    assert window.composer.toPlainText() == ""
    assert "catch" in window.statusBar().currentMessage()

    # A failure is reported honestly.
    window._on_stt_failed("Transcription failed (RuntimeError).")
    assert "Transcription failed" in window.statusBar().currentMessage()

    # A too-short hold is "didn't catch that", not a transcription.
    short_holder = FakeRecorder(_write_silent_wav(tmp / "short.wav", 0.05))
    window.recorder = short_holder
    calls: list[Path] = []
    window._transcribe_file = lambda path: calls.append(path)
    window._on_mic_pressed()
    window._on_mic_released()
    assert calls == [] and window._mic_state == "idle"
    assert "catch" in window.statusBar().currentMessage()

    # ---- wake word: verify → chime → room-relative take → transcribe → send --
    class FakeWakeRecorder:
        """Fakes the wake take (a fake, not a wake): records that the recorder's
        stop-on-silence mode was requested, its floor seed, and how the take
        'ended'."""

        def __init__(self, path: Path) -> None:
            self._path = path
            self.recording = False
            self.started_with = "not started"
            self.auto_stop = None
            self.on_auto_stop = None
            self.floor_db = None
            self.last_reason = "silence"
            self.last_heard_speech = True

        def start(self, source, auto_stop=None, on_auto_stop=None, floor_db=None):
            self.recording = True
            self.started_with = source
            self.auto_stop = auto_stop
            self.on_auto_stop = on_auto_stop
            self.floor_db = floor_db
            return True

        def stop(self):
            self.recording = False
            return self._path

    window._wake_delay_ms = 10        # the shipped delay lets the chime finish
    window._wake_after_speech_ms = 40
    window._wake_after_wake_ms = 40
    window._wake_suppress_until = 0.0
    chimes: list[int] = []
    window._play_wake_chime = lambda: chimes.append(1)  # offscreen: never audible
    wake_wav = _write_silent_wav(tmp / "fake_wake_input.wav", 1.2)
    wake_recorder = FakeWakeRecorder(wake_wav)
    window.recorder = wake_recorder
    transcribed.clear()
    window._transcribe_file = lambda path: transcribed.append(path)
    sent.clear()
    real_send = window.send_message
    window.send_message = lambda t: sent.append(t)

    # Phase 5.1: a candidate is verified by transcribing the moment. The real
    # engine is never loaded offscreen — the verdict is injected here; detection
    # and verification are exercised for real in tests/wake_check.py and on
    # Brandon's mic. No chime/recording may happen before an accepted verdict.
    window._start_verification = lambda score, wav: window.wake_verified.emit(
        score, True, "hey jarvis", ""
    )
    window.wake.noise_floor_db = -18.6  # the (stub) listener's room estimate
    window._on_wake(0.83, str(wake_wav))
    assert chimes == [1], "a verified wake plays the chime"
    assert window._wake_recording, "a verified wake starts a recording"
    QTest.qWait(60)  # the chime-delay singleShot, then the take
    assert wake_recorder.started_with, "the take opened the resolved source"
    assert wake_recorder.auto_stop is not None, "stop-on-silence mode is requested"
    assert abs(wake_recorder.auto_stop.silence_seconds - 1.5) < 1e-6, (
        "the Settings hang time reaches the take"
    )
    assert abs(wake_recorder.auto_stop.margin_db - 10.0) < 1e-6, (
        "the Settings speech margin reaches the take"
    )
    assert wake_recorder.floor_db == -18.6, "the listener's room floor seeds the VAD"
    assert window._mic_state == "recording", "the recording state is shown"
    assert window.wake.paused, "no detection while a recording is in flight"
    logged = [
        json.loads(line)
        for line in (tmp / "appdata" / "wake_log.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(logged) == 1, "every candidate is logged"
    assert logged[0]["score"] == 0.83 and logged[0]["accepted"] is True
    assert logged[0]["text"] == "hey jarvis", "the verification transcript is kept"

    # End of speech (the recorder's auto-stop) → the same path as push-to-talk.
    window._on_wake_auto_stop()
    assert window._mic_state == "transcribing"
    assert transcribed and transcribed[-1] == wake_wav
    assert not window._wake_recording
    # This section asserts the wake -> transcript -> send path itself. The
    # Phase 6 router legitimately diverts a weather-shaped transcript into an
    # async lookup (thread + queued signal), which would make the sync assert
    # below a race — so tools are off for this one send and restored after;
    # the tool path has its own section further down.
    window.config["tools"]["enabled"] = False
    window._on_stt_finished("what's the weather like")
    assert sent == ["what's the weather like"], "the transcript is sent like typed words"
    window.config["tools"]["enabled"] = True

    QTest.qWait(140)  # the cool-down above elapses
    assert window.wake.running and not window.wake.paused, "the listener re-arms after the cool-down"
    assert "Listening for" in window.wake_pill.text()

    # A TV lookalike (the transcript doesn't contain the name): rejected
    # silently — no chime, no recording — but kept in the log with its text.
    reject_wav = _write_silent_wav(tmp / "fake_reject_input.wav", 2.4)
    window._start_verification = lambda score, wav: window.wake_verified.emit(
        score, False, "hey, Bart's.", "no-wake-word"
    )
    chimes.clear()
    transcribed.clear()
    window._on_wake(0.94, str(reject_wav))
    assert chimes == [], "a rejected candidate never plays the chime"
    assert not window._wake_recording and not wake_recorder.recording, (
        "a rejected candidate never starts a recording"
    )
    logged = [
        json.loads(line)
        for line in (tmp / "appdata" / "wake_log.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert logged[-1]["score"] == 0.94 and logged[-1]["accepted"] is False
    assert logged[-1]["text"] == "hey, Bart's." and logged[-1]["reason"] == "no-wake-word"
    assert transcribed == []
    assert "rejected" in window.statusBar().currentMessage().lower()
    QTest.qWait(140)
    assert not window.wake.paused, "the listener re-arms after a rejected candidate"

    # While verification is in flight the listener stays honestly paused; the
    # verdict then decides. (The real flow hops back from a worker thread.)
    window._start_verification = lambda score, wav: None  # no verdict yet
    inflight_wav = _write_silent_wav(tmp / "fake_inflight_input.wav", 1.0)
    window._on_wake(0.95, str(inflight_wav))
    assert window._verifying and window.wake.paused
    assert "paused" in window.wake_pill.text().lower(), "the badge tells the truth mid-check"
    assert not window._wake_recording, "no recording before the verdict"
    chimes.clear()
    window.wake.noise_floor_db = -21.0
    window.wake_verified.emit(0.95, True, "hey jarvis", "")
    assert not window._verifying and window._wake_recording
    assert chimes == [1]
    QTest.qWait(60)
    assert wake_recorder.floor_db == -21.0, "the floor seed follows the listener"
    window._cancel_recording()  # clean up this take without transcribing
    window.wake.noise_floor_db = None
    QTest.qWait(140)
    assert not window.wake.paused

    # While Jarvis speaks — and for the quiet tail after — detection is paused.
    window._on_speech_busy(True)
    assert window.wake.paused, "no detection while Jarvis speaks"
    assert "paused" in window.wake_pill.text().lower()
    window._on_speech_busy(False)
    assert window.wake.paused, "still paused during the tail after the last sentence"
    QTest.qWait(140)
    assert not window.wake.paused, "re-armed after the quiet tail"

    # Push-to-talk also suppresses detection while it records.
    window.recorder = FakeWakeRecorder(_write_silent_wav(tmp / "fake_ptt.wav", 1.0))
    window._on_mic_pressed()
    assert window.wake.paused, "no detection while push-to-talk records"
    window._on_mic_released()
    window._on_stt_finished("")  # empty transcript: honest notice, back to idle
    assert window._mic_state == "idle"

    # Esc (or the mic button taking over) drops a wake take without transcribing.
    window.recorder = FakeWakeRecorder(_write_silent_wav(tmp / "fake_cancel.wav", 1.2))
    cancel_wav = tmp / "fake_cancel.wav"
    transcribed.clear()
    window._start_verification = lambda score, wav: window.wake_verified.emit(
        score, True, "hey jarvis", ""
    )
    window._on_wake(0.91, str(cancel_wav))
    QTest.qWait(60)
    assert window._wake_recording and window.recorder.recording
    window._cancel_recording()
    assert not window._wake_recording and window._mic_state == "idle"
    assert not cancel_wav.exists(), "a cancelled take is discarded"
    assert transcribed == [], "a cancelled take is never transcribed"
    assert "cancel" in window.statusBar().currentMessage().lower()

    # A configured microphone that is gone: honest error, no silent switch.
    window._wake_suppress_until = 0.0
    window.config["stt"]["source"] = "definitely_not_a_mic"
    window._sync_wake()
    assert "definitely_not_a_mic" in window._wake_error, "the missing mic is named"
    assert not window.wake.running, "the listener stops — it never moves to another mic"
    assert window._wake_retry_timer.isActive(), "it keeps trying — the mic may come back"
    assert "unavailable" in window.wake_pill.text().lower()
    window.config["stt"]["source"] = None
    window._on_wake_retry()
    assert window.wake.running, "once the source resolves again the listener restarts"

    # The Settings count reads the log honestly: three accepted wakes, one
    # rejected candidate — counted separately, never added up into a lie.
    window._open_settings()
    assert "3 wakes logged (1 candidate rejected)" in dialog.wake_count.text()

    # Turning the wake word off: the indicator vanishes and the listener stops.
    dialog.wake_check.setChecked(False)
    assert window.config["wake"]["enabled"] is False
    assert window.wake_pill.isHidden(), "indicator gone when the wake word is off"
    assert not window.wake.running and not window.wake.paused, "the mic is cold again"
    window.send_message = real_send

    # ---- Phase 6: read-only tools — router → lookup → note → injection ------
    # The real router decides; the lookup result is injected through the
    # `_run_tool` seam (the real Open-Meteo calls are measured in
    # tests/tools_check.py). The LLM is a capture stub: this suite must never
    # hit an HTTP model.
    class FakeLlm(QObject):
        delta = pyqtSignal(str)
        finished = pyqtSignal(str)
        failed = pyqtSignal(str)

        def __init__(self):
            super().__init__()
            self.requests: list[list[dict]] = []

        def start(self, port, messages, temperature=0.7, max_tokens=512):
            self.requests.append([dict(m) for m in messages])
            return True

    real_llm = window.llm
    fake_llm = FakeLlm()
    window.llm = fake_llm

    notes: list[str] = []
    real_note = window.transcript.add_tool_note

    def spy_note(text: str) -> None:
        notes.append(text)
        real_note(text)

    window.transcript.add_tool_note = spy_note
    real_run_tool = window._run_tool
    real_start_tool = window._start_tool
    started: list[tools.ToolCall] = []

    def dispatch(call: tools.ToolCall) -> None:
        started.append(call)
        window._run_tool(call, window._next_tool_seq())

    def wire_runner(result: tools.ToolResult):
        def run(call: tools.ToolCall, seq: int) -> None:
            window.tool_finished.emit((seq, result))
        return run

    canned = tools.ToolResult(
        tool="weather",
        ok=True,
        place="Frankfort, Kentucky",
        spoken="It's 44 degrees and clear in Frankfort. High 71, low 37, no rain expected.",
        note="🌦 Weather checked for Frankfort, Kentucky — 44°F, clear",
        context=(
            "Tool result (weather, Frankfort, Kentucky): It's 44 degrees and clear in "
            "Frankfort. High 71, low 37, no rain expected.\n"
            "That is real, current data from Open-Meteo."
        ),
    )
    window._run_tool = wire_runner(canned)
    window._start_tool = dispatch

    # Weather with a place named in the question — the real router extracts it.
    window.send_message("what's the temperature in Lexington?")
    assert started and started[0].tool == "weather" and started[0].place == "Lexington"
    assert window._busy, "the reply is owed until the model stream finishes"
    assert notes[-1] == canned.note, "the tool note shows in the chat"
    assert "Weather checked" in window.statusBar().currentMessage()
    msgs = fake_llm.requests[-1]
    assert msgs[0]["role"] == "system" and "read-only local lookups" in msgs[0]["content"], (
        "tools on: the prompt describes them"
    )
    assert msgs[-1]["role"] == "user" and "Lexington" in msgs[-1]["content"]
    assert msgs[-2]["role"] == "system" and msgs[-2]["content"].startswith("Tool result (weather"), (
        "the result block sits right ahead of the user's message"
    )
    assert window.messages[-1]["role"] == "user", (
        "the tool block never enters the saved conversation"
    )
    log_path = window._tools_log_path()
    assert str(log_path).startswith(str(tmp)), "the tools log lives in the isolated appdata"
    logged = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
    assert logged["tool"] == "weather" and logged["ok"] is True
    assert logged["place"] == "Frankfort, Kentucky"
    window._on_finished("It's 44 and clear in Lexington.")
    assert not window._busy

    # Ordinary chat: no lookup, no note, no injected block — behavior unchanged.
    before_started, before_notes = len(started), len(notes)
    window.send_message("tell me a joke")
    assert len(started) == before_started, "no tool fires for ordinary chat"
    assert len(notes) == before_notes, "no tool note for ordinary chat"
    msgs = fake_llm.requests[-1]
    assert all("Tool result (" not in m["content"] for m in msgs), "no injected block"
    window._on_finished("Why did the chicken cross the road?")

    # A nameless weather question runs the lookup with the configured default place.
    window.send_message("what's the weather?")
    assert started[-1].tool == "weather" and started[-1].place == "", "no place named → default"
    window._on_finished("It's 44 and clear in Frankfort.")

    # A failed lookup is honest in the note and in the injected context.
    failed = tools.failure_result(
        "weather", "Open-Meteo didn't answer in time", place="Frankfort, Kentucky"
    )
    window._run_tool = wire_runner(failed)
    window.send_message("is it raining?")
    assert "failed" in notes[-1] and "didn't answer in time" in notes[-1]
    msgs = fake_llm.requests[-1]
    assert msgs[-2]["content"].startswith("Tool result (weather") and "FAILED" in msgs[-2]["content"]
    assert "Do not guess" in msgs[-2]["content"]
    window._on_finished("I couldn't get the weather that time — try again in a bit.")

    # The clock runs the real (networkless) result — deterministic machinery.
    window._run_tool = real_run_tool
    window.send_message("what time is it?")
    assert started[-1].tool == "clock"
    assert "Clock checked" in notes[-1]
    msgs = fake_llm.requests[-1]
    assert msgs[-2]["content"].startswith("Tool result (clock)")
    window._on_finished("It's about quarter to ten.")

    # Tools off must mean off: the router never runs and the prompt is Phase 5.1.
    dialog.tools_check.setChecked(False)
    assert window.config["tools"]["enabled"] is False
    before_started = len(started)
    window.send_message("what's the weather in Paris?")
    assert len(started) == before_started, "router inert while tools are off"
    msgs = fake_llm.requests[-1]
    assert all("Tool result (" not in m["content"] for m in msgs), "no injected block"
    assert "no tools on this desktop yet" in msgs[0]["content"], "off is the Phase 5.1 prompt"
    window._on_finished("I can't check live weather yet, I'm afraid.")
    dialog.tools_check.setChecked(True)
    assert window.config["tools"]["enabled"] is True

    # A stale lookup (from an earlier tool sequence) is dropped, never lands.
    stale_seq = window._tool_seq
    window.new_session()
    before_notes = len(notes)
    window._on_tool_finished((stale_seq - 1, canned))
    assert len(notes) == before_notes, "a stale tool result is dropped"

    window.transcript.add_tool_note = real_note
    window._run_tool = real_run_tool
    window._start_tool = real_start_tool
    window.llm = real_llm

    # ---- tray: the X hides to the tray (24/7); only a real quit tears down --
    from PyQt6.QtGui import QCloseEvent

    class FakeTray:
        def __init__(self):
            self.messages = []

        def showMessage(self, title, text, icon, ms):  # noqa: N802
            self.messages.append((title, text))

    assert window._really_quit is False, "a plain close is not a quit"
    fake_tray = FakeTray()
    window._tray = fake_tray
    close_ev = QCloseEvent()
    window.closeEvent(close_ev)
    assert not close_ev.isAccepted(), "with a tray, the X is absorbed"
    assert window.isHidden(), "the window hides to the tray"
    assert len(fake_tray.messages) == 1, "one 'still running' tip, the first time"
    window.show()
    close_ev = QCloseEvent()
    window.closeEvent(close_ev)
    assert not close_ev.isAccepted() and len(fake_tray.messages) == 1, "no repeat tip"
    window.show()
    window._really_quit = True
    close_ev = QCloseEvent()
    window.closeEvent(close_ev)
    assert close_ev.isAccepted(), "a real quit runs the teardown and closes"
    window._really_quit = False

    # Quit from the tray while hidden: close() alone is a no-op on a hidden
    # window (the bug Brandon hit) - the tray Quit must exit the app anyway.
    quit_calls: list[int] = []
    real_app_quit = QApplication.quit
    QApplication.quit = lambda self=None: quit_calls.append(1)
    try:
        window.hide()
        window._quit_from_tray()
    finally:
        QApplication.quit = real_app_quit
    assert quit_calls == [1], "tray Quit invokes application quit even from the tray"
    assert not window.isVisible(), "and the window is gone, not just tucked away"
    assert window._really_quit is True
    window._really_quit = False
    window._tray = None
    window.show()

    # New session clears back to idle home and stops any speech.
    window.new_session()
    assert window.messages == []
    assert window.stack.currentIndex() == 0

    QTimer.singleShot(400, app.quit)
    code = app.exec()
    print("smoke ok")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
