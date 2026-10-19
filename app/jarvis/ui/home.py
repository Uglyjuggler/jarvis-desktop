"""The idle home: ambient clock + date, the greeting, suggestion chips, and
the big push-to-talk mic button — the Portal's idle screen. Greeting and
assistant name come from appdata/config.json (see config.py)."""

from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .widgets import MicButton

# The stock Portal suggestion chips (upstream ConversationScreen, verified in Phase 0).
CHIPS = ("What's the weather?", "Set a 10-minute timer")

# The line under the mic, per voice state (Phase 4: push-to-talk is real now).
VOICE_STATES = {
    "idle": "spoken replies are live — hold to talk, release to send",
    "recording": "Listening… release to send",
    "transcribing": "Transcribing…",
}


class IdleHome(QWidget):
    chip_clicked = pyqtSignal(str)

    def __init__(
        self,
        greeting: str = "How can I help?",
        assistant: str = "Jarvis",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._build(greeting, assistant)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        self._tick()

    def _build(self, greeting_text: str, assistant: str) -> None:
        # Left column: the ambient clock + date.
        self.clock = QLabel("") 
        self.clock.setStyleSheet("font-size: 56px; color: %s;" % theme.TEXT)
        self.date = QLabel("")
        self.date.setStyleSheet("font-size: 13px; color: %s;" % theme.TEXT_SUBTLE)

        left = QVBoxLayout()
        left.addStretch(1)
        left.addWidget(self.clock, alignment=Qt.AlignmentFlag.AlignHCenter)
        left.addWidget(self.date, alignment=Qt.AlignmentFlag.AlignHCenter)
        left.addStretch(1)

        divider = QWidget()
        divider.setFixedSize(1, 150)
        divider.setStyleSheet("background: rgba(241, 236, 230, 26);")

        # Right column: greeting + chips.
        self._greeting = QLabel(greeting_text)
        self._greeting.setStyleSheet("font-size: 28px; color: %s;" % theme.TEXT)

        chips = QHBoxLayout()
        chips.setSpacing(12)
        chips.addStretch(1)
        for text in CHIPS:
            button = QPushButton(text)
            button.setObjectName("chip")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, t=text: self.chip_clicked.emit(t))
            chips.addWidget(button)
        chips.addStretch(1)

        right = QVBoxLayout()
        right.addStretch(1)
        right.addWidget(self._greeting, alignment=Qt.AlignmentFlag.AlignHCenter)
        right.addSpacing(10)
        right.addLayout(chips)
        right.addStretch(1)

        columns = QHBoxLayout()
        columns.setContentsMargins(60, 0, 60, 0)
        columns.setSpacing(40)
        columns.addLayout(left, 1)
        columns.addWidget(divider)
        columns.addLayout(right, 1)

        # The big mic: push-to-talk — hold to record, release to transcribe.
        self.mic = MicButton()
        self.mic.setToolTip("Hold to talk — release to send")

        caption = QLabel("Hold the mic to talk")
        caption.setStyleSheet("font-size: 13px; color: %s;" % theme.TEXT_SUBTLE)
        self.sub = QLabel(VOICE_STATES["idle"])
        self.sub.setStyleSheet("font-size: 11px; color: %s;" % theme.TEXT_FAINT)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addStretch(3)
        layout.addLayout(columns)
        layout.addStretch(2)
        layout.addWidget(self.mic, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(10)
        layout.addWidget(caption, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(2)
        layout.addWidget(self.sub, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(22)

    def set_voice_state(self, state: str) -> None:
        """Swap the caption under the mic: idle / recording / transcribing."""
        self.sub.setText(VOICE_STATES.get(state, VOICE_STATES["idle"]))

    def _tick(self) -> None:
        now = datetime.now()
        self.clock.setText(now.strftime("%-I:%M %p"))
        self.date.setText(now.strftime("%A, %B %-d"))
