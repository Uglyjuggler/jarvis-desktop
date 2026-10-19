"""The built-in Model Switcher panel.

Same semantics as /mnt/PROJECTS/model-switcher/model-switcher.py, restyled for
the Jarvis window: status light + status word + Start/Stop per model, with the
switcher's exact enable rules (Start off while running/waking; Stop available
while running, waking or failed; both on when failed; off otherwise). Starting
is exclusive — the app never starts anything by itself.
"""

from __future__ import annotations

from functools import partial

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from ..models import MODELS
from . import theme

# Status colors and words — the Model Switcher's values (its rows, read 2026-10-18).
STATE_COLORS = {
    "active": "#4CAF50",
    "activating": "#FFB300",
    "deactivating": "#FFB300",
    "failed": "#E53935",
    "inactive": "#777777",
    "unknown": "#777777",
}
STATE_WORDS = {
    "active": "Running",
    "activating": "Waking up…",
    "deactivating": "Stopping…",
    "failed": "Failed — try Stop, then Start",
    "inactive": "Off",
    "unknown": "Off",
}


class ModelRow(QFrame):
    start_clicked = pyqtSignal(str)  # unit
    stop_clicked = pyqtSignal(str)

    def __init__(self, model, parent=None) -> None:
        super().__init__(parent)
        self.model = model

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 10, 0, 10)
        layout.setSpacing(14)

        # Status light (same ● approach as the Model Switcher, restyled).
        self.dot = QLabel("●")
        self.dot.setFixedWidth(18)
        self.dot.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.dot)

        # Name / tagline / description / size.
        text_box = QVBoxLayout()
        text_box.setSpacing(2)
        name_line = QHBoxLayout()
        name_line.setSpacing(8)
        name = QLabel(model.name)
        name.setStyleSheet("font-size: 14px; font-weight: 600;")
        name_line.addWidget(name)
        tagline = QLabel(model.tagline)
        tagline.setStyleSheet("color: %s; font-size: 11px;" % theme.TEXT_SUBTLE)
        name_line.addWidget(tagline)
        name_line.addStretch(1)
        text_box.addLayout(name_line)

        desc = QLabel(model.desc)
        desc.setWordWrap(True)
        desc.setStyleSheet("color: %s; font-size: 12px;" % theme.TEXT_SUBTLE)
        text_box.addWidget(desc)

        size = QLabel(model.size)
        size.setStyleSheet("color: %s; font-size: 11px;" % theme.TEXT_FAINT)
        text_box.addWidget(size)
        layout.addLayout(text_box, 1)

        # Status word + buttons.
        self.status = QLabel("…")
        self.status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.status.setMinimumWidth(175)
        layout.addWidget(self.status)

        self.start_btn = QPushButton("Start")
        self.start_btn.setObjectName("startbtn")
        self.start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_btn.setMinimumWidth(84)
        self.start_btn.clicked.connect(partial(self.start_clicked.emit, model.unit))
        layout.addWidget(self.start_btn)

        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("stopbtn")
        self.stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_btn.setMinimumWidth(84)
        self.stop_btn.clicked.connect(partial(self.stop_clicked.emit, model.unit))
        layout.addWidget(self.stop_btn)

        self.refresh("unknown")

    def refresh(self, state: str) -> None:
        """The Model Switcher's enable rules, verbatim in spirit."""
        color = STATE_COLORS.get(state, STATE_COLORS["unknown"])
        self.dot.setStyleSheet("color: %s; font-size: 15px;" % color)
        self.status.setText(STATE_WORDS.get(state, "Off"))
        self.status.setStyleSheet(
            "color: %s; font-size: 11px; font-weight: 600;" % color
        )
        if state in ("active", "activating"):
            self.start_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
        elif state == "failed":
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(True)
        else:  # inactive / deactivating / unknown
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)


class ModelsPanel(QFrame):
    """Card with one row per model; emits user intent, never starts by itself."""

    start_requested = pyqtSignal(str)  # unit
    stop_requested = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("modelspanel")
        self._busy = False
        self._states: dict[str, str] = {m.unit: "unknown" for m in MODELS}
        self.rows: dict[str, ModelRow] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 12)
        layout.setSpacing(0)

        head = QHBoxLayout()
        title = QLabel("Local models")
        title.setStyleSheet("font-size: 14px; font-weight: 600;")
        head.addWidget(title)
        head.addStretch(1)
        hint = QLabel(
            "One graphics card — starting a model stops the others first. "
            "Models stay off until you start one."
        )
        hint.setStyleSheet("color: %s; font-size: 11px;" % theme.TEXT_FAINT)
        head.addWidget(hint)
        layout.addLayout(head)
        layout.addSpacing(4)

        for index, model in enumerate(MODELS):
            if index:
                separator = QFrame()
                separator.setFixedHeight(1)
                separator.setStyleSheet("background: rgba(241, 236, 230, 14);")
                layout.addWidget(separator)
            row = ModelRow(model)
            self.rows[model.unit] = row
            layout.addWidget(row)
            row.start_clicked.connect(self.start_requested)
            row.stop_clicked.connect(self.stop_requested)

        self.foot = QLabel("")
        self.foot.setWordWrap(True)
        self.foot.setStyleSheet("color: %s; font-size: 11px;" % theme.TEXT_SUBTLE)
        layout.addSpacing(2)
        layout.addWidget(self.foot)

    # ---- updates ----------------------------------------------------------

    def set_states(self, states: dict) -> None:
        self._states = states
        self._apply()

    def set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        if message:
            self.foot.setText(message)
        self._apply()

    def set_foot(self, text: str) -> None:
        self.foot.setText(text)

    def _apply(self) -> None:
        for unit, row in self.rows.items():
            row.refresh(self._states.get(unit, "unknown"))
            if self._busy:
                # One operation at a time: freeze every control while it runs.
                row.start_btn.setEnabled(False)
                row.stop_btn.setEnabled(False)
