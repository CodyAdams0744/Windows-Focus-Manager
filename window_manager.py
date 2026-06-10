"""window_manager.py — click/focus-driven, per-monitor tiling window manager.

Behaviour
---------
• Windows sit at their natural sizes by default.

• When a window is brought to the foreground (click or alt-tab), all
  windows on that monitor are arranged as contiguous tiles that fill the
  entire work area — no gaps, no visible desktop background.

  Landscape monitor: tiles run left-to-right, every window fills full height.
  Portrait monitor:  tiles run top-to-bottom, every window fills full width.

  Focused window  → scaled share of the primary axis such that the
                    focused:other ratio equals EXPAND_RATIO:(1−EXPAND_RATIO).
  Other windows   → equal shares of the remaining space.  The focused
                    window shrinks gracefully as more windows are added.

• Gap-free animation: before starting a transition, all windows are
  instantly snapped to a tiled arrangement that exactly fills the monitor
  using their current proportional widths.  Because both the snap state and
  the target state are tiled, the shared edge between any two adjacent
  windows stays at the same interpolated position throughout the animation —
  the desktop is never visible, even mid-transition.

• Each monitor is fully independent.
• Fullscreen and maximised windows are ignored.
• Our own console/terminal window is never managed.
"""

import ctypes
import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

import win32api
import win32con
import win32gui
import win32process

import config

log = logging.getLogger(__name__)

Rect = Tuple[int, int, int, int]   # left, top, right, bottom

# HWND of this process's console window — used to exclude it from management.
_CONSOLE_HWND: int = int(ctypes.windll.kernel32.GetConsoleWindow())

# DWM attribute: visible frame bounds (excludes the invisible shadow border).
_DWMWA_EXTENDED_FRAME_BOUNDS = 9

# Shadow insets don't change during a window's lifetime.
# Cached here and cleared when the hwnd leaves self.states.
_shadow_cache: Dict[int, Tuple[int, int, int, int]] = {}

# Per-PID exe name cache — OpenProcess is expensive; exe names never change.
_pid_exe_cache: Dict[int, str] = {}
# Per-hwnd class name cache — class names never change for a given hwnd.
_class_cache: Dict[int, str] = {}
# Per-hmonitor work area cache — display topology rarely changes.
_work_area_cache: Dict[int, Rect] = {}
# Per-hwnd monitor handle cache — windows rarely jump between monitors.
_monitor_cache: Dict[int, int] = {}

# Sentinel _focused value meaning "this monitor is in the return-to-center
# even-tiled state" (no real window focused).  Never a valid hwnd.
_EVEN = -1


class _CRECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _shadow_insets(hwnd: int) -> Tuple[int, int, int, int]:
    """Return (left, top, right, bottom) invisible DWM shadow border sizes.

    GetWindowRect includes these transparent pixels; the visible frame
    (from DWMWA_EXTENDED_FRAME_BOUNDS) does not.  On most modern apps the
    insets are approximately (8, 0, 8, 8) — no shadow above the title bar.
    Returns (0, 0, 0, 0) if the query fails (e.g. for UWP apps).
    """
    try:
        wr = win32gui.GetWindowRect(hwnd)
        vr = _CRECT()
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS,
            ctypes.byref(vr), ctypes.sizeof(vr))
        if hr == 0:   # S_OK
            return (
                max(0, vr.left   - wr[0]),   # left inset
                max(0, vr.top    - wr[1]),   # top inset
                max(0, wr[2]     - vr.right), # right inset
                max(0, wr[3]     - vr.bottom),# bottom inset
            )
    except Exception:
        pass
    return (0, 0, 0, 0)


def _cached_shadow_insets(hwnd: int) -> Tuple[int, int, int, int]:
    """Return cached shadow insets, computing them on first access per hwnd."""
    if hwnd not in _shadow_cache:
        _shadow_cache[hwnd] = _shadow_insets(hwnd)
    return _shadow_cache[hwnd]


# ---------------------------------------------------------------------------
# Per-window state
# ---------------------------------------------------------------------------

@dataclass
class WindowState:
    hwnd:              int
    original_rect:     Rect   # Rect before we first touched it; refreshed while idle
    current_rect:      Rect   # Last rect we applied
    target_rect:       Rect   # Animation destination
    anim_start_rect:   Rect   # Animation source
    anim_start_time:   float  # Monotonic seconds when animation started
    resize_fail_count: int  = 0


