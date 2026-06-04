"""settings_window.py — native PySide6 settings window for Windows Focus Manager.

A dark, sidebar-navigated desktop window.  Replaces the old localhost HTTP
server + browser UI: it reads and writes config.json through config_io, and
the running tiler live-reloads saved changes — no restart, no open port.

Layout: a left nav (QListWidget) switches a QStackedWidget of pages.  Each
page edits one group of settings.  The Save button at the bottom commits
everything to config.json at once.

Run standalone for development (instant feedback, no rebuild):
    python settings_window.py
"""

import sys

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QBrush
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QSpinBox, QComboBox, QPushButton, QLineEdit, QSlider,
    QListWidget, QListWidgetItem, QFrame, QStackedWidget, QSizePolicy,
)

import config_io

# ---------------------------------------------------------------------------
# Dark theme
# ---------------------------------------------------------------------------

STYLE = """
QWidget { background: #0e0e0e; color: rgba(255,255,255,0.92);
          font-family: 'Segoe UI', system-ui, sans-serif; font-size: 13px; }

#Header { border-bottom: 1px solid rgba(255,255,255,0.08); }
#HeaderTitle { color: rgba(255,255,255,0.60); font-size: 13px; }
#StatusDot { font-size: 18px; }

#Nav { background: #101010; border: none;
       border-right: 1px solid rgba(255,255,255,0.07); outline: 0; }
#Nav::item { padding: 9px 12px; margin: 1px 6px; border-radius: 7px;
             color: rgba(255,255,255,0.50); }
#Nav::item:selected { background: rgba(255,255,255,0.09); color: rgba(255,255,255,0.92); }
#Nav::item:hover:!selected { background: rgba(255,255,255,0.05); }

#PageTitle { color: rgba(255,255,255,0.40); font-size: 10px;
             font-weight: 700; letter-spacing: 2px; }
#Sub  { color: rgba(255,255,255,0.40); font-size: 11px; }
#Hint { color: rgba(255,255,255,0.40); font-size: 11px; }
#Value { color: rgba(255,255,255,0.90); font-family: 'Cascadia Code','Consolas',monospace; font-size: 14px; }
#RowLine { color: rgba(255,255,255,0.10); }

QSlider::groove:horizontal { height: 4px; background: rgba(255,255,255,0.14); border-radius: 2px; }
QSlider::sub-page:horizontal { background: rgba(255,255,255,0.85); border-radius: 2px; }
QSlider::add-page:horizontal { background: rgba(255,255,255,0.10); border-radius: 2px; }
QSlider::handle:horizontal { width: 15px; height: 15px; margin: -6px 0; border-radius: 8px;
                             background: #ffffff; }

QSpinBox, QComboBox, QLineEdit {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.14);
    border-radius: 7px; padding: 5px 8px; min-height: 20px;
    selection-background-color: rgba(255,255,255,0.18);
}
QSpinBox:focus, QComboBox:focus, QLineEdit:focus { border-color: rgba(255,255,255,0.34); }
QComboBox QAbstractItemView { background: #161616; border: 1px solid rgba(255,255,255,0.14);
                              selection-background-color: rgba(255,255,255,0.12); }

QPushButton { background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.14);
              border-radius: 7px; padding: 6px 16px; }
QPushButton:hover  { background: rgba(255,255,255,0.10); border-color: rgba(255,255,255,0.26); }
QPushButton:pressed { background: rgba(255,255,255,0.04); }

#SaveBtn { font-weight: 600; }
#SaveBtn[saved="true"] { background: rgba(255,255,255,0.90); color: #0a0a0a; border-color: transparent; }

#MonTile { padding: 12px 10px; border-radius: 9px; text-align: center;
           background: rgba(255,255,255,0.05); }
#MonTile:checked { background: rgba(255,255,255,0.10); border-color: rgba(255,255,255,0.32); }

#ExclList { background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.10);
            border-radius: 7px; }
#Footer { border-top: 1px solid rgba(255,255,255,0.08); }
#Status { color: rgba(255,255,255,0.45); font-size: 12px; }
"""

NAV = ["Layout", "Animation", "Hover", "Monitors", "Exclusions", "System"]


# ---------------------------------------------------------------------------
# Custom iOS-style toggle switch (Qt has no native one)
# ---------------------------------------------------------------------------

