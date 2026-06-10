"""settings_window.py — native PySide6 settings window for Windows Focus Manager.

A dark "bento" dashboard: every setting group is a rounded tile in a grid,
no paging.  It reads and writes config.json through config_io, and the running
tiler live-reloads saved changes — no restart, no open port.

Run standalone for development (instant feedback, no rebuild):
    python settings_window.py
"""

import sys

from PySide6.QtCore import (
    Qt, Signal, QTimer, QRect, QRectF, QPropertyAnimation,
    QParallelAnimationGroup, QVariantAnimation, QEasingCurve,
)
from PySide6.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QBrush, QPen, QFont, QCursor,
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QSpinBox, QComboBox, QPushButton, QLineEdit, QSlider,
    QListWidget, QListWidgetItem, QFrame, QSizePolicy, QGraphicsOpacityEffect,
    QStackedWidget,
)

import config_io

# ---------------------------------------------------------------------------
# Theme (Bento — soft lavender accent on dark)
# ---------------------------------------------------------------------------

STYLE = """
/* Bento facelift — soft lavender accent on dark.
   accent #a78bfa · accent-bright #c4b5fd · accent-deep #8b5cf6
   window #0c0b12 · tile rgba(255,255,255,0.035) · line rgba(255,255,255,0.08) */

QWidget {
    background: #0c0b12;
    color: rgba(248,247,255,0.95);
    font-family: 'Segoe UI', system-ui, sans-serif;
    font-size: 13px;
}

/* Labels and inner layout containers must not paint the window colour over
   the lighter tiles — keep them transparent so the tile shows through. */
QLabel { background: transparent; }
#Row   { background: transparent; }

/* Header / status */
#Header      { border-bottom: 1px solid rgba(255,255,255,0.07); }
#HeaderTitle { color: rgba(240,238,255,0.58); font-size: 13px; }
#StatusDot   { font-size: 18px; }

/* Sidebar nav (kept this phase, recoloured) */
#Nav        { background: #0e0d15; border: none;
              border-right: 1px solid rgba(255,255,255,0.07); outline: 0; }
#Nav::item  { padding: 9px 12px; margin: 1px 6px; border-radius: 8px;
              color: rgba(240,238,255,0.52); }
#Nav::item:selected        { background: rgba(167,139,250,0.16); color: rgba(248,247,255,0.96); }
#Nav::item:hover:!selected { background: rgba(255,255,255,0.05); }

/* Tiles */
#Tile               { background: rgba(255,255,255,0.035);
                      border: 1px solid rgba(255,255,255,0.08); border-radius: 18px; }
#Tile:hover         { background: rgba(255,255,255,0.06); border-color: rgba(167,139,250,0.22); }
#Tile[focused="true"] { background: rgba(167,139,250,0.10); border: 1px solid #a78bfa; }
#TileLabel          { color: rgba(240,238,255,0.58); font-size: 11px;
                      font-weight: 700; letter-spacing: 1px; }
#TileValue          { color: #c4b5fd; font-family: 'Cascadia Code','Consolas',monospace;
                      font-size: 16px; font-weight: 600; }
#BigValue           { color: #c4b5fd; font-family: 'Cascadia Code','Consolas',monospace;
                      font-size: 40px; font-weight: 600; }
#Eyebrow { color: #c4b5fd; font-size: 12px; font-weight: 700; letter-spacing: 2px; }
#TipRow  { background: rgba(255,255,255,0.045); border: 1px solid rgba(255,255,255,0.07);
           border-radius: 12px; }
#TipRow:hover { background: rgba(167,139,250,0.10); border-color: rgba(167,139,250,0.22); }
#TipText { font-size: 15px; color: rgba(248,247,255,0.90); }

/* Section / labels */
#PageTitle { color: rgba(240,238,255,0.40); font-size: 10px; font-weight: 700; letter-spacing: 2px; }
#Sub       { color: rgba(240,238,255,0.40); font-size: 11px; }
#Hint      { color: rgba(240,238,255,0.40); font-size: 11px; }
#Value     { color: #c4b5fd; font-family: 'Cascadia Code','Consolas',monospace; font-size: 14px; }
#RowLine   { color: rgba(255,255,255,0.10); }

/* Sliders */
QSlider::groove:horizontal   { height: 5px; background: rgba(255,255,255,0.12); border-radius: 3px; }
QSlider::sub-page:horizontal { background: #8b5cf6; border-radius: 3px; }
QSlider::add-page:horizontal { background: rgba(255,255,255,0.10); border-radius: 3px; }
QSlider::handle:horizontal   { width: 14px; height: 14px; margin: -6px 0; border-radius: 5px;
                               background: #ffffff; border: 2px solid #a78bfa; }

/* Inputs */
QSpinBox, QComboBox, QLineEdit {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.10);
    border-radius: 8px; padding: 6px 10px; min-height: 20px;
    selection-background-color: rgba(167,139,250,0.30);
}
QSpinBox:focus, QComboBox:focus, QLineEdit:focus { border-color: rgba(167,139,250,0.45); }
QComboBox QAbstractItemView {
    background: #161421; border: 1px solid rgba(167,139,250,0.22); border-radius: 8px;
    selection-background-color: rgba(167,139,250,0.18); outline: 0;
}

/* Buttons */
QPushButton          { background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.10);
                       border-radius: 9px; padding: 8px 14px; }
QPushButton:hover    { background: rgba(255,255,255,0.10); border-color: rgba(167,139,250,0.30); }
QPushButton:pressed  { background: rgba(255,255,255,0.04); }

#PrimaryBtn          { background: #8b5cf6; color: #ffffff; border: none; font-weight: 600; }
#PrimaryBtn:hover    { background: #9d6dff; }
#PrimaryBtn:pressed  { background: #7c4fe0; }
#SaveBtn             { font-weight: 600; }
#SaveBtn[saved="true"] { background: #8b5cf6; color: #ffffff; border-color: transparent; }

/* Monitor tiles / exclusions / footer */
#MonTile           { padding: 10px 8px; border-radius: 11px; text-align: center;
                     background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08); }
#MonTile:checked   { background: rgba(167,139,250,0.14); border-color: rgba(167,139,250,0.22); }

#ExclList { background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.10); border-radius: 8px; }
#Chip     { background: rgba(167,139,250,0.13); border: 1px solid rgba(167,139,250,0.22);
            border-radius: 8px; padding: 4px 8px; }

#Footer { border-top: 1px solid rgba(255,255,255,0.08); }
#Status { color: rgba(240,238,255,0.45); font-size: 12px; }

/* Scrollbars */
QScrollBar:vertical   { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: rgba(255,255,255,0.14); border-radius: 4px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: rgba(167,139,250,0.40); }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
"""


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
        # Bento palette: accent-deep track when on, subtle white when off.
        on_track  = QColor("#8b5cf6")
        off_track = QColor(255, 255, 255, 28)
        knob_on   = QColor("#ffffff")
        knob_off  = QColor(255, 255, 255, 180)
        p.setBrush(QBrush(on_track if on else off_track))
        p.drawRoundedRect(0, 0, 44, 24, 12, 12)
        p.setBrush(QBrush(knob_on if on else knob_off))
        p.drawEllipse(23 if on else 3, 3, 18, 18)
        p.end()


