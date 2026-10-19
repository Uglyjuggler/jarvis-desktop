"""Portal Jarvis visual identity — colors, palette and the global stylesheet.

Values mirrored from the upstream Compose theme (portal-assistant
`ui/theme/Theme.kt`): accent orange #FF8A00 (the recording-bar signature),
ink background #0A0A0C, user-turn chip #3A332B, body text #F1ECE6, gradient
#FFB74D → #FF6D00, secondary text at the raised legible alpha.

The accent and background are **themeable**: `apply_overrides()` retunes them
from `appdata/config.json` at startup, and the stylesheet is recomputed from
the live values. Everything else (text greys, chip, borders) keeps its
hand-tuned contrast against the near-black ink.
"""

from __future__ import annotations

from PyQt6.QtGui import QColor, QPalette

ACCENT = "#FF8A00"
ORANGE_LIGHT = "#FFB74D"
ORANGE_DEEP = "#FF6D00"
INK = "#0A0A0C"
USER_CHIP = "#3A332B"
USER_CHIP_TEXT = "#DCD3C8"
TEXT = "#F1ECE6"
TEXT_SUBTLE = "#C4BFB9"   # upstream SecondaryAlpha 0.80 — readable across the room
TEXT_FAINT = "#8F8A84"
PANEL = "#131316"
INPUT_BAR = "#101014"
BORDER = "#26262A"
NOTICE_TEXT = "#FFC069"

STATUS_COLORS = {"running": "#4CAF50", "loading": "#FFB300", "off": "#777777"}
STATUS_WORDS = {"running": "Running", "loading": "Waking up…", "off": "Off"}


# ---- theme overrides + color helpers ---------------------------------------

def _valid(value: object, fallback: str) -> str:
    if isinstance(value, str) and QColor(value).isValid():
        return value
    return fallback


def apply_overrides(theme_cfg: dict | None = None) -> None:
    """Retune accent/background from config. Call before building the stylesheet."""
    global ACCENT, ORANGE_LIGHT, ORANGE_DEEP, INK
    if not isinstance(theme_cfg, dict):
        return
    ACCENT = _valid(theme_cfg.get("accent"), ACCENT)
    ORANGE_LIGHT = _valid(theme_cfg.get("accent_light"), ORANGE_LIGHT)
    ORANGE_DEEP = _valid(theme_cfg.get("accent_deep"), ORANGE_DEEP)
    INK = _valid(theme_cfg.get("background"), INK)


def rgba(color: str, alpha: int) -> str:
    """`rgba(r, g, b, a)` for QSS, from any #hex color."""
    c = QColor(color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {int(alpha)})"


def mix(color_a: str, color_b: str, t: float) -> str:
    """Blend a→b by t (0..1); used to derive hover shades from the accent."""
    a, b = QColor(color_a), QColor(color_b)
    r = round(a.red() + (b.red() - a.red()) * t)
    g = round(a.green() + (b.green() - a.green()) * t)
    bl = round(a.blue() + (b.blue() - a.blue()) * t)
    return f"#{r:02X}{g:02X}{bl:02X}"


def accent_qcolor(alpha: int = 255) -> QColor:
    """The live accent as a QColor with alpha — for custom painting."""
    c = QColor(ACCENT)
    c.setAlpha(int(alpha))
    return c


def accent_hover() -> str:
    """A slightly raised accent for hover states."""
    return mix(ACCENT, "#FFFFFF", 0.16)


