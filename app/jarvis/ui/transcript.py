"""The conversation transcript — Portal-style turns: the user's line as a small
warm chip, Jarvis's answers as large light text, streaming in as tokens arrive."""

from __future__ import annotations

import html

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from . import theme


def _rich(text: str, streaming: bool = False) -> str:
    """Model text → simple readable prose (markdown is deliberately not rendered —
    upstream strips it for the same reason: these are spoken answers)."""
    safe = html.escape(text).replace("\n", "<br/>")
    if streaming and text:
        safe += " <span style='color:#8F8A84;'>▍</span>"
    return f"<div style='line-height:145%;'>{safe}</div>"


class TranscriptView(QWidget):
    """Scrolling turn list with follow-the-bottom behavior."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._current_row: QWidget | None = None
        self._current_label: QLabel | None = None

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.container = QWidget()
        self.vbox = QVBoxLayout(self.container)
        self.vbox.setContentsMargins(26, 8, 26, 16)
        self.vbox.setSpacing(10)
        self.vbox.addStretch(1)
        self.scroll.setWidget(self.container)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.scroll)

        self._follow = True
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scrolled)

    # ---- scroll behavior -------------------------------------------------

    def _on_scrolled(self, value: int) -> None:
        bar = self.scroll.verticalScrollBar()
        self._follow = value >= bar.maximum() - 8

    def _stick(self) -> None:
        if self._follow:
            QTimer.singleShot(0, self._scroll_to_bottom)

    def _scroll_to_bottom(self) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ---- turn builders ---------------------------------------------------

    def _insert(self, widget: QWidget) -> None:
        self.vbox.insertWidget(self.vbox.count() - 1, widget)

    def add_user(self, text: str) -> None:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.addStretch(1)

        chip = QFrame()
        chip.setObjectName("userchip")
        chip_layout = QHBoxLayout(chip)
        chip_layout.setContentsMargins(10, 6, 14, 6)

        label = QLabel("\u203a  " + text)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setFont(QFont("DejaVu Sans Mono", 10))
        label.setStyleSheet("color: %s;" % theme.USER_CHIP_TEXT)
        chip_layout.addWidget(label)

        layout.addWidget(chip)
        self._insert(row)
        self._stick()

    def _assistant_row(self, text: str, streaming: bool = False):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 2, 40, 2)

        label = QLabel()
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setStyleSheet("color: %s; font-size: 15px;" % theme.TEXT)
        label.setText(_rich(text, streaming))
        layout.addWidget(label, 1)
        return row, label

    def add_assistant(self, text: str) -> None:
        row, _ = self._assistant_row(text)
        self._insert(row)
        self._stick()

    def begin_assistant(self) -> None:
        row, label = self._assistant_row("", streaming=True)
        self._current_row, self._current_label = row, label
        self._insert(row)
        self._stick()

    def set_streaming_text(self, text: str) -> None:
        if self._current_label is not None:
            self._current_label.setText(_rich(text, streaming=True))
            self._stick()

    def finish_streaming(self, text: str) -> None:
        """End the live bubble: keep it with the full text (or remove it if empty)."""
        if self._current_label is not None:
            if text:
                self._current_label.setText(_rich(text))
            elif self._current_row is not None:
                self._current_row.setParent(None)
                self._current_row.deleteLater()
        self._current_row = None
        self._current_label = None
        self._stick()

    def add_notice(self, text: str, color: str = theme.NOTICE_TEXT) -> None:
        """A small centered line — UI-only, never part of the conversation sent to the model."""
        label = QLabel(text)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("color: %s; font-size: 13px;" % color)
        self._insert(label)
        self._stick()

    def add_tool_note(self, text: str) -> None:
        """One quiet left-aligned machinery line — which read-only tool ran and
        what it returned. Display-only: never part of the model's spoken reply,
        never sent back to the model."""
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setStyleSheet("color: %s; font-size: 12px;" % theme.TEXT_FAINT)
        self._insert(label)
        self._stick()

    def clear(self) -> None:
        while self.vbox.count() > 1:
            item = self.vbox.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._current_row = None
        self._current_label = None
        self._follow = True