# ---------------------------------------------------------------------------
# Small layout helpers
# ---------------------------------------------------------------------------

def _control_row(label: str, widget: QWidget, sub: str = "") -> QWidget:
    """A label-on-left / control-on-right row, with an optional sub-caption."""
    row = QWidget()
    row.setObjectName("Row")
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 4, 0, 4)
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

    def __init__(self, label: str, lo: int, hi: int, suffix: str = "") -> None:
        super().__init__()
        self.setObjectName("Row")
        self._suffix = suffix
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 4, 0, 4)
        v.setSpacing(6)
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

    def _update_label(self, raw: int) -> None:
        self.value_lbl.setText(f"{raw}{self._suffix}")

    def setValue(self, raw: int) -> None:
        self.slider.setValue(int(raw))
        self._update_label(int(raw))

    def value(self) -> int:
        return self.slider.value()


# ---------------------------------------------------------------------------
# Bento tile + canvas (manual geometry so the focus transition can animate)
# ---------------------------------------------------------------------------

class Tile(QFrame):
    """A bento tile.  Emits `clicked` on a *background* press.

    Because child controls (buttons, sliders, toggles) consume their own
    mouse press, those clicks never reach here — so clicking a control does
    not toggle focus, while clicking the tile's empty area or a label does.
    """
    clicked = Signal()
    entered = Signal()
    left = Signal()

    def __init__(self, tile_id: str, parent=None) -> None:
        super().__init__(parent)
        self.tile_id = tile_id
        self.body = None          # hideable content container (set in _tile)
        self.value_label = None   # compact value shown when shrunk (set in _tile)
        self.setObjectName("Tile")

    def mousePressEvent(self, event) -> None:
        self.clicked.emit()
        event.accept()

    def enterEvent(self, event) -> None:
        self.entered.emit()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.left.emit()
        super().leaveEvent(event)


class BentoCanvas(QWidget):
    """Holds the tiles at absolute geometry.  Reports resizes and clicks on
    empty space (the gaps between tiles), which collapse the focused tile."""
    resized = Signal()
    bgClicked = Signal()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.resized.emit()

    def mousePressEvent(self, event) -> None:
        self.bgClicked.emit()
        super().mousePressEvent(event)