# Session-only set of exe names that auto-excluded themselves via resize failures.
# Never written to config.json — the user retains full control.
_auto_excluded_exes: Set[str] = set()


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class WindowManager:
    def __init__(self) -> None:
        self.states: Dict[int, WindowState] = {}
        self._focused: Dict[int, Optional[int]] = {}   # monitor → expanded hwnd (or _EVEN)
        self._last_poll: float = 0.0
        self._paused: bool = False
        # Hover mode: per-monitor pending candidate and the time the cursor first landed on it.
        self._hover_candidate: Dict[int, Tuple[Optional[int], float]] = {}
        # Return-to-center: monitor → frozenset of hwnds last evened (for re-even on change).
        self._even_set: Dict[int, frozenset] = {}

    # ------------------------------------------------------------------
    # Pause / Resume (called from the system-tray menu)
    # ------------------------------------------------------------------

    def pause(self) -> None:
        """Stop tiling and restore all windows to their original positions."""
        if self._paused:
            return
        self._paused = True
        log.info("Window Focus Manager paused — restoring all windows.")
        self._restore_all()
        # Clear focus state so resume starts fresh.
        self._focused.clear()
        self._hover_candidate.clear()
        self._even_set.clear()

    def resume(self) -> None:
        """Re-enable tiling."""
        if not self._paused:
            return
        self._paused = False
        log.info("Window Focus Manager resumed.")

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        log.info("Window Focus Manager running.  Ctrl+C to exit.")

        # Raise Windows timer resolution to 1 ms so time.sleep(0.016) lands
        # within ±1 ms instead of ±15.6 ms (the OS default tick period).
        _winmm = ctypes.windll.winmm
        _winmm.timeBeginPeriod(1)

        try:
            while True:
                frame_start = time.monotonic()

                if frame_start - self._last_poll >= config.POLL_INTERVAL_MS / 1000.0:
                    self._poll(frame_start)
                    self._last_poll = frame_start

                self._step_animations(frame_start)

                elapsed_ms = (time.monotonic() - frame_start) * 1000.0
                time.sleep(max(0.0, config.ANIM_FRAME_MS - elapsed_ms) / 1000.0)

        except KeyboardInterrupt:
            log.info("Shutting down — restoring all windows.")
            self._restore_all()
        finally:
            _winmm.timeEndPeriod(1)

    # ------------------------------------------------------------------
    # Poll
    # ------------------------------------------------------------------

    def _poll(self, now: float) -> None:
        if self._paused:
            return

        # ── 1. Sync window registry ────────────────────────────────────
        # Fast-path: known windows only need visibility/iconic/maximized checks.
        # Full _is_manageable (expensive) only runs for windows not yet tracked.
        quick_valid: set = set()
        for hwnd in self.states:
            if _quick_is_still_manageable(hwnd):
                quick_valid.add(hwnd)

        new_wins    = _enumerate_managed(skip=frozenset(quick_valid))
        visible     = list(quick_valid) + new_wins
        visible_set = set(visible)

        for hwnd in list(self.states):
            if hwnd not in visible_set:
                _restore_window(self.states[hwnd])
                del self.states[hwnd]
                _shadow_cache.pop(hwnd, None)
                _class_cache.pop(hwnd, None)
                _monitor_cache.pop(hwnd, None)
                log.debug("Unregistered hwnd=%d", hwnd)

        for hwnd in visible:
            if hwnd not in self.states:
                try:
                    rect = win32gui.GetWindowRect(hwnd)
                except Exception:
                    continue
                self.states[hwnd] = WindowState(
                    hwnd=hwnd,
                    original_rect=rect, current_rect=rect,
                    target_rect=rect,   anim_start_rect=rect,
                    anim_start_time=now,
                )
                log.debug("Registered  hwnd=%d  %r", hwnd, _safe_title(hwnd))

        # Build per-poll monitor map — avoids repeated _get_monitor() syscalls.
        hwnd_to_mon: Dict[int, int] = {h: _get_monitor(h) for h in self.states}

        # ── 1b. One-time startup diagnostic ───────────────────────────
        if not hasattr(self, '_diagnosed'):
            self._diagnosed = True
            if self.states:
                by_mon: Dict[int, list] = {}
                for h, s in self.states.items():
                    m = hwnd_to_mon.get(h, 0)
                    by_mon.setdefault(m, []).append((h, s))
                log.info("Tracking %d window(s) across %d monitor(s):",
                         len(self.states), len(by_mon))
                for m, wins in by_mon.items():
                    mwa = _monitor_work_area(m)
                    mw  = mwa[2] - mwa[0]
                    mh  = mwa[3] - mwa[1]
                    orient = "landscape" if mw >= mh else "portrait"
                    log.info("  Monitor 0x%x  %dx%d  %s", m, mw, mh, orient)
                    for h, s in wins:
                        r = s.original_rect
                        log.info("    hwnd=%-8d  pos=(%d,%d,%d,%d)  %r",
                                 h, r[0], r[1], r[2], r[3], _safe_title(h))
            else:
                log.warning(
                    "No manageable windows found. "
                    "Set LOG_LEVEL='DEBUG' in config.py to see what is filtered."
                )

        # ── 2. Trigger window (click/foreground  OR  hover) ───────────────
        try:
            fg = win32gui.GetForegroundWindow()
        except Exception:
            fg = 0

        if config.HOVER_ENABLED:
            hover_hwnd = _get_cursor_hwnd()
            # Only treat the cursor's monitor as "active" when the cursor is
            # actually over a managed window.  If it's over the desktop/taskbar
            # or an unmanaged app, active_mon = 0 so all monitors still get
            # their normal housekeeping (closed-window cleanup, etc.).
            managed = _find_managed_hwnd(hover_hwnd, self.states) if hover_hwnd else None
            active_mon = hwnd_to_mon.get(managed, 0) if managed else 0
            log.debug("hover: raw_hwnd=%d  managed=%s  active_mon=0x%x",
                      hover_hwnd, managed, active_mon)
        else:
            hover_hwnd = 0
            active_mon = _get_monitor(fg) if fg else 0

        # ── 3. Per-monitor layout ──────────────────────────────────────
        monitors: Set[int] = set(hwnd_to_mon.values())
        monitors.discard(0)

        # Return-to-center: only active in hover mode.  When enabled, every
        # monitor without a focused window tiles evenly (handled separately so
        # the default code path below is unchanged).
        rtc = config.HOVER_ENABLED and config.RETURN_TO_CENTER

        for mon in monitors:
            if not _is_monitor_enabled(mon):
                continue
            mon_wins = {h: s for h, s in self.states.items()
                        if hwnd_to_mon.get(h) == mon}
            if not mon_wins:
                continue

            old_focus = self._focused.get(mon)

            if rtc:
                self._rtc_monitor(mon, mon_wins, old_focus, active_mon,
                                  hover_hwnd, now)
                continue

            if mon != active_mon:
                # Housekeeping for non-active monitors only.
                if old_focus is not None and old_focus not in self.states:
                    log.info("Monitor 0x%x: expanded window closed — restoring.", int(mon))
                    self._focused[mon] = None
                    for hwnd, tgt in _restore_targets(mon_wins).items():
                        _begin_animation(self.states[hwnd], tgt, now)
                elif old_focus is None:
                    _adopt_manual_resizes(mon_wins)
                continue

            # Active monitor — determine which window should be expanded.
            if config.HOVER_ENABLED:
                new_focus = _resolve_hover(
                    mon, hover_hwnd, mon_wins, self._hover_candidate, now, old_focus)
            else:
                new_focus = fg if fg in mon_wins else None

            if old_focus is None and new_focus is None:
                _adopt_manual_resizes(mon_wins)

            if new_focus == old_focus:
                continue

            # Before retiling, adopt any manual drags/resizes that happened
            # since the last animation settled.  This makes the new layout
            # respect the user's last manual arrangement (ordering, size hints).
            _adopt_manual_resizes(mon_wins)

            self._focused[mon] = new_focus
            log.info("Monitor 0x%x  focus: %s  →  %s", int(mon),
                     _safe_title(old_focus) if old_focus else "<none>",
                     _safe_title(new_focus) if new_focus else "<none>")

            mwa = _monitor_work_area(mon)

            if new_focus is not None:
                # ── Step 1: snap all windows to a gap-free tiled state ─
                # Both the snap (start) and the focus target (end) are tiled,
                # so during animation every adjacent pair shares the same
                # interpolated boundary — the desktop never peeks through.
                tiled_now = _compute_current_tiled(mon_wins, mwa)
                for hwnd, snap in tiled_now.items():
                    if hwnd in self.states:
                        _apply_rect(hwnd, snap, verify=False)

                # ── Step 2: animate to focus-based tiled target ────────
                targets = _layout_focus_targets(new_focus, mon_wins, mwa)
                for hwnd, tgt in targets.items():
                    if hwnd in self.states:
                        _begin_animation(self.states[hwnd], tgt, now,
                                         start_rect=tiled_now.get(hwnd))
            else:
                # Foreground moved to something unmanaged — restore originals.
                for hwnd, tgt in _restore_targets(mon_wins).items():
                    if hwnd in self.states:
                        _begin_animation(self.states[hwnd], tgt, now)

    # ------------------------------------------------------------------
    # Return-to-center: a monitor with no focused window tiles evenly
    # ------------------------------------------------------------------

    def _rtc_monitor(self, mon: int, mon_wins: Dict[int, WindowState],
                     old_focus, active_mon: int, hover_hwnd: int,
                     now: float) -> None:
        win_set = frozenset(mon_wins)

        # Only the monitor under the cursor can hold a hover-focused window.
        if mon == active_mon and config.HOVER_ENABLED:
            new_focus = _resolve_hover(
                mon, hover_hwnd, mon_wins, self._hover_candidate, now, old_focus)
        else:
            new_focus = None
        # A focus that points at a gone window (or the sentinel) is "no focus".
        if new_focus is not None and new_focus not in self.states:
            new_focus = None

        desired = _EVEN if new_focus is None else new_focus
        # Re-even if the window set changed while already evened.
        need_reeven = (desired == _EVEN and old_focus == _EVEN
                       and self._even_set.get(mon) != win_set)
        if desired == old_focus and not need_reeven:
            return

        _adopt_manual_resizes(mon_wins)
        self._focused[mon] = desired
        mwa = _monitor_work_area(mon)

        if desired == _EVEN:
            self._even_set[mon] = win_set
            targets = _layout_even(mon_wins, mwa)
        else:
            self._even_set.pop(mon, None)
            targets = _layout_focus_targets(desired, mon_wins, mwa)

        # Gap-free: pre-snap to the current tiled state, then animate to target.
        tiled_now = _compute_current_tiled(mon_wins, mwa)
        for hwnd, snap in tiled_now.items():
            if hwnd in self.states:
                _apply_rect(hwnd, snap, verify=False)
        for hwnd, tgt in targets.items():
            if hwnd in self.states:
                _begin_animation(self.states[hwnd], tgt, now,
                                 start_rect=tiled_now.get(hwnd))

    # ------------------------------------------------------------------
    # Animation step — ~60 fps
    # ------------------------------------------------------------------

    def _step_animations(self, now: float) -> None:
        updates: List[Tuple[int, Rect]] = []   # (hwnd, rect) to apply this frame
        finished: List[int] = []               # hwnds whose animation just ended

        for hwnd, state in self.states.items():
            if state.current_rect == state.target_rect:
                continue

            dur = config.ANIMATION_DURATION_MS
            t   = min(1.0, (now - state.anim_start_time) * 1000.0 / dur) if dur > 0 else 1.0

            if not config.ANIMATE or t >= 1.0:
                state.current_rect = state.target_rect
                updates.append((hwnd, state.target_rect))
                finished.append(hwnd)
                continue

            new_rect = _lerp_rect(state.anim_start_rect, state.target_rect, _ease(t))
            if new_rect == state.current_rect:
                continue
            state.current_rect = new_rect
            updates.append((hwnd, new_rect))

        if not updates:
            return

        # Apply all position changes atomically so every window moves in the
        # same DWM composition frame — eliminates the inter-window tearing/jitter.
        try:
            hdwp = win32gui.BeginDeferWindowPos(len(updates))
            for hwnd, rect in updates:
                l, t_val, r, b = rect
                hdwp = win32gui.DeferWindowPos(
                    hdwp, hwnd, 0, l, t_val, r - l, b - t_val,
                    win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE,
                )
            win32gui.EndDeferWindowPos(hdwp)
        except Exception as exc:
            log.debug("DeferWindowPos failed (%s) — falling back to individual calls", exc)
            for hwnd, rect in updates:
                _apply_rect(hwnd, rect, verify=False)

        # Sync to DWM's composition cycle so the batch lands on a vsync boundary.
        try:
            ctypes.windll.dwmapi.DwmFlush()
        except Exception:
            pass

        # Resize-resistance check: after 3 failures, auto-exclude the exe for
        # the rest of the session so it stops consuming a tile slot.
        for hwnd in finished:
            state = self.states.get(hwnd)
            if state is None or state.resize_fail_count >= 3:
                continue
            try:
                actual = win32gui.GetWindowRect(hwnd)
                if not any(abs(actual[i] - state.target_rect[i]) > 32
                           for i in range(4)):
                    continue   # resize succeeded — nothing to do
                state.resize_fail_count += 1
                if state.resize_fail_count >= 3:
                    exe = _get_exe_name(hwnd)
                    if exe:
                        _auto_excluded_exes.add(exe)
                        log.info(
                            "Auto-excluded %r after repeated resize failures "
                            "(hwnd=%d  %r)", exe, hwnd, _safe_title(hwnd))
                    else:
                        log.warning("Window resists resize: hwnd=%d  %r",
                                    hwnd, _safe_title(hwnd))
                else:
                    log.debug("Resize resistance %d/3: hwnd=%d  %r",
                              state.resize_fail_count, hwnd, _safe_title(hwnd))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Restore on exit
    # ------------------------------------------------------------------

    def _restore_all(self) -> None:
        for state in self.states.values():
            _restore_window(state)
        log.info("All windows restored.")


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

