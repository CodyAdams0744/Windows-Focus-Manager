"""tray.py — Qt system-tray icon + bento flyout for Windows Focus Manager.

Runs on the main thread (owns the Qt event loop); the tiler runs on a
background thread.  Left-click the tray icon → a dark bento popup with quick
actions (Pause/Resume, Open Settings, Hover mode, Exit).  Right-click → a
simple fallback menu.
"""

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QSize, QPointF, QRectF, QEvent
from PySide6.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QPen, QPolygonF, QCursor,
)
from PySide6.QtWidgets import (
    QApplication, QSystemTrayIcon, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QPushButton, QMenu, QFrame,
)

import config_io

ACCENT = "#a78bfa"
ACCENT_BRIGHT = "#c4b5fd"
OK_GREEN = "#7ee0a8"
WARN_AMBER = "#ffd166"
DANGER = "#ff8d8d"

FLYOUT_QSS = """
#FlyoutCard { background: #14121c; border: 1px solid rgba(167,139,250,0.20);
              border-radius: 14px; }
QLabel { background: transparent; color: rgba(248,247,255,0.92); }
#FTitle  { font-size: 13px; font-weight: 600; }
#FStatus { font-size: 11px; }
#Action {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.10);
    border-radius: 10px; padding: 10px 12px; text-align: left;
    color: rgba(248,247,255,0.92); font-size: 12px;
}
#Action:hover   { background: rgba(167,139,250,0.14); border-color: rgba(167,139,250,0.30); }
#Action:checked { background: rgba(167,139,250,0.20); border-color: #a78bfa; }
#ActionExit       { background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.10);
                    border-radius: 10px; padding: 10px 12px; text-align: left;
                    color: #ff8d8d; font-size: 12px; }
#ActionExit:hover { background: rgba(255,141,141,0.12); border-color: rgba(255,141,141,0.40); }
QMenu { background: #161421; border: 1px solid rgba(167,139,250,0.22); border-radius: 8px;
        color: rgba(248,247,255,0.92); padding: 4px; }
QMenu::item { padding: 6px 18px; border-radius: 6px; }
QMenu::item:selected { background: rgba(167,139,250,0.18); }
"""


# ---------------------------------------------------------------------------
# Icon drawing
# ---------------------------------------------------------------------------