class TilingPreview(QWidget):
    """A small live diagram of the tiler: a focused window beside stacked
    siblings.  When interactive, hovering a window focuses it — it expands and
    the others reflow gap-free, exactly like the real app."""

    def __init__(self, ratio: float = 0.65, interactive: bool = False,
                 parent=None) -> None:
        super().__init__(parent)
        self._ratio = ratio
        self._interactive = interactive
        # Interactive preview uses the bento grid-bias model (like the settings
        # tiles); the dashboard preview uses the ratio split.
        self._mode = "grid" if interactive else "ratio"
        self._n = 2 if interactive else 4
        self._focus = 0
        self._start: dict = {}       # rects at animation start
        self._end: dict = {}         # rects at animation target
        self._t = 1.0                # 0..1 progress (already eased)
        self._anim = None
        self.setObjectName("Row")
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        if interactive:
            self.setMouseTracking(True)
            self.setCursor(Qt.PointingHandCursor)

    def set_ratio(self, percent: int) -> None:
        self._ratio = max(0.2, min(0.9, percent / 100.0))
        self._end = self._layout(self._focus)
        self._start = {}
        self._t = 1.0
        self.update()

    # ── layout maths ────────────────────────────────────────────────────
    def _layout(self, focus: int) -> dict:
        mon = QRectF(1, 1, self.width() - 2, self.height() - 2)
        inner = mon.adjusted(12, 12, -12, -12)
        gap = 6
        if inner.width() < 40 or inner.height() < 30:
            return {i: QRectF(inner) for i in range(self._n)}
        if self._mode == "grid":
            return self._layout_grid(focus, inner, gap)
        # Ratio split: focused window on the left, siblings stacked right.
        fw = max(24.0, (inner.width() - gap) * self._ratio)
        res = {focus: QRectF(inner.left(), inner.top(), fw, inner.height())}
        sibs = [i for i in range(self._n) if i != focus]
        sx = inner.left() + fw + gap
        sw = inner.right() - sx
        n = len(sibs)
        sib_h = (inner.height() - (n - 1) * gap) / n if n else inner.height()
        for j, i in enumerate(sibs):
            res[i] = QRectF(sx, inner.top() + j * (sib_h + gap), sw, sib_h)
        return res

    def _layout_grid(self, focus: int, inner: QRectF, gap: int) -> dict:
        """Two windows side by side — hovering one widens it while the other
        yields, gap-free, like the settings tiles."""
        bias = 2.0
        cw = [1.0, 1.0]
        cw[focus] *= bias
        aw = inner.width() - gap
        colw = [aw * c / sum(cw) for c in cw]
        return {
            0: QRectF(inner.left(), inner.top(), colw[0], inner.height()),
            1: QRectF(inner.left() + colw[0] + gap, inner.top(),
                      colw[1], inner.height()),
        }

    def _rects(self) -> dict:
        if not self._end:
            self._end = self._layout(self._focus)
        if self._t >= 1.0 or not self._start:
            return self._end
        t = self._t
        out = {}
        for i, b in self._end.items():
            a = self._start.get(i, b)
            out[i] = QRectF(a.x() + (b.x() - a.x()) * t,
                            a.y() + (b.y() - a.y()) * t,
                            a.width() + (b.width() - a.width()) * t,
                            a.height() + (b.height() - a.height()) * t)
        return out

    def _set_focus(self, win: int) -> None:
        self._start = self._rects()
        self._focus = win
        self._end = self._layout(win)
        self._t = 0.0
        anim = QVariantAnimation(self)
        anim.setDuration(260)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.valueChanged.connect(self._on_anim)
        self._anim = anim
        anim.start()

    def _on_anim(self, val) -> None:
        self._t = float(val)
        self.update()

    def resizeEvent(self, event) -> None:
        self._end = self._layout(self._focus)
        self._start = {}
        self._t = 1.0
        super().resizeEvent(event)
        self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._interactive:
            pos = event.position()
            for i, r in self._rects().items():
                if i != self._focus and r.contains(pos):
                    self._set_focus(i)
                    break
        super().mouseMoveEvent(event)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        mon = QRectF(1, 1, self.width() - 2, self.height() - 2)
        p.setPen(QPen(QColor(255, 255, 255, 28), 1))
        p.setBrush(QColor(0, 0, 0, 70))
        p.drawRoundedRect(mon, 10, 10)

        f = QFont("Segoe UI")
        f.setPointSize(8)
        f.setBold(True)
        rects = self._rects()
        for i in range(self._n):
            r = rects.get(i)
            if r is None or r.width() < 6 or r.height() < 6:
                continue
            focused = (i == self._focus)
            if focused:
                p.setPen(QPen(QColor("#a78bfa"), 1.6))
                p.setBrush(QColor(167, 139, 250, 32))
            else:
                p.setPen(QPen(QColor(255, 255, 255, 22), 1))
                p.setBrush(QColor(255, 255, 255, 12))
            p.drawRoundedRect(r, 6, 6)

            if focused and r.width() > 60:
                p.setFont(f)
                p.setPen(QColor("#c4b5fd"))
                label = (f"Focused · {round(self._ratio * 100)}%"
                         if self._mode == "ratio" else "Focused")
                p.drawText(QRectF(r.left() + 9, r.top() + 7, r.width() - 14, 16),
                           int(Qt.AlignLeft | Qt.AlignVCenter), label)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(196, 181, 253, 95))
                for k, frac in enumerate((0.72, 0.86, 0.5)):
                    ly = r.top() + 28 + k * 8
                    if ly + 3 < r.bottom() - 8:
                        p.drawRoundedRect(QRectF(r.left() + 9, ly,
                                                 (r.width() - 18) * frac, 3), 1.5, 1.5)
            elif not focused and r.width() > 16:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, 40))
                p.drawRoundedRect(QRectF(r.left() + 7, r.top() + 7,
                                         min(r.width() * 0.6, r.width() - 14), 3), 1.5, 1.5)
                if r.height() > 24:
                    p.drawRoundedRect(QRectF(r.left() + 7, r.top() + 14,
                                             min(r.width() * 0.4, r.width() - 14), 3), 1.5, 1.5)
        p.end()


