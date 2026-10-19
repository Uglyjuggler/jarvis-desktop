"""The main window: Portal header (orb, name, model status), the idle home /
transcript switch, the model picker, the chat input — plus spoken replies
(Kokoro voice), push-to-talk speech input (local SenseVoice transcription), and
the opt-in wake word (openWakeWord; every candidate is verified by transcribing
the moment before it counts), all configured in the Settings dialog."""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QIcon, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QMenu,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .. import audio, config, state, stt, tools, wake
from ..health import HealthPoller
from ..llm import LlmWorker
from ..models import BY_KEY, MODELS
from ..prompt import build_system_prompt
from ..switcher import Switcher, UnitPoller
from ..tts import DEFAULT_VOICE, KokoroEngine, Speaker
from . import theme
from .home import IdleHome
from .panel import ModelsPanel
from .settings import SettingsDialog
from .transcript import TranscriptView
from .widgets import Composer, GlowRoot, MicButton, dot_icon, make_orb_pixmap

HISTORY_CHAR_CAP = 24_000  # keep requests small and fast; the models' contexts are 16–32K tokens
RECORD_MAX_MS = 60_000     # a held mic never records longer than this
WAKE_RECORD_DELAY_MS = 650  # the chime plays (~0.5 s), then recording starts
WAKE_AFTER_SPEECH_MS = 2_500  # quiet time after Jarvis speaks — he must not hear himself
WAKE_AFTER_WAKE_MS = 1_500    # cool-down after a wake before the listener re-arms
WAKE_RETRY_MS = 5_000         # when the listener can't run, try again this often