def _layout_focus_targets(
    focused_hwnd: int,
    mon_wins: Dict[int, WindowState],
    mwa: Rect,
) -> Dict[int, Rect]:
    """
    True tiling layout: windows are placed as contiguous, gap-free tiles.

    Landscape → left-to-right tiles, every window spans full height.
    Portrait  → top-to-bottom tiles, every window spans full width.

    Tiles are ordered by each window's original-rect centre on the primary
    axis, so the visual left-to-right / top-to-bottom order is stable and
    predictable across every focus switch.

    The focused window receives EXPAND_RATIO of the primary axis; the
    remaining (1 − EXPAND_RATIO) is distributed among the others in
    proportion to their original primary-axis sizes.  Integer rounding
    is absorbed by the last tile (it is snapped to the monitor edge),
    so the tiles always fill the work area exactly.
    """
    mw = mwa[2] - mwa[0]
    mh = mwa[3] - mwa[1]

    if mw >= mh:   # ── Landscape ────────────────────────────────────────
        ordered = sorted(
            mon_wins.items(),
            key=lambda kv: (kv[1].original_rect[0] + kv[1].original_rect[2]) // 2,
        )
        others   = [(h, s) for h, s in ordered if h != focused_hwnd]
        n_others = len(others)

        # Scale the effective expand ratio so the focused:other size ratio stays
        # constant as window count grows.  With 2 windows this equals EXPAND_RATIO
        # exactly; with more windows the focused window shrinks proportionally so
        # each non-focused window remains a usable size.
        #
        # Formula derivation: let k = E/(1-E) (the focused:other ratio from config).
        # With N total windows: focused = k/(k + N-1), each other = 1/(k + N-1).
        if n_others:
            k = config.EXPAND_RATIO / max(1e-6, 1.0 - config.EXPAND_RATIO)
            effective_ratio = k / (k + n_others)
            focused_w = max(
                config.MIN_WINDOW_WIDTH,
                min(int(mw * effective_ratio),
                    mw - n_others * config.MIN_WINDOW_WIDTH),
            )
        else:
            focused_w = int(mw * config.EXPAND_RATIO)
        remaining = mw - focused_w

        # All non-focused windows get an equal share (see symmetry note above).
        other_w = (remaining // n_others) if n_others else 0
        widths: Dict[int, int] = {focused_hwnd: focused_w}
        for h, _ in others:
            widths[h] = other_w

        targets: Dict[int, Rect] = {}
        x = mwa[0]
        for i, (h, _) in enumerate(ordered):
            w = widths[h]
            r = mwa[2] if i == len(ordered) - 1 else x + w
            targets[h] = (x, mwa[1], r, mwa[3])
            x = r
        return targets

    else:           # ── Portrait ─────────────────────────────────────────
        ordered = sorted(
            mon_wins.items(),
            key=lambda kv: (kv[1].original_rect[1] + kv[1].original_rect[3]) // 2,
        )
        others   = [(h, s) for h, s in ordered if h != focused_hwnd]
        n_others = len(others)

        # Same constant-ratio scaling as landscape.
        if n_others:
            k = config.EXPAND_RATIO / max(1e-6, 1.0 - config.EXPAND_RATIO)
            effective_ratio = k / (k + n_others)
            focused_h = max(
                config.MIN_WINDOW_HEIGHT,
                min(int(mh * effective_ratio),
                    mh - n_others * config.MIN_WINDOW_HEIGHT),
            )
        else:
            focused_h = int(mh * config.EXPAND_RATIO)
        remaining = mh - focused_h

        # Equal share for each non-focused window.
        other_h = (remaining // n_others) if n_others else 0
        heights: Dict[int, int] = {focused_hwnd: focused_h}
        for h, _ in others:
            heights[h] = other_h

        if log.isEnabledFor(logging.DEBUG):
            log.debug("Portrait layout  mwa=%r  mh=%d  focused_h=%d  other_h=%d",
                      mwa, mh, focused_h, other_h)

        targets: Dict[int, Rect] = {}
        y = mwa[1]
        for i, (h, _) in enumerate(ordered):
            hh     = heights[h]
            shadow = _cached_shadow_insets(h)[3]
            # Every tile (including the last) extends its rect bottom by the
            # window's invisible DWM shadow border so the visible bottom of
            # each tile aligns exactly with the visible top of the next tile.
            # For the last tile this pushes the rect slightly past mwa[3] into
            # the taskbar gap — the shadow is transparent so it is invisible,
            # but without it the last tile leaves a visible gap at the bottom.
            b = mwa[3] + shadow if i == len(ordered) - 1 else y + hh + shadow
            targets[h] = (mwa[0], y, mwa[2], b)
            if log.isEnabledFor(logging.DEBUG):
                log.debug("  tile[%d] hwnd=%-8d  y=%-5d  h=%-5d  shadow=%d  %r",
                          i, h, y, hh, shadow, _safe_title(h))
            y = y + hh   # next tile's logical top (not y=b — avoids accumulating shadow)
        return targets


def _layout_even(
    mon_wins: Dict[int, WindowState],
    mwa: Rect,
) -> Dict[int, Rect]:
    """Even, gap-free split — every window gets an equal share of the monitor.

    Ordering and edge/shadow handling mirror _layout_focus_targets so the
    return-to-center layout is visually consistent with focus tiling.
    """
    mw = mwa[2] - mwa[0]
    mh = mwa[3] - mwa[1]
    n = len(mon_wins)
    if n == 0:
        return {}

    if mw >= mh:   # ── Landscape — equal widths ─────────────────────────
        ordered = sorted(
            mon_wins.items(),
            key=lambda kv: (kv[1].original_rect[0] + kv[1].original_rect[2]) // 2,
        )
        each = mw // n
        targets: Dict[int, Rect] = {}
        x = mwa[0]
        for i, (h, _) in enumerate(ordered):
            r = mwa[2] if i == n - 1 else x + each
            targets[h] = (x, mwa[1], r, mwa[3])
            x = r
        return targets

    else:           # ── Portrait — equal heights ─────────────────────────
        ordered = sorted(
            mon_wins.items(),
            key=lambda kv: (kv[1].original_rect[1] + kv[1].original_rect[3]) // 2,
        )
        each = mh // n
        targets: Dict[int, Rect] = {}
        y = mwa[1]
        for i, (h, _) in enumerate(ordered):
            shadow = _cached_shadow_insets(h)[3]
            b = mwa[3] + shadow if i == n - 1 else y + each + shadow
            targets[h] = (mwa[0], y, mwa[2], b)
            y = y + each
        return targets


def _compute_current_tiled(
    mon_wins: Dict[int, WindowState],
    mwa: Rect,
) -> Dict[int, Rect]:
    """
    Arrange windows as contiguous tiles using their *current* proportional
    widths (or heights for portrait), ordered by original-rect centre.

    This is applied as an instant pre-snap before every focus animation.
    Because both this snap state and the animation target are tiled layouts,
    the shared boundary between adjacent windows interpolates identically
    in both rects throughout the animation — the desktop is never exposed.

    Edge case: if a window is already mid-animation its current_rect is
    the mid-point, so the snap lands exactly where it is visually and the
    new animation continues from there seamlessly.
    """
    mw = mwa[2] - mwa[0]
    mh = mwa[3] - mwa[1]

    if mw >= mh:   # ── Landscape ────────────────────────────────────────
        ordered = sorted(
            mon_wins.items(),
            key=lambda kv: (kv[1].original_rect[0] + kv[1].original_rect[2]) // 2,
        )
        n        = len(ordered)
        total_cw = sum(max(1, s.current_rect[2] - s.current_rect[0])
                       for _, s in ordered) or 1
        min_w    = config.MIN_WINDOW_WIDTH   # hoist: avoid repeated attribute lookup in loop
        targets: Dict[int, Rect] = {}
        x      = mwa[0]
        budget = mw
        for i, (h, s) in enumerate(ordered):
            if i == n - 1:
                r = mwa[2]   # last tile always snaps to monitor edge
            else:
                cw     = max(1, s.current_rect[2] - s.current_rect[0])
                n_left = n - i
                cap    = budget - (n_left - 1) * min_w
                w      = max(min_w, min(round(mw * cw / total_cw), cap))
                budget -= w
                r = x + w
            targets[h] = (x, mwa[1], r, mwa[3])
            x = r
        return targets

    else:           # ── Portrait ─────────────────────────────────────────
        ordered = sorted(
            mon_wins.items(),
            key=lambda kv: (kv[1].original_rect[1] + kv[1].original_rect[3]) // 2,
        )
        n = len(ordered)
        # Strip previously-added shadow insets from heights so proportions
        # are computed from the logical (visible) height, not the physical rect.
        insets_map = {h: _cached_shadow_insets(h) for h, _ in ordered}
        total_ch = sum(
            max(1, s.current_rect[3] - s.current_rect[1] - insets_map[h][3])
            for h, s in ordered
        ) or 1
        min_h    = config.MIN_WINDOW_HEIGHT  # hoist: avoid repeated attribute lookup in loop
        targets: Dict[int, Rect] = {}
        y      = mwa[1]
        budget = mh
        for i, (h, s) in enumerate(ordered):
            logical_h = max(1, s.current_rect[3] - s.current_rect[1] - insets_map[h][3])
            shadow    = insets_map[h][3]
            if i == n - 1:
                tile_h = mwa[3] - y
                b      = mwa[3] + shadow
            else:
                n_left = n - i
                cap    = budget - (n_left - 1) * min_h
                tile_h = max(min_h,
                             min(round(mh * logical_h / total_ch), cap))
                budget -= tile_h
                b = y + tile_h + shadow
            targets[h] = (mwa[0], y, mwa[2], b)
            y = y + tile_h
        return targets


def _restore_targets(mon_wins: Dict[int, WindowState]) -> Dict[int, Rect]:
    return {hwnd: state.original_rect for hwnd, state in mon_wins.items()}


def _adopt_manual_resizes(mon_wins: Dict[int, WindowState]) -> None:
    """While a monitor is idle and animations are settled, track manual resizes."""
    for hwnd, state in mon_wins.items():
        if state.current_rect != state.target_rect:
            continue
        try:
            actual = win32gui.GetWindowRect(hwnd)
            if actual != state.original_rect and actual != (0, 0, 0, 0):
                log.debug("Adopted manual resize for hwnd=%d", hwnd)
                # The window may have been dragged to a different monitor; drop
                # its cached monitor handle so the next poll re-resolves it.
                _monitor_cache.pop(hwnd, None)
                state.original_rect   = actual
                state.current_rect    = actual
                state.target_rect     = actual   # prevent _step_animations snapping back
                state.anim_start_rect = actual
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Win32 helpers
# ---------------------------------------------------------------------------

def _get_cursor_hwnd() -> int:
    """Return the window handle under the mouse cursor, or 0 on failure.

    Returns the raw child handle from WindowFromPoint so that
    _find_managed_hwnd can walk the parent chain upward to find the
    registered top-level window.  (Walking to GA_ROOT first and then
    trying to walk further up is a no-op because GetParent(root) == 0.)
    """
    try:
        pt   = win32api.GetCursorPos()
        hwnd = win32gui.WindowFromPoint(pt)
        return int(hwnd) if hwnd else 0
    except Exception:
        return 0


def _find_managed_hwnd(hwnd: int, mon_wins: Dict[int, "WindowState"]) -> Optional[int]:
    """
    Given any window handle (possibly a child control), find the nearest ancestor
    that is a managed top-level window.  Returns None if none is found.

    This is needed because WindowFromPoint returns child controls, and even
    GetAncestor(GA_ROOT) can return a different object than what EnumWindows
    registered for complex app frameworks (Electron, UWP, etc.).
    """
    h = hwnd
    for _ in range(12):          # cap the walk depth
        if h in mon_wins:
            return h
        try:
            parent = win32gui.GetParent(h)
        except Exception:
            break
        if not parent or parent == h:
            break
        h = parent
    return None


def _resolve_hover(
    mon: int,
    cursor_hwnd: int,
    mon_wins: Dict[int, "WindowState"],
    candidates: Dict[int, Tuple[Optional[int], float]],
    now: float,
    old_focus: Optional[int],
) -> Optional[int]:
    """
    Hover trigger logic with configurable delay.

    Tracks which managed window the cursor is resting on.  Only commits the
    expansion once the cursor has stayed on the same window for HOVER_DELAY_MS
    — this prevents jitter when moving the mouse quickly across windows.

    Returns the hwnd that should be expanded.  Returns `old_focus` unchanged
    when the delay hasn't expired yet or the cursor is over nothing managed,
    so the caller's `if new_focus == old_focus: continue` skips retiling.
    """
    # Resolve the raw cursor hwnd to a managed window, walking parents if needed.
    raw = _find_managed_hwnd(cursor_hwnd, mon_wins) if cursor_hwnd else None

    cand_hwnd, t_start = candidates.get(mon, (None, now))

    if raw != cand_hwnd:
        # Cursor moved to a different window — restart the hover timer.
        candidates[mon] = (raw, now)
        return old_focus   # keep current layout until delay expires

    # Cursor is still on the same window (or still over nothing).
    elapsed_ms = (now - t_start) * 1000.0
    if elapsed_ms >= config.HOVER_DELAY_MS:
        if raw is not None:
            return raw       # delay satisfied — expand the hovered window
        else:
            return None      # cursor has been off managed windows long enough — restore

    return old_focus         # still within the delay period


def _is_monitor_enabled(hmonitor) -> bool:
    """Returns True if this monitor should be managed by the tiler."""
    if not config.ENABLED_MONITORS:
        return True  # empty / None → all monitors enabled
    try:
        mr = win32api.GetMonitorInfo(hmonitor)["Monitor"]
        return any(
            e["left"] == mr[0] and e["top"] == mr[1]
            for e in config.ENABLED_MONITORS
        )
    except Exception:
        return True   # fail-open


def _get_monitor(hwnd: int) -> int:
    # Always query fresh — the cache is intentionally not read here because a
    # window dragged to a different monitor would return a stale handle and cause
    # WFM to tile the window back onto the wrong monitor.  MonitorFromWindow is
    # a cheap kernel call; the per-poll overhead for ~N windows is negligible.
    try:
        mon = int(win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST))
    except Exception:
        mon = 0
    if mon:
        _monitor_cache[hwnd] = mon
    return mon


def _monitor_work_area(hmonitor: int) -> Rect:
    if hmonitor in _work_area_cache:
        return _work_area_cache[hmonitor]
    try:
        info = win32api.GetMonitorInfo(hmonitor)
        wa   = info["Work"]
        result: Rect = (wa[0], wa[1], wa[2], wa[3])
    except Exception:
        sw = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
        sh = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
        result = (0, 0, sw, sh)
    _work_area_cache[hmonitor] = result
    return result


def _quick_is_still_manageable(hwnd: int) -> bool:
    """Fast re-validation for windows already tracked as managed.

    Skips expensive class/exe/style/DWM queries — only rechecks conditions
    that can change after a window is first admitted (visibility, iconic, maximized).
    """
    try:
        if not win32gui.IsWindowVisible(hwnd):
            return False
        if win32gui.IsIconic(hwnd):
            return False
        if win32gui.GetWindowPlacement(hwnd)[1] == win32con.SW_SHOWMAXIMIZED:
            return False
        return True
    except Exception:
        return False


def _enumerate_managed(skip: frozenset = frozenset()) -> List[int]:
    results: List[int] = []

    def _cb(hwnd: int, _) -> bool:
        try:
            if hwnd not in skip and _is_manageable(hwnd):
                results.append(hwnd)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception as exc:
        log.error("EnumWindows: %s", exc)
    return results


_DWMWA_CLOAKED = 14   # DwmGetWindowAttribute attribute: non-zero → window is cloaked


def _get_exe_name(hwnd: int) -> str:
    """Return the lowercase executable filename (e.g. 'spotify.exe') for hwnd.

    Returns an empty string on any failure so callers can treat it as
    "unknown" without crashing.
    """
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid in _pid_exe_cache:
            return _pid_exe_cache[pid]
        h = win32api.OpenProcess(
            win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
            False, pid)
        try:
            path = win32process.GetModuleFileNameEx(h, 0)
            name = os.path.basename(path).lower()
        finally:
            win32api.CloseHandle(h)
        _pid_exe_cache[pid] = name
        return name
    except Exception:
        return ""


def _get_class_name(hwnd: int) -> str:
    if hwnd not in _class_cache:
        try:
            _class_cache[hwnd] = win32gui.GetClassName(hwnd)
        except Exception:
            _class_cache[hwnd] = ""
    return _class_cache[hwnd]


def _is_manageable(hwnd: int) -> bool:
    if not win32gui.IsWindowVisible(hwnd):
        return False
    if win32gui.IsIconic(hwnd):
        return False

    # Never manage our own console/terminal window.
    if _CONSOLE_HWND and hwnd == _CONSOLE_HWND:
        return False

    cls = _get_class_name(hwnd)
    if not cls or cls in config.SKIP_CLASSES:
        return False

    # Cheap window-style filters first — these reject the majority of
    # non-app windows (menus, tooltips, palettes, dialogs) using only
    # GetWindowLong, before we pay for the title/exe/DWM syscalls below.
    try:
        style   = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        exstyle = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    except Exception:
        return False

    # Require a title bar — filters menus, tooltips, and helper windows.
    if not (style & win32con.WS_CAPTION):
        return False

    # Tool windows (WS_EX_TOOLWINDOW) are floating secondary windows
    # (palettes, toolbars) that don't appear in Alt+Tab — skip them.
    if exstyle & win32con.WS_EX_TOOLWINDOW:
        log.debug("Skipping tool window hwnd=%d  cls=%r", hwnd, cls)
        return False

    # Skip owned windows (dialogs, secondary windows) — only manage
    # top-level application windows.
    try:
        if ctypes.windll.user32.GetWindow(hwnd, 4):  # GW_OWNER = 4
            return False
    except Exception:
        pass

    # User-defined exclusions: skip by window title substring.
    if config.SKIP_TITLES:
        try:
            title = win32gui.GetWindowText(hwnd).lower()
            if any(s.lower() in title for s in config.SKIP_TITLES):
                log.debug("Skipping excluded title hwnd=%d  title=%r", hwnd, title)
                return False
        except Exception:
            pass

    # User-defined and auto-exclusions by executable name — single
    # _get_exe_name call covers both lists (OpenProcess is expensive).
    if config.SKIP_EXE or _auto_excluded_exes:
        exe = _get_exe_name(hwnd)
        if exe:
            if config.SKIP_EXE and exe in config.SKIP_EXE:
                log.debug("Skipping excluded exe hwnd=%d  exe=%r", hwnd, exe)
                return False
            if _auto_excluded_exes and exe in _auto_excluded_exes:
                return False

    # Cloaked windows are technically "visible" (IsWindowVisible returns True)
    # but are hidden by DWM — background UWP apps, windows on other virtual
    # desktops, shell-suspended apps, etc.  They pass every other filter but
    # are invisible to the user and create phantom tile slots.
    try:
        cloaked = ctypes.c_int(0)
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        if hr == 0 and cloaked.value:
            log.debug("Skipping cloaked window hwnd=%d  cls=%r  cloaked=0x%x",
                      hwnd, cls, cloaked.value)
            return False
    except Exception:
        pass

    try:
        if win32gui.GetWindowPlacement(hwnd)[1] == win32con.SW_SHOWMAXIMIZED:
            return False
    except Exception:
        pass

    try:
        rect = win32gui.GetWindowRect(hwnd)
    except Exception:
        return False
    w = rect[2] - rect[0]
    h = rect[3] - rect[1]
    if w < config.MIN_WINDOW_WIDTH or h < config.MIN_WINDOW_HEIGHT:
        return False

    # Window center must lie on an actual monitor — excludes off-screen
    # or ghost windows (e.g. background processes parked off-screen).
    try:
        cx = (rect[0] + rect[2]) // 2
        cy = (rect[1] + rect[3]) // 2
        if not win32api.MonitorFromPoint((cx, cy), win32con.MONITOR_DEFAULTTONULL):
            log.debug("Skipping off-screen window hwnd=%d  cls=%r", hwnd, cls)
            return False
    except Exception:
        pass

    # Skip fullscreen windows (covers entire monitor).
    try:
        mon = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        mr  = win32api.GetMonitorInfo(mon)["Monitor"]
        if w >= mr[2] - mr[0] and h >= mr[3] - mr[1]:
            return False
    except Exception:
        pass

    return True


def _begin_animation(
    state: WindowState,
    target: Rect,
    now: float,
    start_rect: Optional[Rect] = None,
) -> None:
    """
    Start an animation toward `target`.

    If `start_rect` is provided it is used as the animation origin (used
    after a pre-snap so the start is a known tiled position rather than
    whatever GetWindowRect happens to return).  Otherwise the current
    Win32 rect is read to handle mid-flight reversals correctly.
    """
    if start_rect is not None:
        state.anim_start_rect = start_rect
        state.current_rect    = start_rect
    else:
        try:
            actual = win32gui.GetWindowRect(state.hwnd)
        except Exception:
            actual = state.current_rect
        state.anim_start_rect = actual
        state.current_rect    = actual
    state.target_rect     = target
    state.anim_start_time = now


def _restore_window(state: WindowState) -> None:
    if state.current_rect == state.original_rect:
        return
    try:
        _apply_rect(state.hwnd, state.original_rect, verify=False)
    except Exception:
        pass


def _apply_rect(hwnd: int, rect: Rect, verify: bool = True) -> bool:
    l, t, r, b = rect
    try:
        win32gui.SetWindowPos(
            hwnd, 0, l, t, r - l, b - t,
            win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE,
        )
    except Exception:
        return False
    if not verify:
        return True
    try:
        actual = win32gui.GetWindowRect(hwnd)
        return all(abs(actual[i] - rect[i]) <= 8 for i in range(4))
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------

def _lerp_rect(a: Rect, b: Rect, t: float) -> Rect:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(4))  # type: ignore[return-value]


def _ease(t: float) -> float:
    """
    Quintic smootherstep — C² continuity at both endpoints.
    Zero velocity AND zero acceleration at t=0 and t=1, giving a silky
    feel with no perceptible 'click' at start or finish.

        f(t) = 6t⁵ − 15t⁴ + 10t³
    """
    return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)


def _safe_title(hwnd: Optional[int]) -> str:
    if hwnd is None:
        return "<none>"
    try:
        return win32gui.GetWindowText(hwnd) or "<no title>"
    except Exception:
        return "<err>"