class _WelcomeStage(QWidget):
    """Two welcome panels that tile against each other: hovering one expands
    it while the other yields, gap-free — the app demonstrating itself with its
    own content.  Panels are positioned by code so the transition animates."""

    def __init__(self) -> None:
        super().__init__()
        self._left = None
        self._right = None
        self._focus = None          # None (even), "left", or "right"
        self._anim = None
        self._revert = QTimer(self)
        self._revert.setSingleShot(True)
        self._revert.timeout.connect(self._to_even)

    def setup(self, left, right) -> None:
        self._left, self._right = left, right
        left.entered.connect(lambda: self._on_enter("left"))
        right.entered.connect(lambda: self._on_enter("right"))
        left.left.connect(lambda: self._on_leave(left))
        right.left.connect(lambda: self._on_leave(right))
        QTimer.singleShot(0, lambda: self._relayout(animate=False))

    def _on_enter(self, side: str) -> None:
        self._revert.stop()
        if self._focus != side:
            self._focus = side
            self._relayout(animate=True)

    def _on_leave(self, tile) -> None:
        # Ignore leaves caused by moving onto a child widget (still inside).
        if tile.rect().contains(tile.mapFromGlobal(QCursor.pos())):
            return
        self._revert.start(180)

    def _to_even(self) -> None:
        if self._focus is not None:
            self._focus = None
            self._relayout(animate=True)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout(animate=False)

    def _rects(self):
        m, gap = 20, 14
        aw = self.width() - 2 * m - gap
        ah = self.height() - 2 * m
        wl = [1.0, 1.0]
        if self._focus == "left":
            wl[0] *= 1.5
        elif self._focus == "right":
            wl[1] *= 1.5
        lw = aw * wl[0] / sum(wl)
        lr = QRect(m, m, round(lw), ah)
        rr = QRect(m + round(lw) + gap, m, round(aw - lw), ah)
        return lr, rr

    def _relayout(self, animate: bool) -> None:
        if self._left is None or self.width() < 20:
            return
        lr, rr = self._rects()
        if not animate:
            self._left.setGeometry(lr)
            self._right.setGeometry(rr)
            return
        if self._anim is not None:
            self._anim.stop()
        group = QParallelAnimationGroup(self)
        for tile, target in ((self._left, lr), (self._right, rr)):
            a = QPropertyAnimation(tile, b"geometry")
            a.setDuration(300)
            a.setEasingCurve(QEasingCurve.OutCubic)
            a.setStartValue(tile.geometry())
            a.setEndValue(target)
            group.addAnimation(a)
        self._anim = group
        group.start()


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

        self._tiles: dict = {}
        self._focused = None        # id of the expanded tile, or None
        self._anim = None           # current geometry animation group

        # Hover-to-expand (active only when the Hover-mode setting is on).
        self._hover_pending = None
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.timeout.connect(self._hover_fire)
        self._collapse_timer = QTimer(self)
        self._collapse_timer.setSingleShot(True)
        self._collapse_timer.timeout.connect(self._hover_collapse)

        self.setWindowTitle("Window Focus Manager — Settings")
        self.setMinimumSize(1080, 840)
        self.resize(1140, 880)
        self.setStyleSheet(STYLE)
        self.setWindowIcon(_app_icon())

        self._build_ui()
        self._load_values()

    # ── UI ──────────────────────────────────────────────────────────────
    # Bento arrangement on a 4-column x 3-row grid.  Each value is
    # (col, row, col_span, row_span).  The geometry is computed manually
    # (see _rects_for) so the focus transition can animate.
    _SPANS = {
        "expand":     (0, 0, 2, 2),   # hero
        "monitors":   (2, 0, 2, 1),
        "animation":  (2, 1, 1, 1),
        "hover":      (3, 1, 1, 1),
        "minsize":    (0, 2, 1, 1),
        "exclusions": (1, 2, 1, 1),
        "startup":    (2, 2, 1, 1),
        "system":     (3, 2, 1, 1),
    }
    _MARGIN = 20
    _GAP = 14
    _ROW_WEIGHTS = [1.0, 1.0, 1.25]
    _H_BIAS = 1.7    # focused tile's columns grow by this factor (gentle, so
    _V_BIAS = 2.0    # neighbours keep enough room to show their controls)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.stack = QStackedWidget()
        root.addWidget(self.stack)
        self.stack.addWidget(self._build_welcome())      # index 0
        self.stack.addWidget(self._build_dashboard())    # index 1
        self.stack.setCurrentIndex(1 if self.cfg.get("welcome_seen", False) else 0)

    def _build_dashboard(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        self.canvas = BentoCanvas()
        self.canvas.resized.connect(lambda: self._relayout(animate=False))
        self.canvas.bgClicked.connect(self._collapse)
        root.addWidget(self.canvas, 1)

        # Build the tiles (each registers itself into self._tiles and parents
        # to the canvas via _tile()).
        self._tile_expand()
        self._tile_monitors()
        self._tile_animation()
        self._tile_hover()
        self._tile_minsize()
        self._tile_exclusions()
        self._tile_startup()
        self._tile_system()

        # First layout once the canvas has a real size.
        QTimer.singleShot(0, lambda: self._relayout(animate=False))
        return page

    # ── Welcome / first-run screen ──────────────────────────────────────
    def _build_welcome(self) -> QWidget:
        # The two panels themselves tile: hover one and it expands while the
        # other yields — the welcome screen IS the demo.
        stage = _WelcomeStage()

        # Left panel — pitch + actions.
        left = Tile("w_left", stage)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(46, 42, 46, 42)
        lv.setSpacing(0)
        logo = QLabel()
        logo.setPixmap(_app_icon().pixmap(66, 66))
        lv.addWidget(logo)
        lv.addStretch(1)
        eyebrow = QLabel("WINDOW FOCUS MANAGER")
        eyebrow.setObjectName("Eyebrow")
        lv.addWidget(eyebrow)
        lv.addSpacing(16)
        head = QLabel()
        head.setWordWrap(True)
        head.setStyleSheet("background: transparent;")
        head.setText(
            '<span style="font-size:44px; font-weight:bold; color:#f8f7ff;">Every window,</span>'
            '<br>'
            '<span style="font-size:44px; font-weight:bold; color:#c4b5fd;">in its place.</span>')
        lv.addWidget(head)
        lv.addSpacing(22)
        sub = QLabel("Focus a window — the rest tile in around it automatically. "
                     "Gap-free, per-monitor, no shortcuts to learn.")
        sub.setWordWrap(True)
        sub.setStyleSheet("background: transparent; font-size: 17px; line-height: 1.5; "
                          "color: rgba(240,238,255,0.64);")
        lv.addWidget(sub)
        lv.addStretch(1)
        get = QPushButton("Get started")
        get.setObjectName("PrimaryBtn")
        get.setCursor(Qt.PointingHandCursor)
        get.setStyleSheet("font-size: 15px; font-weight: 600; padding: 14px 40px;")
        get.clicked.connect(self._finish_welcome)
        gh = QHBoxLayout()
        gh.addWidget(get)
        gh.addStretch(1)
        lv.addLayout(gh)

        # Right panel — how it works (each tip is its own card).
        right = Tile("w_right", stage)
        rv = QVBoxLayout(right)
        rv.setContentsMargins(42, 40, 42, 40)
        rv.setSpacing(16)
        rv.addStretch(1)
        tlabel = QLabel("How it works")
        tlabel.setStyleSheet("background: transparent; font-size: 28px; "
                             "font-weight: 600; color: rgba(248,247,255,0.95);")
        rv.addWidget(tlabel)
        rv.addSpacing(18)
        for icon, text in (
                ("layout", "These two panels are tiling right now — hover one"),
                ("cursor", "Hover or click a window — it expands, the rest tile around it"),
                ("monitor", "Each monitor tiles on its own, gap-free"),
                ("sliders", "Right-click the tray icon anytime for quick actions")):
            card = QFrame()
            card.setObjectName("TipRow")
            cl = QHBoxLayout(card)
            cl.setContentsMargins(18, 18, 18, 18)
            cl.setSpacing(16)
            ic = QLabel()
            ic.setPixmap(_make_tile_icon(icon, 38))
            ic.setFixedSize(38, 38)
            cl.addWidget(ic)
            txt = QLabel(text)
            txt.setObjectName("TipText")
            txt.setWordWrap(True)
            cl.addWidget(txt, 1)
            rv.addWidget(card)
        rv.addStretch(1)

        stage.setup(left, right)
        return stage

    def _finish_welcome(self) -> None:
        self.cfg["welcome_seen"] = True
        try:
            config_io.save_config(self.cfg)
        except Exception:
            pass
        self.stack.setCurrentIndex(1)

    # ── Geometry / focus animation ──────────────────────────────────────
    def _rects_for(self, size, focused) -> dict:
        """Return {tile_id: QRect} for the given canvas size and focused tile.

        The bento is a weighted 4x3 grid.  Focusing a tile multiplies the
        weights of the columns and rows it spans, so it claims more space and
        its neighbours yield — gap-free, the same idea as the window tiler."""
        w, h = size.width(), size.height()
        cols, rows = 4, 3
        col_w = [1.0] * cols
        row_w = list(self._ROW_WEIGHTS)

        if focused is not None and focused in self._SPANS:
            c0, r0, cs, rs = self._SPANS[focused]
            for c in range(c0, c0 + cs):
                col_w[c] *= self._H_BIAS
            for r in range(r0, r0 + rs):
                row_w[r] *= self._V_BIAS

        avail_w = max(1, w - 2 * self._MARGIN - (cols - 1) * self._GAP)
        avail_h = max(1, h - 2 * self._MARGIN - (rows - 1) * self._GAP)
        sw, sh = sum(col_w), sum(row_w)
        cw = [avail_w * c / sw for c in col_w]
        rh = [avail_h * r / sh for r in row_w]

        xs = [float(self._MARGIN)]
        for c in range(1, cols):
            xs.append(xs[c - 1] + cw[c - 1] + self._GAP)
        ys = [float(self._MARGIN)]
        for r in range(1, rows):
            ys.append(ys[r - 1] + rh[r - 1] + self._GAP)

        rects = {}
        for tid, (c0, r0, cs, rs) in self._SPANS.items():
            left = xs[c0]
            top = ys[r0]
            right = xs[c0 + cs - 1] + cw[c0 + cs - 1]
            bottom = ys[r0 + rs - 1] + rh[r0 + rs - 1]
            rects[tid] = QRect(round(left), round(top),
                               round(right - left), round(bottom - top))
        return rects

    def _relayout(self, animate: bool) -> None:
        if not self._tiles or not self.canvas.size().isValid():
            return
        rects = self._rects_for(self.canvas.size(), self._focused)

        # Reflect focus in styling, and hide the body of shrunk tiles so their
        # content doesn't get clipped (only the label shows when shrunk).
        for tid, tile in self._tiles.items():
            want = (tid == self._focused)
            if bool(tile.property("focused")) != want:
                tile.setProperty("focused", want)
                tile.style().unpolish(tile)
                tile.style().polish(tile)
            # Content stays fully visible in every tile, focused or not — the
            # gentle expansion below keeps neighbours readable rather than blank.

        if not animate:
            for tid, tile in self._tiles.items():
                tile.setGeometry(rects[tid])
            return

        if self._anim is not None:
            self._anim.stop()
        group = QParallelAnimationGroup(self)
        for tid, tile in self._tiles.items():
            a = QPropertyAnimation(tile, b"geometry")
            a.setDuration(320)
            a.setEasingCurve(QEasingCurve.OutCubic)
            a.setStartValue(tile.geometry())
            a.setEndValue(rects[tid])
            group.addAnimation(a)
        self._anim = group
        group.start()

    # ── Content crossfade (body <-> compact value) ──────────────────────
    def _clear_fade(self, widget) -> None:
        old = getattr(widget, "_fade_anim", None)
        if old is not None:
            old.stop()
            old.deleteLater()
            widget._fade_anim = None
        widget.setGraphicsEffect(None)

    def _fade(self, widget, start: float, end: float, dur: int,
              hide_on_end: bool = False, drop_effect_on_end: bool = False) -> None:
        self._clear_fade(widget)
        eff = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(eff)
        eff.setOpacity(start)
        anim = QPropertyAnimation(eff, b"opacity", widget)
        anim.setDuration(dur)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(start)
        anim.setEndValue(end)

        def _done():
            if hide_on_end:
                widget.setVisible(False)
            if drop_effect_on_end:
                widget.setGraphicsEffect(None)
            widget._fade_anim = None

        anim.finished.connect(_done)
        widget._fade_anim = anim
        anim.start()

    def _apply_content_state(self, tile, shrunk: bool, animate: bool) -> None:
        """Show full controls or the compact value, crossfading when animating."""
        prev = getattr(tile, "_shrunk", None)
        tile._shrunk = shrunk
        if shrunk and tile.value_label is not None:
            tile.value_label.setText(self._compact_text(tile.tile_id))

        # No transition (first layout, resize, or unchanged state) → set instantly.
        if not animate or prev == shrunk:
            if tile.body is not None:
                self._clear_fade(tile.body)
                tile.body.setVisible(not shrunk)
            if tile.value_label is not None:
                self._clear_fade(tile.value_label)
                tile.value_label.setVisible(shrunk)
            return

        # Crossfade.
        if shrunk:
            if tile.value_label is not None:
                tile.value_label.setVisible(True)
                self._fade(tile.value_label, 0.0, 1.0, 200)
            if tile.body is not None:
                self._fade(tile.body, 1.0, 0.0, 190, hide_on_end=True)
        else:
            if tile.body is not None:
                tile.body.setVisible(True)
                self._fade(tile.body, 0.0, 1.0, 230, drop_effect_on_end=True)
            if tile.value_label is not None:
                self._fade(tile.value_label, 1.0, 0.0, 170, hide_on_end=True)

    def _compact_text(self, tile_id: str) -> str:
        """One-line summary shown on a tile while it's shrunk."""
        if tile_id == "expand":
            return f"{self.expand_slider.value()}%"
        if tile_id == "monitors":
            enabled = sum(1 for m in self.monitors if self._is_enabled(m))
            return f"{enabled} of {len(self.monitors)}" if self.monitors else "—"
        if tile_id == "animation":
            return f"{self.anim_dur.value()} ms" if self.animate.isChecked() else "Off"
        if tile_id == "hover":
            return f"{self.hover_delay.value()} ms" if self.hover_enabled.isChecked() else "Off"
        if tile_id == "minsize":
            return f"{self.min_w.value()} × {self.min_h.value()}"
        if tile_id == "exclusions":
            n = len(self.skip_titles) + len(self.skip_exe)
            return f"{n} rule" if n == 1 else f"{n} rules"
        if tile_id == "startup":
            return "On" if self.autostart.isChecked() else "Off"
        if tile_id == "system":
            return f"{self.poll.value()} ms"
        return ""

    def _on_tile_clicked(self, tile_id: str) -> None:
        self._focused = None if self._focused == tile_id else tile_id
        self._relayout(animate=True)

    def _collapse(self) -> None:
        if self._focused is not None:
            self._focused = None
            self._relayout(animate=True)

    # ── Hover-to-expand (mirrors the app's Hover-mode setting) ──────────
    def _hover_active(self) -> bool:
        return hasattr(self, "hover_enabled") and self.hover_enabled.isChecked()

    def _on_tile_entered(self, tile_id: str) -> None:
        if not self._hover_active():
            return
        self._collapse_timer.stop()        # cancel any pending collapse
        if self._focused == tile_id:
            return
        self._hover_pending = tile_id
        self._hover_timer.start(max(0, self.hover_delay.value()))

    def _on_tile_left(self, _tile_id: str) -> None:
        if not self._hover_active():
            return
        self._hover_timer.stop()
        # Collapse shortly after leaving a tile — cancelled if we enter another.
        self._collapse_timer.start(160)

    def _hover_fire(self) -> None:
        if (self._hover_pending is not None and self._hover_active()
                and self._focused != self._hover_pending):
            self._focused = self._hover_pending
            self._relayout(animate=True)

    def _hover_collapse(self) -> None:
        if self._hover_active() and self._focused is not None:
            self._focused = None
            self._relayout(animate=True)

    # ── Header ──────────────────────────────────────────────────────────
    def _build_header(self) -> QWidget:
        head = QWidget()
        head.setObjectName("Header")
        head.setFixedHeight(60)
        h = QHBoxLayout(head)
        h.setContentsMargins(20, 0, 20, 0)
        h.setSpacing(12)

        mark = QLabel()
        mark.setPixmap(_app_icon().pixmap(28, 28))
        h.addWidget(mark)

        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel("Settings")
        title.setStyleSheet("font-size: 16px; font-weight: 600; "
                            "color: rgba(248,247,255,0.95);")
        titles.addWidget(title)
        self.status_sub = QLabel("")
        self.status_sub.setObjectName("Sub")
        titles.addWidget(self.status_sub)
        h.addLayout(titles)

        h.addStretch(1)

        self.status = QLabel("All changes applied")
        self.status.setObjectName("Status")
        h.addWidget(self.status)

        self.save_btn = QPushButton("Save")
        self.save_btn.setObjectName("SaveBtn")
        self.save_btn.clicked.connect(self._save)
        h.addWidget(self.save_btn)
        return head

    # ── Tile helper ─────────────────────────────────────────────────────
    def _tile(self, tile_id: str, title: str, icon: str = "") -> tuple:
        frame = Tile(tile_id, self.canvas)
        outer = QVBoxLayout(frame)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        if icon:
            ic = QLabel()
            ic.setPixmap(_make_tile_icon(icon))
            ic.setFixedSize(22, 22)
            head.addWidget(ic)
        lbl = QLabel(title.upper())
        lbl.setObjectName("TileLabel")
        head.addWidget(lbl)
        head.addStretch(1)
        outer.addLayout(head)

        # Compact value, shown only when the tile is shrunk.
        value = QLabel("")
        value.setObjectName("TileValue")
        value.setVisible(False)
        outer.addWidget(value)
        frame.value_label = value

        # All controls live in a body container so it can be hidden when the
        # tile is shrunk (another tile focused) — avoids clipped content.
        body = QWidget()
        body.setObjectName("Row")
        v = QVBoxLayout(body)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        outer.addWidget(body, 1)
        frame.body = body

        frame.clicked.connect(lambda i=tile_id: self._on_tile_clicked(i))
        frame.entered.connect(lambda i=tile_id: self._on_tile_entered(i))
        frame.left.connect(lambda i=tile_id: self._on_tile_left(i))
        self._tiles[tile_id] = frame
        return frame, v

    # ── Tiles ───────────────────────────────────────────────────────────
    def _tile_expand(self) -> QFrame:
        frame, v = self._tile("expand", "Expand Ratio", "layout")
        self.expand_value = QLabel("65%")
        self.expand_value.setObjectName("BigValue")
        v.addWidget(self.expand_value)
        hint = QLabel("of the screen goes to the focused window")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        v.addWidget(hint)

        self.preview = TilingPreview()
        v.addWidget(self.preview, 1)

        self.expand_slider = QSlider(Qt.Horizontal)
        self.expand_slider.setRange(35, 85)
        self.expand_slider.valueChanged.connect(self._on_ratio_changed)
        self.expand_slider.valueChanged.connect(self.preview.set_ratio)
        v.addWidget(self.expand_slider)
        chips = QHBoxLayout()
        chips.setSpacing(6)
        for preset in (40, 65, 80):
            b = QPushButton(str(preset))
            b.setFixedWidth(56)
            b.clicked.connect(lambda _c, p=preset: self.expand_slider.setValue(p))
            chips.addWidget(b)
        chips.addStretch(1)
        v.addLayout(chips)
        return frame

    def _on_ratio_changed(self, value: int) -> None:
        self.expand_value.setText(f"{value}%")

    def _tile_monitors(self) -> QFrame:
        frame, v = self._tile("monitors", "Monitors", "monitor")
        self._mon_tiles = []
        if not self.monitors:
            v.addWidget(QLabel("No monitors detected."))
            v.addStretch(1)
            return frame
        row = QHBoxLayout()
        row.setSpacing(8)
        for i, m in enumerate(self.monitors):
            tile = QPushButton()
            tile.setObjectName("MonTile")
            tile.setCheckable(True)
            tile.setMinimumHeight(56)
            tile.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            badges = "  ★" if m["primary"] else ""
            tile.setText(f"{i + 1}{badges}\n{m['width']}×{m['height']}")
            tile.setChecked(self._is_enabled(m))
            tile.clicked.connect(
                lambda _c, mm=m, tt=tile: self._toggle_monitor(mm, tt))
            row.addWidget(tile)
            self._mon_tiles.append((m, tile))
        v.addLayout(row)
        self.mon_hint = QLabel("")
        self.mon_hint.setObjectName("Hint")
        v.addWidget(self.mon_hint)
        v.addStretch(1)
        return frame

    def _update_mon_hint(self) -> None:
        if not getattr(self, "mon_hint", None) or not self.monitors:
            return
        enabled = sum(1 for m in self.monitors if self._is_enabled(m))
        self.mon_hint.setText(f"{enabled} of {len(self.monitors)} displays tiling")

    def _tile_animation(self) -> QFrame:
        frame, v = self._tile("animation", "Animation", "motion")
        self.animate = ToggleSwitch()
        v.addWidget(_control_row("Smooth", self.animate))
        self.anim_dur = SliderRow("Duration", 50, 600, " ms")
        v.addWidget(self.anim_dur)
        v.addStretch(1)
        return frame

    def _tile_hover(self) -> QFrame:
        frame, v = self._tile("hover", "Hover Mode", "cursor")
        self.hover_enabled = ToggleSwitch()
        self.hover_enabled.toggled.connect(self._update_hover_ui)
        v.addWidget(_control_row("On hover", self.hover_enabled))
        self.hover_delay = SliderRow("Delay", 50, 1000, " ms")
        v.addWidget(self.hover_delay)
        self.return_to_center = ToggleSwitch()
        v.addWidget(_control_row("Return to center", self.return_to_center,
                                 "Monitors with no focus tile evenly"))
        v.addStretch(1)
        return frame

    def _tile_minsize(self) -> QFrame:
        frame, v = self._tile("minsize", "Min Size", "resize")
        self.min_w = _spin(50, 1200, 10, " px")
        self.min_h = _spin(50, 1200, 10, " px")
        v.addWidget(_control_row("Width", self.min_w))
        v.addWidget(_control_row("Height", self.min_h))
        v.addStretch(1)
        return frame

    def _tile_exclusions(self) -> QFrame:
        frame, v = self._tile("exclusions", "Exclusions", "ban")
        v.addWidget(self._excl_block(
            "Windows from these apps are never tiled (match by .exe name)",
            "e.g. spotify.exe", which="exe"))
        v.addStretch(1)
        return frame

    def _tile_startup(self) -> QFrame:
        frame, v = self._tile("startup", "Start-up", "power")
        self.autostart = ToggleSwitch()
        self.autostart.toggled.connect(self._toggle_autostart)
        v.addWidget(_control_row("With Windows", self.autostart))
        sub = ("Launch automatically when you log in" if self.frozen
               else "Run the installed .exe to enable")
        s = QLabel(sub)
        s.setObjectName("Hint")
        s.setWordWrap(True)
        v.addWidget(s)
        v.addStretch(1)
        return frame

    def _tile_system(self) -> QFrame:
        frame, v = self._tile("system", "System", "sliders")
        self.poll = SliderRow("Poll interval", 50, 2000, " ms")
        v.addWidget(self.poll)
        self.log_level = QComboBox()
        self.log_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        v.addWidget(_control_row("Log level", self.log_level))
        v.addStretch(1)
        return frame

    def _excl_block(self, label: str, placeholder: str, which: str) -> QWidget:
        box = QWidget()
        box.setObjectName("Row")
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 2, 0, 2)
        v.setSpacing(5)
        cap = QLabel(label)
        cap.setObjectName("Hint")
        v.addWidget(cap)
        row = QHBoxLayout()
        row.setSpacing(6)
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        add = QPushButton("Add")
        add.setObjectName("PrimaryBtn")
        row.addWidget(edit)
        row.addWidget(add)
        v.addLayout(row)
        lst = QListWidget()
        lst.setObjectName("ExclList")
        lst.setMaximumHeight(66)
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
        self._update_mon_hint()

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
        self._exe_list.clear()
        for it in self.skip_exe:
            self._exe_list.addItem(QListWidgetItem(it))

    def _remove_exclusion(self, which: str, value: str) -> None:
        target = self.skip_titles if which == "title" else self.skip_exe
        if value in target:
            target.remove(value)
        self._render_exclusions()

    # ── Load / hover ───────────────────────────────────────────────────
    def _load_values(self) -> None:
        c = self.cfg
        self.expand_slider.setValue(round(float(c.get("expand_ratio", 0.65)) * 100))
        self._on_ratio_changed(self.expand_slider.value())
        self.min_w.setValue(int(c.get("min_window_width", 200)))
        self.min_h.setValue(int(c.get("min_window_height", 200)))
        self.animate.setChecked(c.get("animate", True) is not False)
        self.anim_dur.setValue(int(c.get("animation_duration_ms", 125)))
        self.hover_enabled.setChecked(c.get("hover_enabled", False) is True)
        self.hover_delay.setValue(int(c.get("hover_delay_ms", 300)))
        self.return_to_center.setChecked(c.get("return_to_center", False) is True)
        self.poll.setValue(int(c.get("poll_interval_ms", 100)))
        self.log_level.setCurrentText(str(c.get("log_level", "INFO")))
        self.autostart.setEnabled(self.frozen)
        if self.frozen:
            self.autostart.setChecked(config_io.get_autostart())
        self._render_exclusions()
        self._update_hover_ui(self.hover_enabled.isChecked())
        self._update_mon_hint()

    def _update_hover_ui(self, on: bool) -> None:
        self.hover_delay.setEnabled(bool(on))
        if hasattr(self, "return_to_center"):
            self.return_to_center.setEnabled(bool(on))

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
            "expand_ratio":          self.expand_slider.value() / 100.0,
            "min_window_width":      self.min_w.value(),
            "min_window_height":     self.min_h.value(),
            "animate":               self.animate.isChecked(),
            "animation_duration_ms": self.anim_dur.value(),
            "hover_enabled":         self.hover_enabled.isChecked(),
            "hover_delay_ms":        self.hover_delay.value(),
            "return_to_center":      self.return_to_center.isChecked(),
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
        self.status.setText("All changes applied")