def global_qss() -> str:
    """App-wide stylesheet. Pages stay transparent so the window's glow shows through."""
    light = accent_hover()
    return f"""
    QMainWindow {{ background: {INK}; }}
    QWidget {{ background: transparent; color: {TEXT}; font-family: "DejaVu Sans"; }}
    QLabel {{ background: transparent; }}
    QToolTip {{ background: {PANEL}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px; }}

    QDialog {{ background: {INK}; }}
    QFrame#settingcard {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 12px; }}
    QCheckBox {{ color: {TEXT}; font-size: 13px; spacing: 8px; }}
    QCheckBox::indicator {{
        width: 16px; height: 16px; border-radius: 5px;
        border: 1px solid {BORDER}; background: {INPUT_BAR};
    }}
    QCheckBox::indicator:hover {{ border-color: {rgba(ACCENT, 170)}; }}
    QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
    QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: {rgba(ACCENT, 190)}; border-radius: 2px; }}
    QSlider::add-page:horizontal {{ background: {BORDER}; border-radius: 2px; }}
    QSlider::handle:horizontal {{
        width: 14px; height: 14px; margin: -6px 0; border-radius: 7px;
        background: {ACCENT};
    }}
    QSlider::handle:horizontal:hover {{ background: {light}; }}

    QComboBox {{
        background: {PANEL}; border: 1px solid {BORDER}; border-radius: 10px;
        padding: 5px 10px; color: {TEXT}; font-size: 13px;
    }}
    QComboBox:hover {{ border-color: #4A4A4E; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox QAbstractItemView {{
        background: {PANEL}; color: {TEXT}; border: 1px solid {BORDER};
        selection-background-color: {USER_CHIP}; selection-color: {TEXT}; outline: none;
    }}

    QPushButton#ghost {{
        background: transparent; border: 1px solid {rgba(TEXT, 60)};
        border-radius: 10px; padding: 6px 14px; color: {TEXT_SUBTLE}; font-size: 13px;
    }}
    QPushButton#ghost:hover {{ border-color: {rgba(ACCENT, 140)}; color: {TEXT}; }}
    QPushButton#ghost:checked {{
        border-color: {rgba(ACCENT, 150)}; color: {TEXT};
        background: {rgba(ACCENT, 18)};
    }}
    QPushButton#ghost:disabled {{ color: {TEXT_FAINT}; border-color: {rgba(TEXT, 30)}; }}

    QPushButton#chip {{
        background: {rgba(ACCENT, 18)}; border: 1px solid {rgba(ACCENT, 90)};
        border-radius: 17px; padding: 8px 18px; color: {TEXT}; font-size: 13px;
    }}
    QPushButton#chip:hover {{ background: {rgba(ACCENT, 45)}; border-color: {rgba(ACCENT, 160)}; }}
    QPushButton#chip:pressed {{ background: {rgba(ACCENT, 70)}; }}

    QPushButton#send {{
        background: {ACCENT}; color: {INK}; border: none; border-radius: 10px;
        padding: 10px 24px; font-size: 14px; font-weight: 600;
    }}
    QPushButton#send:hover {{ background: {light}; }}
    QPushButton#send:disabled {{ background: {rgba(ACCENT, 55)}; color: {rgba(INK, 140)}; }}

    QPlainTextEdit#composer {{
        background: {PANEL}; border: 1px solid {BORDER}; border-radius: 12px;
        padding: 8px 10px; color: {TEXT}; font-size: 14px;
        selection-background-color: {ACCENT}; selection-color: {INK};
    }}
    QPlainTextEdit#composer:focus {{ border: 1px solid {rgba(ACCENT, 150)}; }}

    QFrame#inputbar {{ background: {INPUT_BAR}; border-top: 1px solid #232327; }}
    QFrame#userchip {{ background: {USER_CHIP}; border-radius: 10px; }}
    QFrame#notice {{
        background: {rgba(ACCENT, 26)}; border: 1px solid {rgba(ACCENT, 60)};
        border-radius: 10px;
    }}

    QFrame#modelspanel {{
        background: {PANEL}; border: 1px solid {BORDER}; border-radius: 12px;
    }}

    QPushButton#startbtn {{
        background: {ACCENT}; color: {INK}; border: none; border-radius: 9px;
        padding: 7px 16px; font-size: 13px; font-weight: 600;
    }}
    QPushButton#startbtn:hover {{ background: {light}; }}
    QPushButton#startbtn:disabled {{ background: {rgba(ACCENT, 50)}; color: {rgba(INK, 130)}; }}

    QPushButton#stopbtn {{
        background: transparent; border: 1px solid {rgba(TEXT, 60)};
        border-radius: 9px; padding: 7px 16px; color: {TEXT_SUBTLE}; font-size: 13px;
    }}
    QPushButton#stopbtn:hover {{ border-color: rgba(229, 57, 53, 170); color: #FF8A80; }}
    QPushButton#stopbtn:disabled {{ color: {TEXT_FAINT}; border-color: {rgba(TEXT, 25)}; }}

    QScrollArea {{ border: none; background: transparent; }}
    QStatusBar {{
        background: {INPUT_BAR}; color: {TEXT_SUBTLE}; font-size: 12px;
        border-top: 1px solid #232327;
    }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px 4px 2px; }}
    QScrollBar::handle:vertical {{ background: {rgba(TEXT, 40)}; border-radius: 4px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {rgba(ACCENT, 130)}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
    """


def dark_palette() -> QPalette:
    """Fusion dark palette — same approach as the Model Switcher, so stray
    native widgets (menus, message boxes) match the app."""
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor("#2e2e2e"))
    pal.setColor(QPalette.ColorRole.WindowText, QColor("#e8e8e8"))
    pal.setColor(QPalette.ColorRole.Base, QColor("#1c1c1e"))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor("#26262a"))
    pal.setColor(QPalette.ColorRole.Text, QColor("#e8e8e8"))
    pal.setColor(QPalette.ColorRole.Button, QColor("#383838"))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor("#e8e8e8"))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(INK))
    return pal