class ToggleSwitch(QWidget):
    toggled = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self._checked = False
        self.setFixedSize(44, 24)
        self.setCursor(Qt.PointingHandCursor)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, value: bool) -> None:
        """Set state WITHOUT emitting (used when loading config)."""
        self._checked = bool(value)
        self.update()

    def mousePressEvent(self, _event) -> None:
        if not self.isEnabled():
            return
        self._checked = not self._checked
        self.update()
        self.toggled.emit(self._checked)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        on = self._checked
        op = 1.0 if self.isEnabled() else 0.35
        p.setOpacity(op)
        p.setBrush(QBrush(QColor(255, 255, 255, 224) if on else QColor(255, 255, 255, 26)))
        p.drawRoundedRect(0, 0, 44, 24, 12, 12)
        p.setBrush(QBrush(QColor(17, 17, 17) if on else QColor(255, 255, 255, 150)))
        p.drawEllipse(23 if on else 3, 3, 18, 18)
        p.end()


# ---------------------------------------------------------------------------
# Small layout helpers
# ---------------------------------------------------------------------------

def _hline() -> QFrame:
    f = QFrame()
    f.setObjectName("RowLine")
    f.setFrameShape(QFrame.HLine)
    f.setFixedHeight(1)
    f.setStyleSheet("background: rgba(255,255,255,0.06); border: none;")
    return f


def _control_row(label: str, widget: QWidget, sub: str = "") -> QWidget:
    row = QWidget()
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 8, 0, 8)
    left = QVBoxLayout()
    left.setSpacing(1)
    left.addWidget(QLabel(label))
    if sub:
        s = QLabel(sub)
        s.setObjectName("Sub")
        left.addWidget(s)
    h.addLayout(left)
    h.addStretch(1)
    h.addWidget(widget)
    return row


def _spin(lo: int, hi: int, step: int, suffix: str = "") -> QSpinBox:
    sp = QSpinBox()
    sp.setRange(lo, hi)
    sp.setSingleStep(step)
    if suffix:
        sp.setSuffix(suffix)
    sp.setFixedWidth(110)
    sp.setAlignment(Qt.AlignRight)
    return sp