def _make_tile_icon(name: str, size: int = 22) -> QPixmap:
    """Draw an accent-chip stroke icon for a tile label, at any size."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(167, 139, 250, 33))     # accent chip
    rad = size * 0.27
    p.drawRoundedRect(QRectF(0, 0, size, size), rad, rad)

    pen = QPen(QColor("#c4b5fd"))
    pen.setWidthF(max(1.3, size * 0.064))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    k = size / 22.0
    a, b = 6 * k, 16 * k
    mid = (a + b) / 2

    def L(x1, y1, x2, y2):
        p.drawLine(round(x1), round(y1), round(x2), round(y2))

    if name == "layout":
        p.drawRect(QRectF(a, a, b - a, b - a))
        x = a + (b - a) * 0.6
        L(x, a, x, b)
    elif name == "monitor":
        sh = (b - a) * 0.7
        p.drawRoundedRect(QRectF(a, a, b - a, sh), 2 * k, 2 * k)
        L(mid, a + sh, mid, b)
        L(a + 2 * k, b, b - 2 * k, b)
    elif name == "motion":
        L(a, mid, a + 3 * k, mid)
        L(a + 3 * k, mid, mid - k, a + k)
        L(mid - k, a + k, mid + k, b - k)
        L(mid + k, b - k, b - 3 * k, mid)
        L(b - 3 * k, mid, b, mid)
    elif name == "cursor":
        L(b - 2 * k, a + 2 * k, a + 4 * k, b - 3 * k)
        L(a + 4 * k, b - 3 * k, a + 4 * k, b - 7 * k)
        L(a + 4 * k, b - 3 * k, a + 8 * k, b - 3 * k)
    elif name == "resize":
        p.drawRect(QRectF(a, a, b - a, b - a))
        p.drawRect(QRectF(a, a, (b - a) * 0.5, (b - a) * 0.5))
    elif name == "ban":
        p.drawEllipse(QRectF(a, a, b - a, b - a))
        L(a + 3 * k, a + 3 * k, b - 3 * k, b - 3 * k)
    elif name == "power":
        p.drawArc(QRectF(a + 0.5 * k, a + 0.5 * k, (b - a) - k, (b - a) - k),
                  120 * 16, 300 * 16)
        L(mid, a, mid, a + (b - a) * 0.45)
    elif name == "sliders":
        y1, y2 = a + 3 * k, b - 3 * k
        L(a, y1, b, y1)
        L(a, y2, b, y2)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#c4b5fd"))
        rd = 2 * k
        p.drawEllipse(QRectF(a + (b - a) * 0.62 - rd, y1 - rd, 2 * rd, 2 * rd))
        p.drawEllipse(QRectF(a + (b - a) * 0.30 - rd, y2 - rd, 2 * rd, 2 * rd))
    p.end()
    return pm


def _app_icon() -> QIcon:
    """The dark 4-tile mark, drawn in code (matches the tray icon)."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QBrush(QColor(28, 28, 28)))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(2, 2, 60, 60, 12, 12)
    # Top-left tile accent-tinted (encodes the "focused window" idea).
    tiles = ((12, 12, "#a78bfa"), (34, 12, None), (12, 34, None), (34, 34, None))
    for (x, y, accent) in tiles:
        if accent:
            p.setBrush(QBrush(QColor(accent)))
        else:
            p.setBrush(QBrush(QColor(255, 255, 255, 120)))
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