class JarvisWindow(QMainWindow):
    wake_recording_auto_stop = pyqtSignal()  # queued from the recorder's reader thread
    wake_verified = pyqtSignal(float, bool, str, str)  # (score, accepted, text, reason)
    tool_finished = pyqtSignal(object)  # (seq, tools.ToolResult) from the lookup thread

    def __init__(self) -> None:
        super().__init__()
        self.config = config.load()
        self._name = self.config["assistant"]["name"]
        self.setWindowTitle(self._name)
        self.setWindowIcon(QIcon(make_orb_pixmap(64)))

        saved = state.load()
        self.messages: list[dict] = saved["messages"]
        self.model = BY_KEY.get(saved.get("model"), MODELS[0])
        width, height = saved.get("window", [1080, 760])
        self.resize(int(width), int(height))
        self.setMinimumSize(880, 620)

        self.statuses: dict[str, str] = {m.key: "off" for m in MODELS}
        self._busy = False
        self._streaming_text = ""
        self._auto_picked = False
        self._pending_op: tuple[str, object] | None = None
        self._settings_dialog: SettingsDialog | None = None
        self._speech_preloaded = False
        self._tool_seq = 0  # bumps on every send/new session: stale lookups are dropped
        self._really_quit = False  # only the tray's Quit (or no tray) truly exits
        self._tray: QSystemTrayIcon | None = None
        self._tray_tip_shown = False

        # The voice: same local Kokoro model as the rest of the house uses —
        # the one this project downloaded into models/ (config can point
        # elsewhere; nothing here needs code changes to swap models).
        self.speaker = Speaker(
            state.PROJECT_ROOT / "appdata" / "tts",
            engine=KokoroEngine(config.model_dir(self.config)),
        )

        # Speech input: press-and-hold recording (parecord) + local SenseVoice
        # transcription on a worker thread — same "model in the project's
        # models/ folder" pattern as the voice.
        self.stt_engine = stt.SttEngine(
            config.stt_model_dir(self.config),
            language=str((self.config.get("stt") or {}).get("language", "en")),
        )
        self.transcriber = stt.TranscribeWorker(self.stt_engine)
        self.recorder = stt.Recorder(state.PROJECT_ROOT / "appdata" / "stt")
        self._stt_warmed = False
        self._mic_state = "idle"
        self._record_watchdog = QTimer(self)
        self._record_watchdog.setSingleShot(True)
        self._record_watchdog.setInterval(RECORD_MAX_MS)
        self._record_watchdog.timeout.connect(self._on_mic_released)

        # Wake word (openWakeWord "hey jarvis"): opt-in, off by default. The
        # listener runs entirely on its own thread and loads the models from the
        # project's own models/openwakeword folder (config can point elsewhere).
        self.wake = wake.WakeListener(
            model_dir=config.wake_model_dir(self.config),
            threshold=float((self.config.get("wake") or {}).get("threshold", 0.5)),
            buffer_dir=state.PROJECT_ROOT / "appdata" / "stt",
        )
        self._wake_recording = False
        self._verifying = False  # a candidate is being transcribed before commitment
        self._wake_error = ""
        self._speech_busy = False
        self._wake_suppress_until = 0.0
        # Instance copies so the smoke tests can shrink the delays without
        # touching the module constants the app ships with.
        self._wake_delay_ms = WAKE_RECORD_DELAY_MS
        self._wake_after_speech_ms = WAKE_AFTER_SPEECH_MS
        self._wake_after_wake_ms = WAKE_AFTER_WAKE_MS
        self._wake_resume_timer = QTimer(self)
        self._wake_resume_timer.setSingleShot(True)
        self._wake_resume_timer.timeout.connect(self._sync_wake)
        self._wake_retry_timer = QTimer(self)
        self._wake_retry_timer.setSingleShot(True)
        self._wake_retry_timer.timeout.connect(self._on_wake_retry)

        self._build_ui()
        self._wire()
        self._render_session()
        self._refresh_status_ui()
        self.health.start()
        self._setup_tray()
        self._apply_voice_config(preload=True)
        self._sync_wake()

    # ---- layout ----------------------------------------------------------

    def _build_ui(self) -> None:
        root = GlowRoot()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_header())

        # The Models panel (built-in Model Switcher) — hidden until asked for.
        self.panel_wrap = QWidget()
        panel_wrap_layout = QHBoxLayout(self.panel_wrap)
        panel_wrap_layout.setContentsMargins(24, 10, 24, 0)
        self.panel = ModelsPanel()
        panel_wrap_layout.addWidget(self.panel)
        self.panel_wrap.hide()
        layout.addWidget(self.panel_wrap)

        self.notice_frame = QFrame()
        self.notice_frame.setObjectName("notice")
        self.notice_label = QLabel()
        self.notice_label.setWordWrap(True)
        self.notice_label.setStyleSheet("color: %s; font-size: 13px;" % theme.NOTICE_TEXT)
        notice_layout = QHBoxLayout(self.notice_frame)
        notice_layout.setContentsMargins(12, 8, 12, 8)
        notice_layout.addWidget(self.notice_label)
        self.notice_wrap = QWidget()
        wrap_layout = QHBoxLayout(self.notice_wrap)
        wrap_layout.setContentsMargins(24, 8, 24, 0)
        wrap_layout.addWidget(self.notice_frame)
        layout.addWidget(self.notice_wrap)

        self.idle = IdleHome(
            greeting=self.config["assistant"]["greeting"],
            assistant=self._name,
        )
        self.transcript = TranscriptView()
        self.stack = QStackedWidget()
        self.stack.addWidget(self.idle)
        self.stack.addWidget(self.transcript)
        layout.addWidget(self.stack, 1)

        layout.addWidget(self._build_input_bar())

    def _build_header(self) -> QWidget:
        header = QWidget()
        layout = QHBoxLayout(header)
        layout.setContentsMargins(22, 14, 22, 10)
        layout.setSpacing(12)

        orb = QLabel()
        orb.setPixmap(make_orb_pixmap(30))
        orb.setFixedSize(30, 30)
        layout.addWidget(orb)

        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.title_label = QLabel(self._name)
        self.title_label.setStyleSheet("font-size: 17px; font-weight: 600;")
        self.subtitle = QLabel("")
        self.subtitle.setStyleSheet("color: %s; font-size: 12px;" % theme.TEXT_SUBTLE)
        titles.addWidget(self.title_label)
        titles.addWidget(self.subtitle)
        layout.addLayout(titles)

        layout.addStretch(1)

        # The wake-word badge: absent when off, and it tells the truth about
        # whether the mic is being listened to whenever it is visible.
        self.wake_pill = QLabel("")
        self.wake_pill.setObjectName("wakepill")
        self.wake_pill.setTextFormat(Qt.TextFormat.RichText)
        self.wake_pill.hide()
        layout.addWidget(self.wake_pill)

        self.picker = QComboBox()
        self.picker.setMinimumWidth(240)
        self.picker.setCursor(Qt.CursorShape.PointingHandCursor)
        for model in MODELS:
            self.picker.addItem(model.name, userData=model.key)
        layout.addWidget(self.picker)

        self.models_btn = QPushButton("Models")
        self.models_btn.setObjectName("ghost")
        self.models_btn.setCheckable(True)
        self.models_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.models_btn)

        self.settings_btn = QPushButton("Settings")
        self.settings_btn.setObjectName("ghost")
        self.settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_btn.setToolTip("Voice, speed, and where Jarvis speaks")
        layout.addWidget(self.settings_btn)

        self.new_btn = QPushButton("New session")
        self.new_btn.setObjectName("ghost")
        self.new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.new_btn)

        return header

    def _build_input_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("inputbar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(18, 12, 18, 14)
        layout.setSpacing(10)

        # Push-to-talk, always within reach — the home-screen mic disappears
        # from view once the conversation starts.
        self.talk_btn = MicButton(size=44)
        self.talk_btn.setToolTip("Hold to talk — release to send")
        layout.addWidget(self.talk_btn)

        self.composer = Composer()
        self.composer.setObjectName("composer")
        self.composer.setPlaceholderText("Type a message…")
        self.composer.setFixedHeight(56)
        layout.addWidget(self.composer, 1)

        self.send_btn = QPushButton("Send")
        self.send_btn.setObjectName("send")
        self.send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_btn.setFixedHeight(56)
        layout.addWidget(self.send_btn)

        return bar

    # ---- wiring ----------------------------------------------------------

    def _wire(self) -> None:
        self.picker.currentIndexChanged.connect(self._on_pick)
        self.new_btn.clicked.connect(self.new_session)
        self.send_btn.clicked.connect(self._on_send_clicked)
        self.composer.submitted.connect(self._on_send_clicked)
        self.idle.chip_clicked.connect(self._on_chip)
        self.idle.mic.pressed.connect(self._on_mic_pressed)
        self.idle.mic.released.connect(self._on_mic_released)
        self.talk_btn.pressed.connect(self._on_mic_pressed)
        self.talk_btn.released.connect(self._on_mic_released)

        self.health = HealthPoller()
        self.health.updated.connect(self._on_health)

        self.llm = LlmWorker()
        self.llm.delta.connect(self._on_delta)
        self.llm.finished.connect(self._on_finished)
        self.llm.failed.connect(self._on_failed)

        # Models panel: user-intent only — nothing here starts by itself.
        self.models_btn.toggled.connect(self._on_models_toggled)
        self.panel.start_requested.connect(self._on_panel_start)
        self.panel.stop_requested.connect(self._on_panel_stop)
        self.unit_poller = UnitPoller()
        self.unit_poller.updated.connect(self.panel.set_states)
        self.switcher = Switcher()
        self.switcher.progress.connect(lambda msg: self.panel.set_busy(True, msg))
        self.switcher.finished.connect(self._on_switch_finished)

        # The voice (spoken replies) and its Settings dialog.
        self.settings_btn.clicked.connect(self._open_settings)
        self.speaker.busy_changed.connect(self._on_speech_busy)
        self.speaker.catalog_changed.connect(self._on_catalog_changed)
        self.speaker.error.connect(self._on_speech_error)

        # Speech input (push-to-talk).
        self.transcriber.finished.connect(self._on_stt_finished)
        self.transcriber.failed.connect(self._on_stt_failed)

        # Wake word (opt-in): the listener lives on its own thread; these
        # signals carry its state to the UI. Esc cancels a recording in flight.
        self.wake.ready.connect(self._on_wake_ready)
        self.wake.wake.connect(self._on_wake)
        self.wake.failed.connect(self._on_wake_failed)
        self.wake_verified.connect(self._on_wake_verified)
        self.wake_recording_auto_stop.connect(self._on_wake_auto_stop)
        cancel_shortcut = QShortcut(QKeySequence("Escape"), self)
        cancel_shortcut.activated.connect(self._cancel_recording)

        # Read-only tools (weather, clock): the lookup runs on its own thread;
        # this signal carries the result back to be logged, shown, and injected.
        self.tool_finished.connect(self._on_tool_finished)

    # ---- state helpers ---------------------------------------------------

    def _persist(self) -> None:
        state.save(self.model.key, self.messages, (self.width(), self.height()))

    def _can_send(self) -> bool:
        return not self._busy and self.statuses.get(self.model.key) == "running"

    def _history_for_request(self) -> list[dict]:
        """Recent turns only — bounded by characters, system prompt always first."""
        picked: list[dict] = []
        total = 0
        for message in reversed(self.messages):
            picked.append(message)
            total += len(message["content"])
            if total > HISTORY_CHAR_CAP:
                break
        return list(reversed(picked))

    # ---- health / model picking -------------------------------------------

    def _on_health(self, statuses: dict) -> None:
        self.statuses = statuses
        self._maybe_auto_pick()
        self._refresh_picker_items()
        self._refresh_status_ui()

    def _maybe_auto_pick(self) -> None:
        """On the first health report: if the saved model isn't live but exactly one
        other is, quietly switch to it (the notice explains why)."""
        if self._auto_picked:
            return
        self._auto_picked = True
        if self.statuses.get(self.model.key) == "running":
            return
        live = [m for m in MODELS if self.statuses.get(m.key) == "running"]
        if len(live) == 1 and live[0].key != self.model.key:
            self._select_model(live[0].key)

    def _refresh_picker_items(self) -> None:
        for index, model in enumerate(MODELS):
            status = self.statuses.get(model.key, "off")
            text = f"{model.name} — {theme.STATUS_WORDS[status]}"
            if self.picker.itemText(index) != text:
                self.picker.setItemText(index, text)
            self.picker.setItemIcon(index, QIcon(dot_icon(status)))
        self.picker.setToolTip(self._picker_tooltip())

    def _picker_tooltip(self) -> str:
        lines = ["Only one model can be running at a time (one graphics card)."]
        for model in MODELS:
            status = self.statuses.get(model.key, "off")
            lines.append(f"{model.name}: {theme.STATUS_WORDS[status]}")
        lines.append("")
        lines.append("Start and stop models in the Models panel (top right).")
        return "\n".join(lines)

    def _refresh_status_ui(self) -> None:
        selected_status = self.statuses.get(self.model.key, "off")
        if self._busy:
            self.subtitle.setText(f"{self.model.name} · Speaking…")
        else:
            self.subtitle.setText(
                f"{self.model.name} · {theme.STATUS_WORDS.get(selected_status, 'Off')}"
            )

        if selected_status == "running":
            self.notice_wrap.hide()
        else:
            self.notice_wrap.show()
            self.notice_label.setText(self._notice_text())

        self._update_send_enabled()

    def _notice_text(self) -> str:
        selected = self.model
        status = self.statuses.get(selected.key, "off")
        if status == "loading":
            return f"{selected.name} is waking up — this can take a minute or two on first load."
        live = [m.name for m in MODELS if self.statuses.get(m.key) == "running"]
        if live:
            return (
                f"{selected.name} is off — {', '.join(live)} is running. "
                f"Pick it in the model menu, or start {selected.name} in the Models panel."
            )
        loading = [m.name for m in MODELS if self.statuses.get(m.key) == "loading"]
        if loading:
            return f"{', '.join(loading)} is waking up — give it a minute."
        return (
            "No model is running. Open the Models panel (top right) and start one — "
            "it joins the chat once it has finished loading."
        )

    def _update_send_enabled(self) -> None:
        self.send_btn.setEnabled(self._can_send())
        self.send_btn.setText("…" if self._busy else "Send")

    def _on_pick(self, index: int) -> None:
        key = self.picker.itemData(index)
        if key and key != self.model.key:
            self.model = BY_KEY[key]
            self._persist()
            self._refresh_status_ui()

    def _select_model(self, key: str) -> None:
        index = next(i for i, m in enumerate(MODELS) if m.key == key)
        self.picker.setCurrentIndex(index)  # fires _on_pick

    # ---- Models panel (the built-in Model Switcher) ------------------------

    def _on_models_toggled(self, is_open: bool) -> None:
        if is_open:
            self.panel_wrap.show()
            if not self.switcher.busy:
                self.panel.set_foot(
                    "Nothing starts by itself — a model runs only when you start it."
                )
            self.unit_poller.start()
        else:
            self.panel_wrap.hide()
            self.unit_poller.stop()

    def _on_panel_start(self, unit: str) -> None:
        model = BY_KEY.get(unit)
        if model is None or self.switcher.busy:
            return
        self._pending_op = ("start", model)
        self.panel.set_busy(True, f"Starting {model.name}…")
        self.switcher.start_model(model)

    def _on_panel_stop(self, unit: str) -> None:
        model = BY_KEY.get(unit)
        if model is None or self.switcher.busy:
            return
        self._pending_op = ("stop", model)
        self.panel.set_busy(True, f"Stopping {model.name}…")
        self.switcher.stop_model(model)

    def _on_switch_finished(self, ok: bool, message: str) -> None:
        self.panel.set_busy(False, message)
        if ok and self._pending_op is not None:
            action, model = self._pending_op
            if action == "start":
                # You started it to use it: point the chat at it right away.
                self._select_model(model.key)
        self._pending_op = None

    # ---- spoken replies (Kokoro) -------------------------------------------

    def _preload_speech(self) -> None:
        """Warm the voice model on a background thread (idempotent)."""
        if self._speech_preloaded:
            return
        self._speech_preloaded = True
        threading.Thread(
            target=self.speaker.preload, name="jarvis-tts-preload", daemon=True
        ).start()

    def _resolved_sink_name(self, wanted: str | None) -> str | None:
        """A concrete sink to play through: the chosen one, else the auto-pick
        (the system default output — whatever path the user actually hears —
        falling back to the physical heuristic)."""
        if wanted:
            return wanted
        picked = audio.auto_pick()
        return picked.name if picked else None

    def _resolved_source_name(self, wanted: str | None) -> str | None:
        """A concrete input source to record from: the chosen one, else the
        system default source ("Automatic")."""
        if wanted:
            return wanted
        picked = audio.auto_source()
        return picked.name if picked else None

    def _apply_voice_config(self, preload: bool = False) -> None:
        voice_cfg = self.config["voice"]
        self.speaker.configure(
            enabled=bool(voice_cfg.get("enabled", True)),
            voice=str(voice_cfg.get("voice") or DEFAULT_VOICE),
            speed=float(voice_cfg.get("speed", 1.0)),
            sink=self._resolved_sink_name(voice_cfg.get("sink")),
        )
        if preload and voice_cfg.get("enabled", True):
            self._preload_speech()

    def _on_speech_busy(self, busy: bool) -> None:
        self._refresh_status_ui()
        self._speech_busy = bool(busy)
        if busy:
            self._wake_resume_timer.stop()
            self._sync_wake()  # paused while George talks: he must not wake himself
        else:
            # A couple of seconds of quiet after the last sentence reaches the
            # mic; then the listener re-arms by itself.
            self._wake_suppress_until = max(
                self._wake_suppress_until,
                time.monotonic() + self._wake_after_speech_ms / 1000.0,
            )
            self._wake_resume_timer.start(self._wake_after_speech_ms + 30)
            self._sync_wake()

    def _on_catalog_changed(self) -> None:
        """The model finished loading: its real voice list is known now."""
        catalog = self.speaker.catalog()
        if self._settings_dialog is not None:
            self._settings_dialog.refresh_voices(catalog, prefer=self.config["voice"]["voice"])
            self._sync_voice_from_dialog()

    def _on_speech_error(self, message: str) -> None:
        self._notify(message)

    def _notify(self, message: str) -> None:
        """Honest, visible status: a transcript notice + a moment in the bar."""
        self.transcript.add_notice(message)
        self.statusBar().showMessage(message, 10000)

    # ---- Settings dialog -----------------------------------------------------

    def _open_settings(self) -> None:
        if self._settings_dialog is None:
            dialog = SettingsDialog(self)
            dialog.enabled_changed.connect(self._on_settings_enabled)
            dialog.voice_changed.connect(self._on_settings_voice)
            dialog.speed_changed.connect(self._on_settings_speed)
            dialog.sink_changed.connect(self._on_settings_sink)
            dialog.source_changed.connect(self._on_settings_source)
            dialog.wake_enabled_changed.connect(self._on_settings_wake_enabled)
            dialog.wake_threshold_changed.connect(self._on_settings_wake_threshold)
            dialog.wake_silence_changed.connect(self._on_settings_wake_silence)
            dialog.wake_margin_changed.connect(self._on_settings_wake_margin)
            dialog.tools_enabled_changed.connect(self._on_settings_tools_enabled)
            dialog.tools_place_changed.connect(self._on_settings_tools_place)
            dialog.tools_unit_changed.connect(self._on_settings_tools_unit)
            dialog.preview_clicked.connect(self._on_settings_preview)
            dialog.test_clicked.connect(self._on_settings_test)
            self._settings_dialog = dialog
        dialog = self._settings_dialog
        dialog.load(
            {
                "enabled": self.config["voice"]["enabled"],
                "name": self.config["voice"]["voice"],
                "speed": self.config["voice"]["speed"],
                "sink": self.config["voice"]["sink"],
            },
            audio.list_sinks(),
            self.speaker.catalog(),
            sources=audio.list_sources(),
            stt_settings=self.config.get("stt") or {},
            wake_settings=self.config.get("wake") or {},
            tools_settings=self.config.get("tools") or {},
            wake_count_text=self._wake_summary_text(),
        )
        self._sync_voice_from_dialog()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _sync_voice_from_dialog(self) -> None:
        """If the installed model doesn't have the configured voice, the dialog
        falls back to its first voice — mirror that into the config."""
        dialog = self._settings_dialog
        if dialog is None:
            return
        name = dialog.current_voice()
        if name and name != self.config["voice"]["voice"]:
            self.config["voice"]["voice"] = name
            config.save(self.config)
            self.speaker.configure(voice=name)

    def _on_settings_enabled(self, checked: bool) -> None:
        self.config["voice"]["enabled"] = bool(checked)
        config.save(self.config)
        self.speaker.configure(enabled=bool(checked))
        if checked:
            self._preload_speech()

    def _on_settings_voice(self, name: str) -> None:
        self.config["voice"]["voice"] = name
        config.save(self.config)
        self.speaker.configure(voice=name)

    def _on_settings_speed(self, speed: float) -> None:
        self.config["voice"]["speed"] = float(speed)
        config.save(self.config)
        self.speaker.configure(speed=float(speed))

    def _on_settings_sink(self, name: str) -> None:
        self.config["voice"]["sink"] = name or None
        config.save(self.config)
        self.speaker.configure(sink=self._resolved_sink_name(name or None))

    def _on_settings_source(self, name: str) -> None:
        self.config.setdefault("stt", {})["source"] = name or None
        config.save(self.config)
        self.statusBar().showMessage(
            f"Microphone: {name or 'Automatic (system default)'}", 4000
        )
        self._sync_wake()  # the wake listener follows the chosen microphone

    def _on_settings_wake_enabled(self, checked: bool) -> None:
        self.config.setdefault("wake", {})["enabled"] = bool(checked)
        config.save(self.config)
        self._wake_error = ""
        if not checked:
            self._wake_retry_timer.stop()
            self.wake.stop()
        self._sync_wake()
        self.statusBar().showMessage(
            "Wake word on — the header shows while the mic is being listened to."
            if checked
            else "Wake word off — the indicator is gone and the mic is cold.",
            5000,
        )

    def _on_settings_wake_threshold(self, value: float) -> None:
        self.config.setdefault("wake", {})["threshold"] = float(value)
        config.save(self.config)
        self.wake.set_threshold(float(value))

    def _on_settings_wake_silence(self, value: float) -> None:
        """Hang time — how much quiet ends a take; applies from the next take."""
        self.config.setdefault("wake", {})["silence_seconds"] = float(value)
        config.save(self.config)

    def _on_settings_wake_margin(self, value: float) -> None:
        """Speech margin — dB above the room floor; applies from the next take."""
        self.config.setdefault("wake", {})["speech_margin_db"] = float(value)
        config.save(self.config)

    def _on_settings_tools_enabled(self, checked: bool) -> None:
        """Master tools switch. Off means off: the router never runs."""
        self.config.setdefault("tools", {})["enabled"] = bool(checked)
        config.save(self.config)

    def _on_settings_tools_place(self, place: str) -> None:
        """Default place for weather questions that name none."""
        self.config.setdefault("tools", {})["place"] = place
        config.save(self.config)

    def _on_settings_tools_unit(self, unit: str) -> None:
        self.config.setdefault("tools", {})["unit"] = (
            "celsius" if str(unit).strip().lower() == "celsius" else "fahrenheit"
        )
        config.save(self.config)

    def _on_settings_preview(self) -> None:
        dialog = self._settings_dialog
        if dialog is None:
            return
        text = str(self.config["voice"].get("preview_text") or "")
        # Speaks even while spoken replies are toggled off — that's the point
        # of a preview.
        self.speaker.speak(text, voice=dialog.current_voice() or None, speed=dialog.current_speed())
        self.statusBar().showMessage("Previewing the selected voice…", 4000)

    def _on_settings_test(self) -> None:
        dialog = self._settings_dialog
        chime = state.PROJECT_ROOT / "appdata" / "chime.wav"
        if not audio.ensure_chime(chime):
            self._on_speech_error("Couldn't prepare the test sound.")
            return
        sink = self._resolved_sink_name(dialog.current_sink_name() if dialog else None)
        if audio.play_file(chime, sink) is None:
            self._on_speech_error("Couldn't play the test sound — is paplay available?")

    # ---- wake word (openWakeWord “hey jarvis”; opt-in) ------------------------

    def _wake_log_path(self) -> Path:
        return state.PROJECT_ROOT / "appdata" / "wake_log.jsonl"

    def _wake_summary_text(self) -> str:
        """The Settings line about the wake log — counts read from the file."""
        accepted, rejected, last = wake.log_summary(self._wake_log_path())
        if accepted <= 0 and rejected <= 0:
            return "No wakes logged yet — they land in appdata/wake_log.jsonl."
        line = f"{accepted} {'wake' if accepted == 1 else 'wakes'} logged"
        if rejected:
            line += f" ({rejected} candidate{'s' if rejected != 1 else ''} rejected)"
        return f"{line} — last {last} · appdata/wake_log.jsonl"

    def _resolve_wake_source(self) -> tuple[str | None, str]:
        """The microphone the listener must use: the configured voice-input
        source, else the system default. A configured source that is gone is an
        honest error — the listener never silently switches to a different mic."""
        wanted = (self.config.get("stt") or {}).get("source")
        if wanted:
            names = {source.name for source in audio.list_sources()}
            if wanted in names:
                return wanted, ""
            return None, f"Microphone “{wanted}” isn’t available — the wake word is idle."
        picked = audio.auto_source()
        if picked is not None:
            return picked.name, ""
        return None, "No microphone is available — the wake word is idle."

    def _sync_wake(self) -> None:
        """The one place that decides the listener's state (off / starting /
        listening / paused / error) from config and what is happening now."""
        cfg = self.config.get("wake") or {}
        if not cfg.get("enabled"):
            self._wake_resume_timer.stop()
            self._wake_retry_timer.stop()
            self._wake_error = ""
            self.wake.stop()
            self._set_wake_pill("off")
            return
        if self._wake_error:
            self._set_wake_pill("error", self._wake_error)
            return
        if self._wake_suppressed():
            if self.wake.running:
                self.wake.pause()
            self._set_wake_pill("paused")
            return
        source, problem = self._resolve_wake_source()
        if source is None:
            self._wake_error = problem
            self.wake.stop()
            self._set_wake_pill("error", problem)
            self._wake_retry_timer.start(WAKE_RETRY_MS)
            return
        if not self.wake.running:
            self._set_wake_pill("starting")
            if not self.wake.start(source):
                self._wake_error = self.wake.last_error or "Couldn't start the wake listener."
                self._set_wake_pill("error", self._wake_error)
                self._wake_retry_timer.start(WAKE_RETRY_MS)
            return
        self.wake.resume()
        self._set_wake_pill("listening")

    def _wake_suppressed(self) -> bool:
        """True whenever the listener must not look at the mic: while Jarvis
        speaks (his voice reaches the same room), while any recording runs,
        while transcribing (including verifying a candidate) — and for a short
        cool-down after those."""
        return (
            self._speech_busy
            or self.recorder.recording
            or self.transcriber.busy
            or self._wake_recording
            or self._verifying
            or time.monotonic() < self._wake_suppress_until
        )

    def _on_wake_ready(self) -> None:
        self._wake_error = ""
        self._set_wake_pill("listening")

    def _on_wake_failed(self, message: str) -> None:
        self._wake_error = message or "The wake listener stopped."
        self._set_wake_pill("error", self._wake_error)
        if (self.config.get("wake") or {}).get("enabled"):
            self._wake_retry_timer.start(WAKE_RETRY_MS)

    def _on_wake_retry(self) -> None:
        self._wake_error = ""
        self._sync_wake()

    def _on_wake(self, score: float, wav: str) -> None:
        """A candidate from the listener: verify the moment before committing.

        Nothing happens yet — no chime, no recording. The candidate audio is
        transcribed off the UI thread; only a transcript that really contains
        the name (`_on_wake_verified`) becomes a wake.
        """
        cfg = self.config.get("wake") or {}
        if not cfg.get("enabled") or self._wake_suppressed():
            self._sync_wake()  # a queued trigger racing TTS/PTT — re-arm, don't record over it
            return
        self._verifying = True
        self._sync_wake()  # stays paused until the verdict
        self.statusBar().showMessage("Wake word heard — checking it really was “hey jarvis”…")
        self._start_verification(score, wav)

    def _start_verification(self, score: float, wav: str) -> None:
        """Run the check on its own thread (a seam for the smoke tests)."""
        threading.Thread(
            target=self._run_verification,
            args=(float(score), wav or ""),
            name="jarvis-wake-verify",
            daemon=True,
        ).start()

    def _run_verification(self, score: float, wav: str) -> None:
        """Worker thread: transcribe the candidate, then hand the verdict back
        to the UI thread. Never raises — a failure is a rejection, logged."""
        try:
            accepted, text, reason = self._verification_result(wav)
        except Exception:
            accepted, text, reason = False, "", "verify-error"
        self.wake_verified.emit(float(score), bool(accepted), text, reason)

    def _verification_result(self, wav: str) -> tuple[bool, str, str]:
        """(accepted, transcript, reason) for the candidate audio.

        SenseVoice is fast (~10× real-time here), so the snippet decodes in a
        fraction of a second; the first check after a fresh start also pays
        the one-time model load. No audio or a broken engine is a rejection
        with an honest reason, never a silent pass.
        """
        path = Path(wav) if wav else None
        if path is None or not path.is_file():
            return False, "", "no-audio"
        text = self.stt_engine.transcribe(path)
        if text is None:
            return False, "", "stt-error"
        text = text.strip()
        if wake.transcript_has_wake(text):
            return True, text, ""
        return False, text, "no-wake-word"

    def _on_wake_verified(self, score: float, accepted: bool, text: str, reason: str) -> None:
        """The verdict (UI thread): every candidate is logged; only accepted
        ones chime and record, exactly like Phase 5 from there on."""
        self._verifying = False
        cfg = self.config.get("wake") or {}
        if not cfg.get("enabled"):
            self._sync_wake()
            return
        source, _problem = self._resolve_wake_source()
        wake.log_event(
            self._wake_log_path(),
            score,
            source or "",
            text=text,
            accepted=bool(accepted),
            reason=reason,
        )
        if not accepted:
            # A TV soundalike (or an unusable capture): silently ignored —
            # logged for Brandon, no chime, no recording.
            self.statusBar().showMessage(
                "Wake word candidate rejected — the transcript didn't contain the name.", 4000
            )
            self._finish_wake_cycle()
            return
        if self._wake_suppressed():
            # Verified, but something started speaking/recording meanwhile.
            self.statusBar().showMessage("“Hey jarvis” heard while Jarvis was busy — ignored.", 4000)
            self._finish_wake_cycle()
            return
        self._wake_suppress_until = time.monotonic() + self._wake_after_wake_ms / 1000.0
        self._wake_recording = True
        self._play_wake_chime()
        self.statusBar().showMessage("“Hey jarvis” heard — listening…")
        QTimer.singleShot(self._wake_delay_ms, self._begin_wake_recording)
        self._sync_wake()

    def _play_wake_chime(self) -> None:
        """The same chime Settings uses — but on the system default output,
        NOT the configured voice sink. The chime is the 'speak now' cue and
        recording starts WAKE_RECORD_DELAY_MS after it; a network voice sink
        (the Portal Snapcast stream buffers ~2-3 s) would deliver the cue
        after the recording window has already closed. The default output
        here is the Sunshine/Moonlight path to the laptop — instant. Voice
        replies still go to the configured voice sink.
        (A seam for the smoke tests: offscreen runs must never make noise.)"""
        chime = state.PROJECT_ROOT / "appdata" / "chime.wav"
        if not audio.ensure_chime(chime):
            return
        audio.play_file(chime)  # no sink = system default output

    def _begin_wake_recording(self) -> None:
        """After the chime: capture the request with the recorder's silence VAD.

        The end-of-speech line is relative to the room, and the listener's own
        floor estimate (it has watched the same room since before the take)
        seeds it — so a loud room (TV + speakers) ends the take just as
        promptly as a quiet one.
        """
        if not self._wake_recording:
            return
        wake_cfg = self.config.get("wake") or {}
        if not wake_cfg.get("enabled"):
            self._wake_recording = False
            self._sync_wake()
            return
        source, problem = self._resolve_wake_source()
        if source is None:
            self._wake_recording = False
            self._notify(problem)
            self._sync_wake()
            return
        auto = stt.AutoStop(
            silence_seconds=float(wake_cfg.get("silence_seconds", 1.0)),
            margin_db=float(wake_cfg.get("speech_margin_db", 8.0)),
        )
        started = self.recorder.start(
            source,
            auto_stop=auto,
            on_auto_stop=self.wake_recording_auto_stop.emit,
            floor_db=self.wake.noise_floor_db,
        )
        if not started:
            self._wake_recording = False
            self._notify("Couldn't open the microphone for the wake word.")
            self._sync_wake()
            return
        self._record_watchdog.start()
        self._warm_stt()
        self._set_mic_state("recording")

    def _on_wake_auto_stop(self) -> None:
        """The recorder heard the end of speech (queued from its reader thread)."""
        if not self._wake_recording:
            return
        self._wake_recording = False
        self._record_watchdog.stop()
        path = self.recorder.stop()
        reason = self.recorder.last_reason
        heard = self.recorder.last_heard_speech
        if path is None or reason == "eof":
            self._set_mic_state("idle")
            self._notify(
                "The microphone stream ended unexpectedly."
                if reason == "eof"
                else "Didn't catch that — try again."
            )
            self._finish_wake_cycle()
            return
        # Nothing above the room's noise floor was heard — honest, no invention.
        if (not heard and reason == "timeout") or stt.wav_seconds(path) < stt.MIN_SECONDS:
            stt.discard(path)
            self._set_mic_state("idle")
            self._notify("Didn't catch that — say “hey jarvis”, then your question.")
            self._finish_wake_cycle()
            return
        self._finish_wake_cycle()  # keep the listener paused through transcribe+reply
        self._set_mic_state("transcribing")
        self._transcribe_file(path)

    def _finish_wake_cycle(self) -> None:
        """Cool down, then let `_sync_wake` re-arm the listener when all is quiet."""
        self._wake_suppress_until = time.monotonic() + self._wake_after_wake_ms / 1000.0
        self._wake_resume_timer.start(self._wake_after_wake_ms + 30)
        self._sync_wake()

    def _cancel_recording(self) -> None:
        """Esc (or the mic button taking over) drops the recording on the floor."""
        if not (self.recorder.recording or self._wake_recording):
            return
        was_wake = self._wake_recording
        self._wake_recording = False
        self._record_watchdog.stop()
        path = self.recorder.stop()
        if path is not None:
            stt.discard(path)
        self._set_mic_state("idle")
        if was_wake:
            self._finish_wake_cycle()
        self.statusBar().showMessage("Recording cancelled.", 4000)

    def _set_wake_pill(self, kind: str, detail: str = "") -> None:
        """The header badge — the one place that tells the truth about the mic."""
        if kind == "off":
            self.wake_pill.hide()
            return
        if kind == "listening":
            self.wake_pill.setText(
                f'<span style="color:{theme.ACCENT}">●</span> '
                "Listening for “hey jarvis”"
            )
            self.wake_pill.setStyleSheet(
                "background: %s; border: 1px solid %s; border-radius: 11px;"
                " padding: 3px 10px; font-size: 11px;"
                % (theme.rgba(theme.ACCENT, 20), theme.rgba(theme.ACCENT, 90))
            )
            self.wake_pill.setToolTip(
                "The mic is being listened to for “hey jarvis” on this PC only.\n"
                "Turn it off in Settings → Wake word (or just hold the mic button)."
            )
        elif kind == "error":
            self.wake_pill.setText("Wake word unavailable")
            self.wake_pill.setStyleSheet(
                "background: transparent; border: 1px solid rgba(229, 57, 53, 150);"
                " border-radius: 11px; padding: 3px 10px; font-size: 11px;"
                " color: #FF8A80;"
            )
            self.wake_pill.setToolTip(detail or "The wake-word listener isn't running.")
        else:  # starting / paused — dim, and it does NOT claim to be listening
            self.wake_pill.setText(
                "Wake word starting…" if kind == "starting" else "Wake word paused"
            )
            self.wake_pill.setStyleSheet(
                "background: transparent; border: 1px solid %s; border-radius: 11px;"
                " padding: 3px 10px; font-size: 11px; color: %s;"
                % (theme.rgba(theme.TEXT, 40), theme.TEXT_FAINT)
            )
            self.wake_pill.setToolTip(
                "Not listening right now — Jarvis is speaking, recording, or cooling down."
            )
        self.wake_pill.show()

    # ---- session / sending ------------------------------------------------

    def _render_session(self) -> None:
        self.transcript.clear()
        for message in self.messages:
            if message["role"] == "user":
                self.transcript.add_user(message["content"])
            else:
                self.transcript.add_assistant(message["content"])
        self.stack.setCurrentIndex(1 if self.messages else 0)

    def new_session(self) -> None:
        if self._busy:
            return
        self.speaker.cancel()
        self.messages = []
        self._persist()
        self.transcript.clear()
        self.stack.setCurrentIndex(0)
        self.composer.setFocus()

    def _on_chip(self, text: str) -> None:
        if self._can_send():
            self.send_message(text)
        else:
            # No model live: don't lose the click — stage the text in the box.
            self.composer.setPlainText(text)
            self.composer.setFocus()

    # ---- push-to-talk speech input (local SenseVoice) -----------------------

    def _set_mic_state(self, state: str) -> None:
        """recording / transcribing / idle — shown on both mic buttons and the
        home caption, so the state is visible wherever the eye happens to be."""
        self._mic_state = state
        recording = state == "recording"
        transcribing = state == "transcribing"
        for button in (self.idle.mic, self.talk_btn):
            button.set_recording(recording)
            button.set_busy(transcribing)
        self.idle.set_voice_state(state)
        message = {"recording": "Listening… release to send", "transcribing": "Transcribing…"}.get(state)
        if message:
            self.statusBar().showMessage(message)
        self._sync_wake()  # recording/transcribing state changed → re-decide the listener

    def _warm_stt(self) -> None:
        """Load the speech model on a background thread — started the moment a
        recording begins, so the load overlaps the speaking (idempotent)."""
        if self._stt_warmed:
            return
        self._stt_warmed = True
        threading.Thread(
            target=self.stt_engine.load, name="jarvis-stt-preload", daemon=True
        ).start()

    def _on_mic_pressed(self) -> None:
        if self._wake_recording:
            self._cancel_recording()  # the button takes over from the wake take
        if self._verifying or self.recorder.recording or self.transcriber.busy:
            return  # a candidate is being checked — one moment, then the mic is free
        self.speaker.cancel()  # talking over Jarvis stops him, like a person would
        wanted = (self.config.get("stt") or {}).get("source")
        if not self.recorder.start(self._resolved_source_name(wanted)):
            self._notify("Couldn't open the microphone — is parec available?")
            return
        self._record_watchdog.start()
        self._warm_stt()  # load the model while he speaks
        self._set_mic_state("recording")

    def _on_mic_released(self) -> None:
        if not self.recorder.recording:
            return
        self._record_watchdog.stop()
        path = self.recorder.stop()
        if path is None:
            self._set_mic_state("idle")
            self._notify("Couldn't capture the microphone.")
            return
        if stt.wav_seconds(path) < stt.MIN_SECONDS:
            stt.discard(path)
            self._set_mic_state("idle")
            self._notify("Didn't catch that — hold the mic while you speak.")
            return
        self._set_mic_state("transcribing")
        self._transcribe_file(path)

    def _transcribe_file(self, path) -> None:
        """Hand the WAV to the worker thread (a seam for the smoke tests —
        they replace this to avoid loading the model offscreen)."""
        if not self.transcriber.start(path):
            self._set_mic_state("idle")
            self._notify("Still transcribing the last recording — one moment.")

    def _on_stt_finished(self, text: str) -> None:
        self._set_mic_state("idle")
        text = (text or "").strip()
        if not text:
            self._notify("Didn't catch that — hold the mic and speak again.")
            return
        if self._can_send():
            self.send_message(text)  # exactly like typed words
            return
        # No model live (or one is still replying): never lose the words —
        # stage them in the box where typed text would go.
        self.composer.setPlainText(text)
        self.composer.setFocus()
        if self._busy:
            self._notify("Jarvis is still replying — your words are in the message box.")
        else:
            self._notify("No model is running — your words are in the message box.")

    def _on_stt_failed(self, message: str) -> None:
        self._set_mic_state("idle")
        self._notify(message or "Transcription failed.")

    def _on_send_clicked(self) -> None:
        if not self._can_send():
            return
        text = self.composer.toPlainText().strip()
        if not text:
            return
        self.send_message(text)

    def send_message(self, text: str) -> None:
        text = text.strip()
        if not text or not self._can_send():
            return

        self.speaker.cancel()  # a new question interrupts whatever is being said
        self.messages.append({"role": "user", "content": text})
        self.stack.setCurrentIndex(1)
        self.transcript.add_user(text)
        self.composer.clear()
        self._persist()

        self._busy = True
        self._streaming_text = ""
        self._refresh_status_ui()

        call = tools.route(text) if self._tools_enabled() else None
        if call is None:
            self._start_request(self._history_for_request())
            return
        # A read-only tool applies: run it off the UI thread (a dead network can
        # never freeze the reply). The result comes back on `tool_finished`, is
        # noted + logged, then injected ahead of the user's message.
        self.statusBar().showMessage(
            "Checking the weather…" if call.tool == "weather" else "Reading the clock…", 5000
        )
        self._start_tool(call)

    def _start_request(self, history: list[dict]) -> None:
        """Build the real request (system prompt + history) and stream a reply."""
        self.transcript.begin_assistant()
        request = [
            {
                "role": "system",
                "content": build_system_prompt(
                    datetime.now(), name=self._name, tools=self._tools_enabled()
                ),
            }
        ]
        request += history
        self.llm.start(self.model.port, request)

    # ---- read-only tools (weather, clock) -----------------------------------

    def _tools_enabled(self) -> bool:
        return bool((self.config.get("tools") or {}).get("enabled", True))

    def _tools_log_path(self) -> Path:
        return Path(state.PROJECT_ROOT) / "appdata" / "tools_log.jsonl"

    def _next_tool_seq(self) -> int:
        self._tool_seq += 1
        return self._tool_seq

    def _start_tool(self, call: tools.ToolCall) -> None:
        """Run the lookup on its own thread — the network never touches the UI thread."""
        seq = self._next_tool_seq()
        thread = threading.Thread(
            target=self._run_tool, args=(call, seq), name="jarvis-tool", daemon=True
        )
        thread.start()

    def _run_tool(self, call: tools.ToolCall, seq: int) -> None:
        """Worker thread: resolve config, run the lookup, carry it back by signal."""
        cfg = self.config.get("tools") or {}
        try:
            if call.tool == "weather":
                result = tools.weather_result(
                    call.place or str(cfg.get("place") or tools.DEFAULT_PLACE),
                    unit=str(cfg.get("unit") or tools.DEFAULT_UNIT),
                )
            else:
                result = tools.clock_result()
        except Exception as exc:  # a tool crash must never take a reply down
            result = tools.failure_result(
                call.tool, f"unexpected error ({exc.__class__.__name__})"
            )
        self.tool_finished.emit((seq, result))

    def _on_tool_finished(self, payload) -> None:
        """UI thread: log it, show the one-line note, inject ahead of the message."""
        seq, result = payload
        if seq != self._tool_seq:
            return  # superseded (defensive: a stale lookup never lands in a fresh turn)
        tools.log_result(self._tools_log_path(), result)
        self.transcript.add_tool_note(result.note)
        self.statusBar().showMessage(result.note, 8000)

        history = self._history_for_request()
        if result.context and self._tools_enabled():
            if history and history[-1].get("role") == "user":
                history = history[:-1] + [
                    {"role": "system", "content": result.context},
                    history[-1],
                ]
            else:
                history = history + [{"role": "system", "content": result.context}]
        self._start_request(history)

    # ---- streaming callbacks ----------------------------------------------

    def _on_delta(self, piece: str) -> None:
        self._streaming_text += piece
        self.transcript.set_streaming_text(self._streaming_text)
        self.speaker.feed(piece)

    def _finish_busy(self) -> None:
        self._busy = False
        self._refresh_status_ui()

    def _on_finished(self, full: str) -> None:
        text = (full or self._streaming_text).strip()
        if text:
            self.transcript.finish_streaming(text)
            self.messages.append({"role": "assistant", "content": text})
            self._persist()
        else:
            self.transcript.finish_streaming("")
            self.transcript.add_notice("The model returned an empty reply — try again.")
        self.speaker.flush()
        self._finish_busy()

    def _on_failed(self, error: str) -> None:
        partial = self._streaming_text.strip()
        if partial:
            self.transcript.finish_streaming(partial)
            self.messages.append({"role": "assistant", "content": partial})
            self._persist()
            self.speaker.flush()  # speak what did arrive, if anything is queued
        else:
            self.transcript.finish_streaming("")
            self.speaker.cancel()
        self.transcript.add_notice(error)
        self._finish_busy()

    # ---- system tray (24/7 presence) ---------------------------------------

    def _setup_tray(self) -> None:
        """Close-to-tray: the X hides Jarvis instead of quitting, so he stays
        resident (wake word listening) around the clock. The only in-app exit
        is the tray menu's Quit. A desktop with no tray keeps the old close."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = None
            return
        tray = QSystemTrayIcon(QIcon(make_orb_pixmap(64)), self)
        tray.setToolTip(self._name)
        menu = QMenu()
        open_action = QAction(f"Open {self._name}", self)
        open_action.triggered.connect(self._restore_from_tray)
        menu.addAction(open_action)
        menu.addSeparator()
        quit_action = QAction(f"Quit {self._name}", self)
        quit_action.triggered.connect(self._quit_from_tray)
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self._tray = tray

    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            if self.isVisible():
                self.hide()
            else:
                self._restore_from_tray()

    def _quit_from_tray(self) -> None:
        """The tray's Quit. A hidden window cannot be closed - close() on one
        is a no-op, which is why the first version's Quit did nothing while
        Jarvis sat in the tray. Surface first so closeEvent runs the real
        teardown, then quit the application outright as a backstop."""
        self._really_quit = True
        self.show()
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _hide_to_tray(self) -> bool:
        """Absorb a close into the tray: hide, keep everything running, and
        (once) tell the user where Jarvis went. False = do the real teardown."""
        if self._really_quit or self._tray is None:
            return False
        self.hide()
        if not self._tray_tip_shown:
            self._tray_tip_shown = True
            self._tray.showMessage(
                self._name,
                f"{self._name} is still running - the wake word still listens. "
                "Use the tray icon to open or quit.",
                QSystemTrayIcon.MessageIcon.Information,
                4000,
            )
        return True

    # ---- lifecycle --------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._hide_to_tray():
            event.ignore()
            return
        self.speaker.cancel()
        self._record_watchdog.stop()
        self._wake_resume_timer.stop()
        self._wake_retry_timer.stop()
        self.wake.stop()
        if self.recorder.recording:
            leftover = self.recorder.stop()
            if leftover is not None:
                stt.discard(leftover)
        self.health.stop()
        self.unit_poller.stop()
        self._persist()
        super().closeEvent(event)
