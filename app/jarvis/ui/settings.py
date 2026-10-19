"""The Settings dialog: the Kokoro voice, speed, the spoken-replies toggle,
the audio output device, the speech-input microphone, the opt-in wake word
("hey jarvis"), and the read-only tools (weather place/unit) — with Preview
and Test sound buttons.

Voice list: filled from the **installed model's own catalog** (see tts.py) —
whichever Kokoro model `voice.model_dir` points at, its voices appear here,
with no code changes. Choices are stored **by voice name, never by sid**.
Input sources come from pactl the same way ("Automatic" = the system default
source, whatever it is at the time).

Settings apply as you change them (the Portal way — no OK to forget) and are
persisted immediately by the window. Until the catalog is known (model folder
missing, or still being read) the picker shows a placeholder.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .. import audio, tools
from ..tts import Voice
from . import theme

AUTO_SINK = ""  # combo sentinel: "Automatic" (the system default device)


def _row_label(text: str, width: int = 64) -> QLabel:
    label = QLabel(text)
    label.setFixedWidth(width)
    label.setStyleSheet("color: %s; font-size: 13px;" % theme.TEXT_SUBTLE)
    return label


class SettingsDialog(QDialog):
    enabled_changed = pyqtSignal(bool)
    voice_changed = pyqtSignal(str)
    speed_changed = pyqtSignal(float)
    sink_changed = pyqtSignal(str)    # AUTO_SINK means "follow the system default output"
    source_changed = pyqtSignal(str)  # AUTO_SINK means "follow the system default source"
    wake_enabled_changed = pyqtSignal(bool)
    wake_threshold_changed = pyqtSignal(float)
    wake_silence_changed = pyqtSignal(float)   # end-of-speech hang time, seconds
    wake_margin_changed = pyqtSignal(float)    # dB above the room's noise floor = speech
    tools_enabled_changed = pyqtSignal(bool)   # read-only lookups (weather, date/time)
    tools_place_changed = pyqtSignal(str)      # default place for nameless weather questions
    tools_unit_changed = pyqtSignal(str)       # "fahrenheit" | "celsius"
    preview_clicked = pyqtSignal()
    test_clicked = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(False)
        self.setMinimumWidth(720)
        self._loading = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 16)
        layout.setSpacing(14)

        # The cards live in a scroll area: five cards no longer fit short
        # screens (the Tools card pushed the Done button off the bottom).
        # The footer stays outside the scroll, always reachable.
        cards = QWidget()
        cards_layout = QVBoxLayout(cards)
        cards_layout.setContentsMargins(0, 0, 6, 0)
        cards_layout.setSpacing(14)
        cards_layout.addWidget(self._build_voice_card())
        cards_layout.addWidget(self._build_tools_card())
        cards_layout.addWidget(self._build_input_card())
        cards_layout.addWidget(self._build_wake_card())
        cards_layout.addWidget(self._build_output_card())
        scroll = QScrollArea()
        scroll.setObjectName("settingsscroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(cards)
        layout.addWidget(scroll, 1)

        footer = QHBoxLayout()
        note = QLabel("Changes apply and save as you make them.")
        note.setStyleSheet("color: %s; font-size: 11px;" % theme.TEXT_FAINT)
        footer.addWidget(note)
        footer.addStretch(1)
        done_btn = QPushButton("Done")
        done_btn.setObjectName("ghost")
        done_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        done_btn.clicked.connect(self.close)
        footer.addWidget(done_btn)
        layout.addLayout(footer)

        self.setMinimumHeight(340)
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            cap = int(screen.availableGeometry().height() * 0.92)
            self.setMaximumHeight(cap)
            self.resize(max(self.width(), 780), min(880, cap))

    # ---- cards -------------------------------------------------------------

    def _build_voice_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("settingcard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 16)
        layout.setSpacing(10)

        title = QLabel("Voice")
        title.setStyleSheet("font-size: 14px; font-weight: 600;")
        layout.addWidget(title)

        self.enabled_check = QCheckBox("Speak replies out loud")
        self.enabled_check.toggled.connect(self._emit_enabled)
        layout.addWidget(self.enabled_check)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(_row_label("Voice"))
        self.voice_combo = QComboBox()
        self.voice_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.voice_combo.setMinimumContentsLength(28)
        self.voice_combo.setMinimumWidth(300)
        self.voice_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.voice_combo.currentIndexChanged.connect(self._emit_voice)
        row.addWidget(self.voice_combo, 1)
        self.preview_btn = QPushButton("Preview")
        self.preview_btn.setObjectName("ghost")
        self.preview_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.preview_btn.setToolTip("Speak a sample line with the selected voice")
        self.preview_btn.clicked.connect(self.preview_clicked)
        row.addWidget(self.preview_btn)
        layout.addLayout(row)

        speed_row = QHBoxLayout()
        speed_row.setSpacing(10)
        speed_row.addWidget(_row_label("Speed"))
        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(50, 200)  # 0.5× – 2.0×
        self.speed_slider.setSingleStep(10)
        self.speed_slider.setPageStep(10)
        self.speed_slider.valueChanged.connect(self._emit_speed)
        speed_row.addWidget(self.speed_slider, 1)
        self.speed_value = QLabel("1.0×")
        self.speed_value.setFixedWidth(48)
        self.speed_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.speed_value.setStyleSheet("color: %s; font-size: 13px;" % theme.TEXT_SUBTLE)
        speed_row.addWidget(self.speed_value)
        layout.addLayout(speed_row)

        self.voice_hint = QLabel("")
        self.voice_hint.setWordWrap(True)
        self.voice_hint.setStyleSheet("color: %s; font-size: 11px;" % theme.TEXT_FAINT)
        layout.addWidget(self.voice_hint)

        return card

    def _build_tools_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("settingcard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 16)
        layout.setSpacing(10)

        title = QLabel("Tools")
        title.setStyleSheet("font-size: 14px; font-weight: 600;")
        layout.addWidget(title)

        self.tools_check = QCheckBox("Local lookups (weather, date & time)")
        self.tools_check.setToolTip(
            "When on, questions about the weather or the time are looked up "
            "before the model replies — weather from Open-Meteo (free, no "
            "account), date/time from this PC. Off means off: questions go to "
            "the model exactly as before."
        )
        self.tools_check.toggled.connect(self._emit_tools_enabled)
        layout.addWidget(self.tools_check)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(_row_label("Place", 84))
        self.tools_place = QLineEdit()
        self.tools_place.setPlaceholderText(tools.DEFAULT_PLACE)
        self.tools_place.setToolTip(
            "Used when a weather question names no place. “City, State” works "
            "best; a place named in the question always wins."
        )
        self.tools_place.editingFinished.connect(self._emit_tools_place)
        row.addWidget(self.tools_place, 1)
        layout.addLayout(row)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(_row_label("Units", 84))
        self.tools_unit = QComboBox()
        self.tools_unit.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tools_unit.addItem("Fahrenheit (°F)", userData="fahrenheit")
        self.tools_unit.addItem("Celsius (°C)", userData="celsius")
        self.tools_unit.currentIndexChanged.connect(self._emit_tools_unit)
        row.addWidget(self.tools_unit, 1)
        layout.addLayout(row)

        hint = QLabel(
            "Read-only: no web search, no PC control, nothing changed anywhere. "
            "If a lookup fails (no network, unknown place) Jarvis says so "
            "plainly rather than guessing — the failure is logged in appdata/"
            "tools_log.jsonl. Chat that doesn't ask for weather or the time is "
            "untouched."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: %s; font-size: 11px;" % theme.TEXT_FAINT)
        layout.addWidget(hint)

        return card

    def _build_input_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("settingcard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 16)
        layout.setSpacing(10)

        title = QLabel("Voice input")
        title.setStyleSheet("font-size: 14px; font-weight: 600;")
        layout.addWidget(title)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(_row_label("Mic"))
        self.source_combo = QComboBox()
        self.source_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.source_combo.setMinimumContentsLength(28)
        self.source_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.source_combo.currentIndexChanged.connect(self._emit_source)
        row.addWidget(self.source_combo, 1)
        layout.addLayout(row)

        hint = QLabel(
            "Hold the mic button (home screen or beside the message box) to talk — "
            "speech is transcribed on this PC and lands in the chat like typed "
            "words. “Automatic” follows the system default microphone."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: %s; font-size: 11px;" % theme.TEXT_FAINT)
        layout.addWidget(hint)

        return card

    def _build_wake_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("settingcard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 16)
        layout.setSpacing(10)

        title = QLabel("Wake word")
        title.setStyleSheet("font-size: 14px; font-weight: 600;")
        layout.addWidget(title)

        self.wake_check = QCheckBox("Listen for “hey jarvis”")
        self.wake_check.toggled.connect(self._emit_wake_enabled)
        layout.addWidget(self.wake_check)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(_row_label("Trigger", 84))
        self.wake_slider = QSlider(Qt.Orientation.Horizontal)
        self.wake_slider.setRange(30, 90)  # 0.30 – 0.90
        self.wake_slider.setSingleStep(5)
        self.wake_slider.setPageStep(5)
        self.wake_slider.valueChanged.connect(self._emit_wake_threshold)
        row.addWidget(self.wake_slider, 1)
        self.wake_value = QLabel("0.50")
        self.wake_value.setFixedWidth(48)
        self.wake_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.wake_value.setStyleSheet("color: %s; font-size: 13px;" % theme.TEXT_SUBTLE)
        row.addWidget(self.wake_value)
        layout.addLayout(row)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(_row_label("Hang time", 84))
        self.wake_silence = QSlider(Qt.Orientation.Horizontal)
        self.wake_silence.setRange(50, 300)  # 0.50 – 3.00 s
        self.wake_silence.setSingleStep(25)
        self.wake_silence.setPageStep(25)
        self.wake_silence.setToolTip(
            "How long the room must stay quiet after you stop talking before the "
            "take ends (0.5–3.0 s). Once you have spoken, the take ends after this "
            "much quiet — the 15 s cap is only a backstop."
        )
        self.wake_silence.valueChanged.connect(self._emit_wake_silence)
        row.addWidget(self.wake_silence, 1)
        self.wake_silence_value = QLabel("1.00 s")
        self.wake_silence_value.setFixedWidth(48)
        self.wake_silence_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.wake_silence_value.setStyleSheet("color: %s; font-size: 13px;" % theme.TEXT_SUBTLE)
        row.addWidget(self.wake_silence_value)
        layout.addLayout(row)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(_row_label("Margin", 84))
        self.wake_margin = QSlider(Qt.Orientation.Horizontal)
        self.wake_margin.setRange(4, 16)  # 4 – 16 dB
        self.wake_margin.setSingleStep(1)
        self.wake_margin.setPageStep(2)
        self.wake_margin.setToolTip(
            "How far above the room's own noise floor your voice must be to count "
            "as speech (dB). Raise it if the TV keeps the take open; lower it if "
            "your voice isn't picked up in a loud room."
        )
        self.wake_margin.valueChanged.connect(self._emit_wake_margin)
        row.addWidget(self.wake_margin, 1)
        self.wake_margin_value = QLabel("+8 dB")
        self.wake_margin_value.setFixedWidth(48)
        self.wake_margin_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.wake_margin_value.setStyleSheet("color: %s; font-size: 13px;" % theme.TEXT_SUBTLE)
        row.addWidget(self.wake_margin_value)
        layout.addLayout(row)

        self.wake_count = QLabel("")
        self.wake_count.setWordWrap(True)
        self.wake_count.setStyleSheet("color: %s; font-size: 11px;" % theme.TEXT_SUBTLE)
        layout.addWidget(self.wake_count)

        hint = QLabel(
            "Off by default. While it is on, the mic stays open for the local "
            "openWakeWord model on this PC (nothing leaves the machine) and the "
            "header badge shows whenever it is truly being listened to. Every "
            "detection is checked before it counts: the moment is transcribed "
            "locally and the chime only plays when the name is really in it — TV "
            "soundalikes are logged as rejected and ignored. Higher trigger = "
            "fewer false wakes, but “hey jarvis” has to be clearer."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: %s; font-size: 11px;" % theme.TEXT_FAINT)
        layout.addWidget(hint)

        return card

    def _build_output_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("settingcard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 16)
        layout.setSpacing(10)

        title = QLabel("Audio output")
        title.setStyleSheet("font-size: 14px; font-weight: 600;")
        layout.addWidget(title)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(_row_label("Device"))
        self.sink_combo = QComboBox()
        self.sink_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.sink_combo.setMinimumContentsLength(28)
        self.sink_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sink_combo.currentIndexChanged.connect(self._emit_sink)
        row.addWidget(self.sink_combo, 1)
        self.test_btn = QPushButton("Test sound")
        self.test_btn.setObjectName("ghost")
        self.test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.test_btn.setToolTip("Play a short chime on the selected device")
        self.test_btn.clicked.connect(self.test_clicked)
        row.addWidget(self.test_btn)
        layout.addLayout(row)

        hint = QLabel(
            "“Automatic” follows the system default output — the same path every "
            "other app uses (on this PC that is the Moonlight stream). Pick a "
            "device to pin Jarvis's voice there instead; virtual devices are "
            "fine to choose."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: %s; font-size: 11px;" % theme.TEXT_FAINT)
        layout.addWidget(hint)

        return card

    # ---- populate / read -----------------------------------------------------

    def load(
        self,
        settings: dict,
        sinks: list[audio.Sink],
        voices: list[Voice] | None = None,
        sources: list[audio.Source] | None = None,
        stt_settings: dict | None = None,
        wake_settings: dict | None = None,
        tools_settings: dict | None = None,
        wake_count_text: str = "",
    ) -> None:
        """Fill the widgets from settings + fresh catalogs (no signals out)."""
        self._loading = True
        try:
            self.enabled_check.setChecked(bool(settings.get("enabled", True)))

            self._fill_voices(voices)
            name = str(settings.get("name") or "")
            index = self.voice_combo.findData(name) if name else -1
            if index >= 0:
                self.voice_combo.setCurrentIndex(index)

            speed = float(settings.get("speed", 1.0))
            self.speed_slider.setValue(int(round(speed * 100)))
            self._update_speed_label(self.speed_slider.value())

            self._fill_sinks(sinks, settings.get("sink"))
            self._fill_sources(sources or [], (stt_settings or {}).get("source"))

            wake_cfg = wake_settings or {}
            self.wake_check.setChecked(bool(wake_cfg.get("enabled", False)))
            threshold = float(wake_cfg.get("threshold", 0.5) or 0.5)
            self.wake_slider.setValue(int(round(max(0.3, min(0.9, threshold)) * 100)))
            self._update_wake_label(self.wake_slider.value())
            silence = float(wake_cfg.get("silence_seconds", 1.0) or 1.0)
            self.wake_silence.setValue(int(round(max(0.5, min(3.0, silence)) * 100)))
            self._update_wake_silence_label(self.wake_silence.value())
            margin = float(wake_cfg.get("speech_margin_db", 8.0) or 8.0)
            self.wake_margin.setValue(int(round(max(4.0, min(16.0, margin)))))
            self._update_wake_margin_label(self.wake_margin.value())
            self.wake_count.setText(wake_count_text)

            tools_cfg = tools_settings or {}
            self.tools_check.setChecked(bool(tools_cfg.get("enabled", True)))
            self.tools_place.setText(str(tools_cfg.get("place") or tools.DEFAULT_PLACE))
            unit = str(tools_cfg.get("unit") or "fahrenheit").lower()
            unit_index = self.tools_unit.findData(unit)
            self.tools_unit.setCurrentIndex(unit_index if unit_index >= 0 else 0)
        finally:
            self._loading = False

    def refresh_voices(self, voices: list[Voice] | None, prefer: str | None = None) -> None:
        """The catalog changed (model finished reading/loading): refill, keep the
        current pick, else `prefer`, else fall back to the first voice."""
        wanted = self.current_voice() or (prefer or "")
        self._loading = True
        try:
            self._fill_voices(voices)
            index = self.voice_combo.findData(wanted) if wanted else -1
            if index >= 0:
                self.voice_combo.setCurrentIndex(index)
        finally:
            self._loading = False

    def _fill_voices(self, voices: list[Voice] | None) -> None:
        self.voice_combo.clear()
        if not voices:
            self.voice_combo.addItem("Voice model not found…", userData=None)
            self.voice_combo.setEnabled(False)
            self.preview_btn.setEnabled(False)
            self.voice_hint.setText(
                "No voices yet — the Kokoro model folder is missing or unreadable. "
                "Point voice.model_dir in appdata/config.json at a model, then reopen."
            )
            return
        self.voice_combo.setEnabled(True)
        self.preview_btn.setEnabled(True)
        for voice in voices:
            label = f"{voice.name} — {voice.label}" if voice.label else voice.name
            self.voice_combo.addItem(label, userData=voice.name)
        self.voice_hint.setText(
            f"{len(voices)} voices from the installed model. "
            "Pick with your ears — Preview speaks the sample line."
        )

    def _fill_sinks(self, sinks: list[audio.Sink], wanted: str | None) -> None:
        self.sink_combo.clear()
        picked = audio.auto_pick(sinks)
        auto_label = (
            f"Automatic — {audio.display_name(picked)}" if picked else "Automatic (system default)"
        )
        self.sink_combo.addItem(auto_label, userData=AUTO_SINK)
        for sink in sinks:
            self.sink_combo.addItem(audio.display_name(sink), userData=sink.name)
        index = self.sink_combo.findData(wanted) if wanted else 0
        self.sink_combo.setCurrentIndex(index if index >= 0 else 0)
        self.sink_combo.setToolTip(str(wanted or auto_label))

    def _fill_sources(self, sources: list[audio.Source], wanted: str | None) -> None:
        self.source_combo.clear()
        default = audio.auto_source(sources)
        auto_label = (
            f"Automatic — {audio.source_display_name(default)}"
            if default
            else "Automatic (system default)"
        )
        self.source_combo.addItem(auto_label, userData=AUTO_SINK)
        for source in sources:
            if source.monitor:  # captures output, not a microphone
                continue
            self.source_combo.addItem(audio.source_display_name(source), userData=source.name)
        index = self.source_combo.findData(wanted) if wanted else 0
        self.source_combo.setCurrentIndex(index if index >= 0 else 0)
        self.source_combo.setToolTip(str(wanted or auto_label))

    def current_voice(self) -> str:
        data = self.voice_combo.currentData()
        return str(data) if data else ""

    def current_speed(self) -> float:
        return self.speed_slider.value() / 100.0

    def current_sink_name(self) -> str | None:
        """The chosen sink; None when "Automatic"."""
        data = self.sink_combo.currentData()
        return str(data) if data else None

    def current_source_name(self) -> str | None:
        """The chosen input source; None when "Automatic"."""
        data = self.source_combo.currentData()
        return str(data) if data else None

    # ---- signal plumbing -----------------------------------------------------

    def _emit_enabled(self, checked: bool) -> None:
        if not self._loading:
            self.enabled_changed.emit(bool(checked))

    def _emit_voice(self, index: int) -> None:
        if self._loading:
            return
        name = self.voice_combo.itemData(index)
        if name:
            self.voice_changed.emit(str(name))

    def _emit_speed(self, value: int) -> None:
        self._update_speed_label(value)
        if not self._loading:
            self.speed_changed.emit(value / 100.0)

    def _emit_sink(self, index: int) -> None:
        if self._loading:
            return
        data = self.sink_combo.itemData(index)
        if data is not None:  # AUTO_SINK ("") and real names both count
            self.sink_changed.emit(str(data))

    def _emit_source(self, index: int) -> None:
        if self._loading:
            return
        data = self.source_combo.itemData(index)
        if data is not None:  # AUTO_SINK ("") and real names both count
            self.source_changed.emit(str(data))

    def _emit_wake_enabled(self, checked: bool) -> None:
        if not self._loading:
            self.wake_enabled_changed.emit(bool(checked))

    def _emit_wake_threshold(self, value: int) -> None:
        self._update_wake_label(value)
        if not self._loading:
            self.wake_threshold_changed.emit(value / 100.0)

    def _emit_wake_silence(self, value: int) -> None:
        self._update_wake_silence_label(value)
        if not self._loading:
            self.wake_silence_changed.emit(value / 100.0)

    def _emit_wake_margin(self, value: int) -> None:
        self._update_wake_margin_label(value)
        if not self._loading:
            self.wake_margin_changed.emit(float(value))

    def _emit_tools_enabled(self, checked: bool) -> None:
        if not self._loading:
            self.tools_enabled_changed.emit(bool(checked))

    def _emit_tools_place(self) -> None:
        if self._loading:
            return
        place = self.tools_place.text().strip()
        if place:  # an emptied field keeps the old value; the dialog shows the default
            self.tools_place_changed.emit(place)

    def _emit_tools_unit(self, index: int) -> None:
        if self._loading:
            return
        data = self.tools_unit.itemData(index)
        if data:
            self.tools_unit_changed.emit(str(data))

    def _update_speed_label(self, value: int) -> None:
        self.speed_value.setText(f"{value / 100.0:.1f}×")

    def _update_wake_label(self, value: int) -> None:
        self.wake_value.setText(f"{value / 100.0:.2f}")

    def _update_wake_silence_label(self, value: int) -> None:
        self.wake_silence_value.setText(f"{value / 100.0:.2f} s")

    def _update_wake_margin_label(self, value: int) -> None:
        self.wake_margin_value.setText(f"+{value} dB")
