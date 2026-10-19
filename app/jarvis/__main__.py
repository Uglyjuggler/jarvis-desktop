"""Entry point — run with `python3 -m jarvis` (or ./run.sh from the project root).

Config (assistant name/greeting, voice, theme) is read first: the window title,
palette and stylesheet all derive from `appdata/config.json`.
"""

from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication

from . import __version__, config
from .ui import theme
from .ui.window import JarvisWindow


def main() -> int:
    app = QApplication(sys.argv)
    cfg = config.load()
    name = cfg["assistant"]["name"]
    theme.apply_overrides(cfg.get("theme"))
    app.setApplicationName(name)
    app.setApplicationDisplayName(name)
    app.setApplicationVersion(__version__)
    app.setStyle("Fusion")
    app.setPalette(theme.dark_palette())
    app.setStyleSheet(theme.global_qss())

    window = JarvisWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