class SliderRow(QWidget):
    """A labelled slider with a live monospace readout on the right."""

    def __init__(self, label: str, lo: int, hi: int, suffix: str = "",
                 sub: str = "", scale: float = 1.0) -> None:
        super().__init__()
        self._suffix = suffix
        self._scale = scale          # display value = slider value * scale
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 8, 0, 8)
        v.setSpacing(7)
        top = QHBoxLayout()
        top.addWidget(QLabel(label))
        top.addStretch(1)
        self.value_lbl = QLabel("")
        self.value_lbl.setObjectName("Value")
        top.addWidget(self.value_lbl)
        v.addLayout(top)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(lo, hi)
        self.slider.valueChanged.connect(self._update_label)
        v.addWidget(self.slider)
        if sub:
            s = QLabel(sub)
            s.setObjectName("Sub")
            v.addWidget(s)

    def _update_label(self, raw: int) -> None:
        disp = raw * self._scale
        text = f"{disp:.0f}" if self._scale == 1.0 else f"{disp:.0f}"
        self.value_lbl.setText(f"{text}{self._suffix}")

    def setValue(self, raw: int) -> None:
        self.slider.setValue(int(raw))
        self._update_label(int(raw))

    def value(self) -> int:
        return self.slider.value()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class SettingsWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.cfg = config_io.load_config()
        self.monitors = config_io.get_monitors()
        self.enabled_monitors = self.cfg.get("enabled_monitors") or None
        self.skip_titles = list(self.cfg.get("skip_titles", []))
        self.skip_exe = list(self.cfg.get("skip_exe", []))
        self.frozen = config_io.is_frozen()

        self.setWindowTitle("Window Focus Manager — Settings")
        self.setMinimumSize(640, 480)
        self.resize(680, 520)
        self.setStyleSheet(STYLE)
        self.setWindowIcon(_app_icon())

        self._build_ui()
        self._load_values()

    # ── UI ──────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())

        mid = QHBoxLayout()
        mid.setContentsMargins(0, 0, 0, 0)
        mid.setSpacing(0)

        self.nav = QListWidget()
        self.nav.setObjectName("Nav")
        self.nav.setFixedWidth(160)
        for name in NAV:
            self.nav.addItem(QListWidgetItem(name))
        self.nav.currentRowChanged.connect(self._on_nav)
        mid.addWidget(self.nav)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._page_layout())
        self.stack.addWidget(self._page_animation())
        self.stack.addWidget(self._page_hover())
        self.stack.addWidget(self._page_monitors())
        self.stack.addWidget(self._page_exclusions())
        self.stack.addWidget(self._page_system())
        mid.addWidget(self.stack, 1)

        root.addLayout(mid, 1)
        root.addWidget(self._build_footer())

        self.nav.setCurrentRow(0)

    def _build_header(self) -> QWidget:
        head = QWidget()
        head.setObjectName("Header")
        head.setFixedHeight(52)
        h = QHBoxLayout(head)
        h.setContentsMargins(18, 0, 18, 0)
        h.setSpacing(11)
        mark = QLabel()
        mark.setPixmap(_app_icon().pixmap(26, 26))
        h.addWidget(mark)
        title = QLabel("Window Focus Manager")
        title.setObjectName("HeaderTitle")
        h.addWidget(title)
        h.addStretch(1)
        return head

    def _build_footer(self) -> QWidget:
        foot = QWidget()
        foot.setObjectName("Footer")
        fh = QHBoxLayout(foot)
        fh.setContentsMargins(20, 12, 20, 14)
        self.status = QLabel("")
        self.status.setObjectName("Status")
        fh.addWidget(self.status)
        fh.addStretch(1)
        self.save_btn = QPushButton("Save")
        self.save_btn.setObjectName("SaveBtn")
        self.save_btn.clicked.connect(self._save)
        fh.addWidget(self.save_btn)
        return foot

    def _on_nav(self, row: int) -> None:
        if row >= 0:
            self.stack.setCurrentIndex(row)

    def _page(self, title: str) -> tuple:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(26, 22, 26, 22)
        outer.setSpacing(4)
        t = QLabel(title.upper())
        t.setObjectName("PageTitle")
        outer.addWidget(t)
        outer.addSpacing(10)
        return page, outer

    # ── Pages ───────────────────────────────────────────────────────────
    def _page_layout(self):
        page, lay = self._page("Layout")
        self.expand_ratio = SliderRow("Expand Ratio", 35, 85, "%",
                                      "Share of the monitor given to the focused window")
        lay.addWidget(self.expand_ratio)
        lay.addWidget(_hline())
        self.min_w = _spin(50, 800, 10, " px")
        lay.addWidget(_control_row("Min Window Width", self.min_w))
        self.min_h = _spin(50, 800, 10, " px")
        lay.addWidget(_control_row("Min Window Height", self.min_h))
        lay.addStretch(1)
        return page

    def _page_animation(self):
        page, lay = self._page("Animation")
        self.animate = ToggleSwitch()
        lay.addWidget(_control_row("Smooth Animation", self.animate,
                                   "Animate windows into place"))
        lay.addWidget(_hline())
        self.anim_dur = SliderRow("Duration", 50, 600, " ms")
        lay.addWidget(self.anim_dur)
        lay.addStretch(1)
        return page

    def _page_hover(self):
        page, lay = self._page("Hover to Expand")
        self.hover_enabled = ToggleSwitch()
        self.hover_enabled.toggled.connect(self._update_hover_ui)
        lay.addWidget(_control_row("Enable", self.hover_enabled,
                                   "Expand on cursor hover instead of click"))
        lay.addWidget(_hline())
        self.hover_delay = SliderRow("Delay", 50, 1000, " ms",
                                     "How long the cursor must rest before expanding")
        lay.addWidget(self.hover_delay)
        lay.addStretch(1)
        return page

    def _page_monitors(self):
        page, lay = self._page("Monitors")
        grid = QGridLayout()
        grid.setSpacing(8)
        self._mon_tiles = []
        if not self.monitors:
            lay.addWidget(QLabel("No monitors detected."))
        else:
            for i, m in enumerate(self.monitors):
                tile = QPushButton()
                tile.setObjectName("MonTile")
                tile.setCheckable(True)
                tile.setMinimumHeight(64)
                tile.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                badges = "  ★ Main" if m["primary"] else ""
                if m["orientation"] == "portrait":
                    badges += "  ⤓ Portrait"
                tile.setText(f"Monitor {i + 1}{badges}\n{m['width']} × {m['height']}")
                tile.setChecked(self._is_enabled(m))
                tile.clicked.connect(
                    lambda _c, mm=m, tt=tile: self._toggle_monitor(mm, tt))
                grid.addWidget(tile, i // 2, i % 2)
                self._mon_tiles.append((m, tile))
            lay.addLayout(grid)
            hint = QLabel("Click a monitor to toggle tiling on that display")
            hint.setObjectName("Hint")
            lay.addWidget(hint)
        lay.addStretch(1)
        return page

    def _page_exclusions(self):
        page, lay = self._page("App Exclusions")
        lay.addWidget(self._excl_block(
            "By Window Title", "Skip windows whose title contains this text",
            "e.g. Spotify, Picture-in-Picture", which="title"))
        lay.addWidget(_hline())
        lay.addWidget(self._excl_block(
            "By Executable", "Skip all windows from this process",
            "e.g. spotify.exe, discord.exe", which="exe"))
        lay.addStretch(1)
        return page

    def _page_system(self):
        page, lay = self._page("System")
        self.poll = SliderRow("Poll Interval", 50, 2000, " ms",
                              "How often the tiler checks for focus changes")
        lay.addWidget(self.poll)
        lay.addWidget(_hline())
        self.log_level = QComboBox()
        self.log_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.log_level.setFixedWidth(130)
        lay.addWidget(_control_row("Log Level", self.log_level))
        self.autostart = ToggleSwitch()
        self.autostart.toggled.connect(self._toggle_autostart)
        sub = ("Launch automatically when you log in" if self.frozen
               else "Run the installed .exe to enable this option")
        lay.addWidget(_control_row("Start with Windows", self.autostart, sub))
        lay.addStretch(1)
        return page

    def _excl_block(self, title: str, hint: str, placeholder: str, which: str) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 4, 0, 4)
        v.setSpacing(5)
        v.addWidget(QLabel(title))
        h = QLabel(hint)
        h.setObjectName("Hint")
        v.addWidget(h)
        row = QHBoxLayout()
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        add = QPushButton("Add")
        row.addWidget(edit)
        row.addWidget(add)
        v.addLayout(row)
        lst = QListWidget()
        lst.setObjectName("ExclList")
        lst.setMaximumHeight(96)
        lst.setToolTip("Double-click an entry to remove it")
        v.addWidget(lst)

        add.clicked.connect(lambda: self._add_exclusion(which, edit))
        edit.returnPressed.connect(lambda: self._add_exclusion(which, edit))
        lst.itemDoubleClicked.connect(
            lambda item, w=which: self._remove_exclusion(w, item.text()))

        if which == "title":
            self._title_edit, self._title_list = edit, lst
        else:
            self._exe_edit, self._exe_list = edit, lst
        return box

    # ── Monitor toggle logic ───────────────────────────────────────────
    def _is_enabled(self, m: dict) -> bool:
        if not self.enabled_monitors:
            return True
        return any(e["left"] == m["left"] and e["top"] == m["top"]
                   for e in self.enabled_monitors)

    def _toggle_monitor(self, m: dict, tile: QPushButton) -> None:
        was = self._is_enabled(m)
        if not self.enabled_monitors:
            self.enabled_monitors = [
                {"left": x["left"], "top": x["top"]}
                for x in self.monitors
                if not (x["left"] == m["left"] and x["top"] == m["top"])
            ]
        elif was:
            if len(self.enabled_monitors) <= 1:
                tile.setChecked(True)   # never disable the last monitor
                return
            self.enabled_monitors = [
                e for e in self.enabled_monitors
                if not (e["left"] == m["left"] and e["top"] == m["top"])
            ]
        else:
            self.enabled_monitors.append({"left": m["left"], "top": m["top"]})
            if len(self.enabled_monitors) == len(self.monitors):
                self.enabled_monitors = None
        tile.setChecked(self._is_enabled(m))

    # ── Exclusions ─────────────────────────────────────────────────────
    def _add_exclusion(self, which: str, edit: QLineEdit) -> None:
        val = edit.text().strip()
        if not val:
            return
        if which == "title":
            if val not in self.skip_titles:
                self.skip_titles.append(val)
        else:
            val = val.lower()
            if val not in self.skip_exe:
                self.skip_exe.append(val)
        edit.clear()
        self._render_exclusions()

    def _render_exclusions(self) -> None:
        for lst, items in ((self._title_list, self.skip_titles),
                           (self._exe_list, self.skip_exe)):
            lst.clear()
            for it in items:
                lst.addItem(QListWidgetItem(it))

    def _remove_exclusion(self, which: str, value: str) -> None:
        target = self.skip_titles if which == "title" else self.skip_exe
        if value in target:
            target.remove(value)
        self._render_exclusions()

    # ── Load / hover ───────────────────────────────────────────────────
    def _load_values(self) -> None:
        c = self.cfg
        self.expand_ratio.setValue(round(float(c.get("expand_ratio", 0.65)) * 100))
        self.min_w.setValue(int(c.get("min_window_width", 200)))
        self.min_h.setValue(int(c.get("min_window_height", 200)))
        self.animate.setChecked(c.get("animate", True) is not False)
        self.anim_dur.setValue(int(c.get("animation_duration_ms", 125)))
        self.hover_enabled.setChecked(c.get("hover_enabled", False) is True)
        self.hover_delay.setValue(int(c.get("hover_delay_ms", 300)))
        self.poll.setValue(int(c.get("poll_interval_ms", 100)))
        self.log_level.setCurrentText(str(c.get("log_level", "INFO")))
        self.autostart.setEnabled(self.frozen)
        if self.frozen:
            self.autostart.setChecked(config_io.get_autostart())
        self._render_exclusions()
        self._update_hover_ui(self.hover_enabled.isChecked())

    def _update_hover_ui(self, on: bool) -> None:
        self.hover_delay.setEnabled(bool(on))

    # ── Autostart (applies immediately) ────────────────────────────────
    def _toggle_autostart(self, checked: bool) -> None:
        try:
            config_io.set_autostart(checked)
        except Exception as exc:
            self.autostart.setChecked(not checked)
            self._flash(f"Autostart failed: {exc}")

    # ── Save ───────────────────────────────────────────────────────────
    def _save(self) -> None:
        new = dict(self.cfg)
        new.update({
            "expand_ratio":          self.expand_ratio.value() / 100.0,
            "min_window_width":      self.min_w.value(),
            "min_window_height":     self.min_h.value(),
            "animate":               self.animate.isChecked(),
            "animation_duration_ms": self.anim_dur.value(),
            "hover_enabled":         self.hover_enabled.isChecked(),
            "hover_delay_ms":        self.hover_delay.value(),
            "poll_interval_ms":      self.poll.value(),
            "log_level":             self.log_level.currentText(),
            "enabled_monitors":      self.enabled_monitors,
            "skip_titles":           self.skip_titles,
            "skip_exe":              self.skip_exe,
        })
        try:
            config_io.save_config(new)
            self.cfg = new
            self._flash("Saved · settings applied", saved=True)
        except Exception as exc:
            self._flash(f"Save failed: {exc}")

    def _flash(self, text: str, saved: bool = False) -> None:
        self.status.setText(text)
        if saved:
            self.save_btn.setText("Saved")
            self.save_btn.setProperty("saved", True)
            self.save_btn.style().unpolish(self.save_btn)
            self.save_btn.style().polish(self.save_btn)
            QTimer.singleShot(2200, self._reset_save)

    def _reset_save(self) -> None:
        self.save_btn.setText("Save")
        self.save_btn.setProperty("saved", False)
        self.save_btn.style().unpolish(self.save_btn)
        self.save_btn.style().polish(self.save_btn)
        self.status.setText("")


def _app_icon() -> QIcon:
    """The dark 4-tile mark, drawn in code (matches the tray icon)."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QBrush(QColor(28, 28, 28)))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(2, 2, 60, 60, 12, 12)
    for (x, y, a) in ((12, 12, 220), (34, 12, 128), (12, 34, 128), (34, 34, 60)):
        p.setBrush(QBrush(QColor(255, 255, 255, a)))
        p.drawRoundedRect(x, y, 18, 18, 4, 4)
    p.end()
    return QIcon(pm)


def run() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    win = SettingsWindow()
    win.show()
    win.raise_()
    win.activateWindow()
    return app.exec()


if __name__ == "__main__":
    sys.exit(run())