def _tray_icon() -> QIcon:
    """Light 4-tile mark for the taskbar (visible on a dark tray)."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    for (x, y, col) in ((8, 8, ACCENT), (34, 8, "#cfc6f5"),
                        (8, 34, "#cfc6f5"), (34, 34, "#9a8fd0")):
        p.setBrush(QColor(col))
        p.drawRoundedRect(x, y, 22, 22, 5, 5)
    p.end()
    return QIcon(pm)


def _glyph(name: str, color: str) -> QIcon:
    pm = QPixmap(18, 18)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color))
    pen.setWidthF(1.5)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    if name == "pause":
        p.drawLine(6, 4, 6, 14)
        p.drawLine(12, 4, 12, 14)
    elif name == "play":
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawPolygon(QPolygonF([QPointF(6, 4), QPointF(14, 9), QPointF(6, 14)]))
    elif name == "settings":
        p.drawLine(3, 6, 15, 6)
        p.drawLine(3, 12, 15, 12)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawEllipse(QPointF(11, 6), 2.2, 2.2)
        p.drawEllipse(QPointF(7, 12), 2.2, 2.2)
    elif name == "cursor":
        p.drawLine(14, 4, 6, 13)
        p.drawLine(6, 13, 6, 9)
        p.drawLine(6, 13, 10, 13)
    elif name == "power":
        p.drawArc(QRectF(4, 4, 10, 10), 120 * 16, 300 * 16)
        p.drawLine(9, 3, 9, 9)
    p.end()
    return QIcon(pm)


def _launch_settings() -> None:
    try:
        if getattr(sys, "frozen", False):
            args = [sys.executable, "--settings"]
        else:
            args = [sys.executable, str(Path(__file__).with_name("settings_window.py"))]
        subprocess.Popen(args, close_fds=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Flyout
# ---------------------------------------------------------------------------

class Flyout(QWidget):
    def __init__(self, manager, log) -> None:
        super().__init__()
        self.manager = manager
        self.log = log
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool |
                            Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(FLYOUT_QSS)
        self.setFixedWidth(304)
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("FlyoutCard")
        outer.addWidget(card)
        cv = QVBoxLayout(card)
        cv.setContentsMargins(16, 14, 16, 16)
        cv.setSpacing(14)

        # Header.
        head = QHBoxLayout()
        head.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(_tray_icon().pixmap(26, 26))
        head.addWidget(logo)
        titles = QVBoxLayout()
        titles.setSpacing(1)
        t = QLabel("Focus Manager")
        t.setObjectName("FTitle")
        titles.addWidget(t)
        self.status_lbl = QLabel("● Tiling active")
        self.status_lbl.setObjectName("FStatus")
        titles.addWidget(self.status_lbl)
        head.addLayout(titles)
        head.addStretch(1)
        cv.addLayout(head)

        # 2x2 action grid.
        grid = QGridLayout()
        grid.setSpacing(8)
        self.pause_btn = self._action("Pause tiling", "pause")
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.settings_btn = self._action("Open settings", "settings")
        self.settings_btn.clicked.connect(self._open_settings)
        self.hover_btn = self._action("Hover mode", "cursor", checkable=True)
        self.hover_btn.clicked.connect(self._toggle_hover)
        self.exit_btn = self._action("Exit", "power", danger=True)
        self.exit_btn.clicked.connect(self._exit)
        grid.addWidget(self.pause_btn, 0, 0)
        grid.addWidget(self.settings_btn, 0, 1)
        grid.addWidget(self.hover_btn, 1, 0)
        grid.addWidget(self.exit_btn, 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        cv.addLayout(grid)

    def _action(self, text: str, icon: str, checkable: bool = False,
                danger: bool = False) -> QPushButton:
        b = QPushButton("  " + text)
        b.setObjectName("ActionExit" if danger else "Action")
        b.setIcon(_glyph(icon, DANGER if danger else ACCENT_BRIGHT))
        b.setIconSize(QSize(16, 16))
        b.setCheckable(checkable)
        b.setMinimumHeight(56)
        b.setCursor(Qt.PointingHandCursor)
        return b

    # ── state ───────────────────────────────────────────────────────────
    def refresh(self) -> None:
        paused = bool(getattr(self.manager, "_paused", False))
        self.status_lbl.setText("● Paused" if paused else "● Tiling active")
        self.status_lbl.setStyleSheet(
            f"color: {WARN_AMBER};" if paused else f"color: {OK_GREEN};")
        self.pause_btn.setText("  Resume tiling" if paused else "  Pause tiling")
        self.pause_btn.setIcon(_glyph("play" if paused else "pause", ACCENT_BRIGHT))
        hover = bool(config_io.load_config().get("hover_enabled", False))
        self.hover_btn.setChecked(hover)

    def show_near_cursor(self) -> None:
        self.adjustSize()
        pos = QCursor.pos()
        screen = QApplication.screenAt(pos) or QApplication.primaryScreen()
        avail = screen.availableGeometry()
        w, h = self.width(), self.height()
        x = min(max(avail.left() + 8, pos.x() - w + 24), avail.right() - w - 8)
        y = avail.bottom() - h - 8
        self.move(int(x), int(y))
        self.show()
        self.raise_()
        self.activateWindow()

    def event(self, e) -> bool:
        if e.type() == QEvent.WindowDeactivate:
            self.hide()
        return super().event(e)

    # ── actions ─────────────────────────────────────────────────────────
    def _toggle_pause(self) -> None:
        try:
            if getattr(self.manager, "_paused", False):
                self.manager.resume()
            else:
                self.manager.pause()
        except Exception as exc:
            self.log.error("Pause/resume failed: %s", exc)
        self.refresh()

    def _open_settings(self) -> None:
        self.hide()
        _launch_settings()

    def _toggle_hover(self) -> None:
        try:
            cfg = config_io.load_config()
            cfg["hover_enabled"] = not cfg.get("hover_enabled", False)
            config_io.save_config(cfg)
        except Exception as exc:
            self.log.error("Hover toggle failed: %s", exc)
        self.refresh()

    def _exit(self) -> None:
        self.log.info("Exit requested from tray.")
        try:
            self.manager.pause()
        except Exception:
            pass
        os._exit(0)


# ---------------------------------------------------------------------------
# Entry point (call on the main thread)
# ---------------------------------------------------------------------------

def run_tray(manager, log) -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.error("System tray not available on this system.")

    flyout = Flyout(manager, log)

    tray = QSystemTrayIcon(_tray_icon())
    tray.setToolTip("Window Focus Manager")

    menu = QMenu()
    menu.setStyleSheet(FLYOUT_QSS)
    menu.addAction("Open Settings", flyout._open_settings)
    menu.addAction("Pause / Resume", flyout._toggle_pause)
    menu.addSeparator()
    menu.addAction("Exit", flyout._exit)
    tray.setContextMenu(menu)

    def _activated(reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            flyout.refresh()
            flyout.show_near_cursor()

    tray.activated.connect(_activated)
    tray.show()
    log.info("System tray (Qt) started.")
    app.exec()
