"""Custom painted widgets: the glow background, the Jarvis orb, status dots,
the push-to-talk mic button, and the Enter-to-send composer."""

from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPen, QPixmap, QRadialGradient
from PyQt6.QtWidgets import QPlainTextEdit, QPushButton, QWidget

from . import theme


class GlowRoot(QWidget):
    """Window background: near-black ink with the Portal's warm radial glows."""

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        painter.fillRect(rect, QColor(theme.INK))

        # Bottom-center warm glow (the Portal's ambient identity).
        bottom = QRadialGradient(
            QPointF(rect.width() * 0.5, rect.height() * 1.08),
            rect.height() * 0.85,
        )
        bottom.setColorAt(0.0, theme.accent_qcolor(34))
        bottom.setColorAt(0.55, theme.accent_qcolor(12))
        bottom.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(rect, QBrush(bottom))

        # Faint top-left lift so the header never feels flat.
        top = QRadialGradient(QPointF(0, 0), rect.width() * 0.45)
        top.setColorAt(0.0, theme.accent_qcolor(12))
        top.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(rect, QBrush(top))


def make_orb_pixmap(size: int) -> QPixmap:
    """The glowing orange orb used in the header (and as the app icon)."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    center = QPointF(size / 2, size / 2)
    radius = size / 2 - 0.5

    glow = QRadialGradient(center, radius)
    glow.setColorAt(0.0, theme.accent_qcolor(255))
    glow.setColorAt(0.55, theme.accent_qcolor(190))
    glow.setColorAt(1.0, theme.accent_qcolor(0))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(glow))
    painter.drawEllipse(center, radius, radius)

    painter.setBrush(QColor(theme.mix(theme.ACCENT, "#FFFFFF", 0.6)))
    painter.drawEllipse(center, size * 0.17, size * 0.17)
    painter.end()
    return pixmap


def dot_icon(status: str) -> QPixmap:
    """A 12 px status dot for the model picker."""
    pixmap = QPixmap(12, 12)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(theme.STATUS_COLORS.get(status, theme.STATUS_COLORS["off"])))
    painter.drawEllipse(QRectF(1, 1, 10, 10))
    painter.end()
    return pixmap


class MicButton(QPushButton):
    """Round push-to-talk mic. Press-and-hold to record: the face turns red and
    the halo pulses while listening; while the transcript is being produced the
    halo keeps breathing in the theme accent. `size` scales the whole glyph so
    the same control works as the big home mic and the input-bar mic."""

    def __init__(self, parent=None, size: int = 92) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("background: transparent; border: none;")
        self._hover = False
        self._recording = False
        self._busy = False
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def busy(self) -> bool:
        return self._busy

    def set_recording(self, on: bool) -> None:
        if on == self._recording:
            return
        self._recording = on
        self._sync_timer()
        self.update()

    def set_busy(self, on: bool) -> None:
        if on == self._busy:
            return
        self._busy = on
        self._sync_timer()
        self.update()

    def _sync_timer(self) -> None:
        if self._recording or self._busy:
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()

    def _tick(self) -> None:  # noqa: N802 — animation frame while active
        self._phase = (self._phase + 0.14) % math.tau
        self.update()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        d = float(min(self.width(), self.height()))
        cx, cy = self.width() / 2.0, self.height() / 2.0
        r = d / 2 - 3
        pulse = (math.sin(self._phase) + 1.0) / 2.0

        # Soft ambient halo — red and breathing while recording, accent while
        # transcribing, plain accent otherwise.
        halo = QRadialGradient(QPointF(cx, cy), r * 1.35)
        if self._recording:
            halo.setColorAt(0.0, QColor(229, 57, 53, 55 + int(75 * pulse)))
        elif self._busy:
            halo.setColorAt(0.0, theme.accent_qcolor(40 + int(35 * pulse)))
        else:
            halo.setColorAt(0.0, theme.accent_qcolor(70 if self._hover else 45))
        halo.setColorAt(0.7, QColor(0, 0, 0, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(halo))
        painter.drawEllipse(QPointF(cx, cy), r * 1.35, r * 1.35)

        # Face: the Portal's orange gradient — red while recording.
        face = QLinearGradient(cx - r, cy - r, cx + r * 0.5, cy + r)
        if self._recording:
            face.setColorAt(0.0, QColor("#FF6D5A"))
            face.setColorAt(1.0, QColor("#B32418"))
        else:
            face.setColorAt(
                0.0,
                QColor(theme.ORANGE_LIGHT if self._hover else theme.mix(theme.ACCENT, "#FFFFFF", 0.28)),
            )
            face.setColorAt(1.0, QColor(theme.ORANGE_DEEP))
        painter.setBrush(QBrush(face))
        painter.drawEllipse(QPointF(cx, cy), r, r)

        # A thin live ring around the face while the mic is hot.
        if self._recording:
            ring = QPen(QColor(255, 120, 105, 170), max(1.5, d * 0.022))
            painter.setPen(ring)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(cx, cy), r + d * 0.02, r + d * 0.02)

        # Mic glyph.
        pen = QPen(QColor(255, 255, 255, 235), max(2.5, d * 0.045))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        cap_w, cap_h = d * 0.20, d * 0.30
        cap_top = cy - d * 0.28
        painter.setBrush(QColor(255, 255, 255, 235))
        painter.drawRoundedRect(
            QRectF(cx - cap_w / 2, cap_top, cap_w, cap_h), cap_w / 2, cap_w / 2
        )
        painter.setBrush(Qt.BrushStyle.NoBrush)
        arc_size = d * 0.40
        painter.drawArc(
            QRectF(cx - arc_size / 2, cy - d * 0.16, arc_size, arc_size),
            180 * 16,
            -180 * 16,
        )
        painter.drawLine(QPointF(cx, cy + d * 0.24), QPointF(cx, cy + d * 0.31))
        painter.drawLine(QPointF(cx - d * 0.12, cy + d * 0.31), QPointF(cx + d * 0.12, cy + d * 0.31))


class Composer(QPlainTextEdit):
    """Chat input: Enter sends, Shift+Enter makes a newline."""

    submitted = pyqtSignal()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        is_return = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if is_return and not shift:
            self.submitted.emit()
            event.accept()
            return
        super().keyPressEvent(event)
