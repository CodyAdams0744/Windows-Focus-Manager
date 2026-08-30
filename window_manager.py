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
import threading
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
# DWM attribute: non-zero → window is cloaked (other virtual desktop, etc.).
_DWMWA_CLOAKED = 14

# Shadow insets don't change during a window's lifetime.
# Cached here and cleared when the hwnd leaves self.states.
_shadow_cache: Dict[int, Tuple[int, int, int, int]] = {}

# Per-PID exe name cache — OpenProcess is expensive; exe names never change.
_pid_exe_cache: Dict[int, str] = {}
# Per-hwnd class name cache — class names never change for a given hwnd.
_class_cache: Dict[int, str] = {}
# Per-hmonitor work area cache, entries expire after a short TTL so
# resolution / taskbar / docking changes are picked up without a restart.
_work_area_cache: Dict[int, Tuple[Rect, float]] = {}
_WORK_AREA_TTL_S = 5.0
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
        # Guards self.states and the layout state.  The tiler thread holds it
        # for each poll/animation step; the tray thread (pause/resume/exit)
        # holds it while restoring — prevents mutation during iteration.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Pause / Resume (called from the system-tray menu)
    # ------------------------------------------------------------------

    def pause(self) -> None:
        """Stop tiling and restore all windows to their original positions.

        Called from the tray (Qt) thread — the lock waits for the tiler
        thread to finish its current frame before windows are restored."""
        if self._paused:
            return
        with self._lock:
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

                with self._lock:
                    if frame_start - self._last_poll >= config.POLL_INTERVAL_MS / 1000.0:
                        self._poll(frame_start)
                        self._last_poll = frame_start

                    self._step_animations(frame_start)

                elapsed_ms = (time.monotonic() - frame_start) * 1000.0
                time.sleep(max(0.0, config.ANIM_FRAME_MS - elapsed_ms) / 1000.0)

        except KeyboardInterrupt:
            log.info("Shutting down — restoring all windows.")
            with self._lock:
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
                tiled_now = _compute_current_tiled(mon_wins, mwa, mon)
                for hwnd, snap in tiled_now.items():
                    if hwnd in self.states:
                        _apply_rect(hwnd, snap, verify=False)

                # ── Step 2: animate to focus-based tiled target ────────
                targets = _layout_focus_targets(new_focus, mon_wins, mwa, mon)
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
            targets = _layout_even(mon_wins, mwa, mon)
        else:
            self._even_set.pop(mon, None)
            targets = _layout_focus_targets(desired, mon_wins, mwa, mon)

        # Gap-free: pre-snap to the current tiled state, then animate to target.
        tiled_now = _compute_current_tiled(mon_wins, mwa, mon)
        for hwnd, snap in tiled_now.items():
            if hwnd in self.states:
                _apply_rect(hwnd, snap, verify=False)
        for hwnd, tgt in targets.items():
            if hwnd in self.states:
                _begin_animation(self.states[hwnd], tgt, now,
                                 start_rect=tiled_now.get(hwnd))

    # ------------------------------------------------------------------
    # Manual placement — swap which window occupies which tile
    # ------------------------------------------------------------------

    def _mon_windows(self, mon: int) -> Dict[int, WindowState]:
        return {h: s for h, s in self.states.items() if _get_monitor(h) == mon}

    def _targets_for(self, mon: int, mon_wins: Dict[int, WindowState],
                     mwa: Rect) -> Dict[int, Rect]:
        """The resting layout for a monitor at its current focus state."""
        focus = self._focused.get(mon)
        if focus is None or focus == _EVEN or focus not in self.states:
            return _layout_even(mon_wins, mwa, mon)
        return _layout_focus_targets(focus, mon_wins, mwa, mon)

    def _animate_to(self, mon: int, mon_wins: Dict[int, WindowState],
                    targets: Dict[int, Rect], mwa: Rect, now: float) -> None:
        """Gap-free pre-snap, then animate every window to its target rect."""
        tiled_now = _compute_current_tiled(mon_wins, mwa, mon)
        for hwnd, snap in tiled_now.items():
            if hwnd in self.states:
                _apply_rect(hwnd, snap, verify=False)
        for hwnd, tgt in targets.items():
            if hwnd in self.states:
                _begin_animation(self.states[hwnd], tgt, now,
                                 start_rect=tiled_now.get(hwnd))

    def current_zones(self, mon: int) -> Dict[int, Rect]:
        """Return {hwnd: rect} for the monitor's resting layout — the drop
        zones a drag can target.  Thread-safe (callable from the Qt thread)."""
        with self._lock:
            mon_wins = self._mon_windows(mon)
            if not mon_wins:
                return {}
            return self._targets_for(mon, mon_wins, _monitor_work_area(mon))

    def swap_windows(self, mon: int, hwnd_a: int, hwnd_b: int) -> None:
        """Swap which tile two windows occupy (by swapping their layout anchors)
        and retile the monitor.  Thread-safe; called by the drag controller."""
        if hwnd_a == hwnd_b:
            return
        with self._lock:
            if hwnd_a not in self.states or hwnd_b not in self.states:
                return
            sa, sb = self.states[hwnd_a], self.states[hwnd_b]
            sa.original_rect, sb.original_rect = sb.original_rect, sa.original_rect
            mon_wins = self._mon_windows(mon)
            if not mon_wins:
                return
            mwa = _monitor_work_area(mon)
            targets = self._targets_for(mon, mon_wins, mwa)
            self._animate_to(mon, mon_wins, targets, mwa, time.monotonic())

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

def _compensate(hwnd: int, rect: Rect) -> Rect:
    """Extend a logical (visible) rect by the window's invisible DWM shadow
    insets so the visible frames of adjacent tiles touch exactly.  Top is
    left alone — modern apps have no shadow above the title bar."""
    ins = _cached_shadow_insets(hwnd)
    return (rect[0] - ins[0], rect[1], rect[2] + ins[2], rect[3] + ins[3])


# Cached hmonitor → monitor-rect (left, top), used to match config overrides.
_mon_pos_cache: Dict[int, Tuple[int, int]] = {}


def _monitor_override(mon: int) -> dict:
    """Per-monitor settings override from config.MONITOR_OVERRIDES, or {}."""
    if not mon or not config.MONITOR_OVERRIDES:
        return {}
    pos = _mon_pos_cache.get(mon)
    if pos is None:
        try:
            mr = win32api.GetMonitorInfo(mon)["Monitor"]
            pos = (mr[0], mr[1])
        except Exception:
            pos = (0, 0)
        _mon_pos_cache[mon] = pos
    for o in config.MONITOR_OVERRIDES:
        if o.get("left") == pos[0] and o.get("top") == pos[1]:
            return o
    return {}


def _effective_mode(mon: int) -> str:
    return _monitor_override(mon).get("layout_mode") or config.LAYOUT_MODE


def _effective_ratio(mon: int) -> float:
    return float(_monitor_override(mon).get("expand_ratio")
                 or config.EXPAND_RATIO)


def _focus_bias(ratio: float) -> float:
    """Weight multiplier for the focused window's band and cell, derived
    from the (per-monitor) expand ratio (65% → ~1.86) and capped by
    LAYOUT_BIAS_MAX so the focused window can't crush its neighbours."""
    e = min(0.85, max(0.35, ratio))
    return max(1.3, min(config.LAYOUT_BIAS_MAX, e / (1.0 - e)))


# ---------------------------------------------------------------------------
# Band engine (see docs/layout-design.md)
#
# A layout is an ordered list of bands (columns on landscape, rows on
# portrait; fgrid uses the transpose).  Windows are assigned to bands
# positionally and each band stacks its members along the other axis.
# Focus only stretches weighted boundaries — windows never relocate, which
# is what keeps hover mode free of feedback loops and makes the gap-free
# pre-snap measurable for every preset.
# ---------------------------------------------------------------------------

_PRESETS = ("strip", "quadrant", "triptych", "fgrid")

# Debounce state for count-based preset switching: mwa → [count, since, preset].
# Keyed by the work-area rect, which is unique per monitor.
_tier_state: Dict[Rect, list] = {}


def _tier_preset(n: int, mode: str) -> str:
    """Preset for n windows: fixed mode, or the tier table in auto mode."""
    if mode != "auto":
        return mode
    t = config.LAYOUT_TIERS
    if n <= 2:
        return t.get("1_2", "strip")
    if n <= 4:
        return t.get("3_4", "quadrant")
    if n <= 6:
        return t.get("5_6", "triptych")
    return t.get("7_plus", "fgrid")


def _resolve_preset(n: int, mwa: Rect, mon: int = 0) -> str:
    """Active preset for a monitor (honouring its override), debounced
    against window-count flapping so a short-lived dialog or splash screen
    can't thrash the layout."""
    mode = _effective_mode(mon)
    now = time.monotonic()
    st = _tier_state.get(mwa)
    if st is None:
        st = [n, now, _tier_preset(n, mode)]
        _tier_state[mwa] = st
        return st[2]
    if st[0] != n:        # count changed — restart the stability timer
        st[0] = n
        st[1] = now
    elif now - st[1] >= config.LAYOUT_DEBOUNCE_MS / 1000.0:
        st[2] = _tier_preset(n, mode)   # count stable — adopt its preset
    return st[2]


def _band_caps(n: int, k: int, large_first: bool) -> List[int]:
    """Distribute n windows across k bands.  With a remainder, some bands
    hold fewer windows (and therefore larger cells); large_first puts those
    bands first (left / top)."""
    base, extra = divmod(n, k)
    if large_first:
        return [base + (1 if i >= k - extra else 0) for i in range(k)]
    return [base + (1 if i < extra else 0) for i in range(k)]


def _preset_geometry(preset: str, mwa: Rect, n: int) -> Tuple[int, bool, bool]:
    """Return (n_bands, bands_are_columns, large_first) for this monitor."""
    landscape = (mwa[2] - mwa[0]) >= (mwa[3] - mwa[1])
    lf = config.LAYOUT_LARGE_FIRST
    if preset == "quadrant":
        return min(2, n), landscape, bool(lf.get("quadrant", True))
    if preset == "triptych":
        return min(3, n), landscape, bool(lf.get("triptych", True))
    if preset == "fgrid":
        return min(2, n), not landscape, bool(lf.get("fgrid", False))
    return n, landscape, False     # strip: one window per band


def _band_structure(
    mon_wins: Dict[int, WindowState],
    mwa: Rect,
    preset: str,
) -> Tuple[List[List[int]], bool]:
    """Assign windows to bands positionally.  Returns (bands, bands_are_cols)
    where bands is a list of ordered hwnd lists.

    Windows are sorted along the band axis by original-rect centre and dealt
    into bands by capacity, then each band is ordered along the stack axis.
    original_rect only changes on user drags (adopted while idle), so the
    assignment is sticky across focus changes — the hover-stability anchor.
    """
    n = len(mon_wins)
    k, cols, large_first = _preset_geometry(preset, mwa, n)

    def cx(kv):
        return (kv[1].original_rect[0] + kv[1].original_rect[2]) // 2

    def cy(kv):
        return (kv[1].original_rect[1] + kv[1].original_rect[3]) // 2

    primary = sorted(mon_wins.items(),
                     key=(lambda kv: (cx(kv), cy(kv))) if cols else
                         (lambda kv: (cy(kv), cx(kv))))
    bands: List[List[int]] = []
    idx = 0
    for cap in _band_caps(n, k, large_first):
        chunk = primary[idx:idx + cap]
        chunk.sort(key=cy if cols else cx)
        bands.append([h for h, _ in chunk])
        idx += cap
    return bands, cols


def _emit_bands(
    bands: List[List[int]],
    cols: bool,
    mwa: Rect,
    band_w: List[float],
    cell_w: Dict[int, float],
    gap: int,
) -> Dict[int, Rect]:
    """Emit shadow-compensated rects from band/cell weights.

    Boundaries are cumulative floats rounded at emission, so adjacent tiles
    share the exact same boundary pixel — gap-free at gap=0, evenly spaced
    otherwise.  Last band/cell snaps to the monitor edge exactly.
    """
    if cols:
        p0, p1, s0, s1 = mwa[0], mwa[2], mwa[1], mwa[3]
    else:
        p0, p1, s0, s1 = mwa[1], mwa[3], mwa[0], mwa[2]
    k = len(bands)
    psize = (p1 - p0) - (k - 1) * gap
    sum_b = sum(band_w) or 1.0

    targets: Dict[int, Rect] = {}
    pa = float(p0)
    for bi, band in enumerate(bands):
        pb = float(p1) if bi == k - 1 else pa + psize * band_w[bi] / sum_b
        m = len(band)
        ssize = (s1 - s0) - (m - 1) * gap
        sum_c = sum(cell_w[h] for h in band) or 1.0
        sa = float(s0)
        for ci, h in enumerate(band):
            sb = float(s1) if ci == m - 1 else sa + ssize * cell_w[h] / sum_c
            if cols:
                rect = (round(pa), round(sa), round(pb), round(sb))
            else:
                rect = (round(sa), round(pa), round(sb), round(pb))
            targets[h] = _compensate(h, rect)
            sa = sb + gap
        pa = pb + gap
    return targets


def _layout_bands_focus(
    focused_hwnd: Optional[int],
    mon_wins: Dict[int, WindowState],
    mwa: Rect,
    preset: str,
    ratio: float = 0.65,
) -> Dict[int, Rect]:
    """Focus layout for a band preset.  focused_hwnd=None gives the even
    layout (all weights 1) used by return-to-center."""
    n = len(mon_wins)
    if n == 0:
        return {}
    if n == 1:
        h = next(iter(mon_wins))
        return {h: _compensate(h, mwa)}
    bands, cols = _band_structure(mon_wins, mwa, preset)
    bias = _focus_bias(ratio)
    band_w = [bias if focused_hwnd in band else 1.0 for band in bands]
    cell_w = {h: (bias if h == focused_hwnd else 1.0) for h in mon_wins}
    return _emit_bands(bands, cols, mwa, band_w, cell_w, config.LAYOUT_GAP_PX)


def _current_tiled_bands(
    mon_wins: Dict[int, WindowState],
    mwa: Rect,
    preset: str,
) -> Dict[int, Rect]:
    """Pre-snap state for band presets: the same band structure with weights
    measured from the windows' current logical sizes.  Both this snap and
    the focus target share boundary structure, so the strip layout's
    gap-free animation guarantee carries over."""
    n = len(mon_wins)
    if n <= 1:
        return _layout_bands_focus(None, mon_wins, mwa, preset)
    bands, cols = _band_structure(mon_wins, mwa, preset)

    def logical(h, horizontal):
        s   = mon_wins[h]
        ins = _cached_shadow_insets(h)
        if horizontal:
            return max(1, s.current_rect[2] - s.current_rect[0] - ins[0] - ins[2])
        return max(1, s.current_rect[3] - s.current_rect[1] - ins[3])

    band_w: List[float] = []
    cell_w: Dict[int, float] = {}
    for band in bands:
        sizes = []
        for h in band:
            sizes.append(logical(h, cols))
            cell_w[h] = logical(h, not cols)
        band_w.append(sum(sizes) / len(sizes))
    return _emit_bands(bands, cols, mwa, band_w, cell_w, config.LAYOUT_GAP_PX)


def _layout_focus_targets(
    focused_hwnd: int,
    mon_wins: Dict[int, WindowState],
    mwa: Rect,
    mon: int = 0,
) -> Dict[int, Rect]:
    """Dispatch to the monitor's active preset (override-aware mode/tier
    table + count debounce) with its effective expand ratio."""
    preset = _resolve_preset(len(mon_wins), mwa, mon)
    ratio  = _effective_ratio(mon)
    if preset == "strip":
        return _layout_strip(focused_hwnd, mon_wins, mwa, ratio)
    return _layout_bands_focus(focused_hwnd, mon_wins, mwa, preset, ratio)


def _layout_strip(
    focused_hwnd: int,
    mon_wins: Dict[int, WindowState],
    mwa: Rect,
    ratio: float = 0.65,
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
            k = ratio / max(1e-6, 1.0 - ratio)
            effective_ratio = k / (k + n_others)
            focused_w = max(
                config.MIN_WINDOW_WIDTH,
                min(int(mw * effective_ratio),
                    mw - n_others * config.MIN_WINDOW_WIDTH),
            )
        else:
            focused_w = int(mw * ratio)
        remaining = mw - focused_w

        # All non-focused windows get an equal share (see symmetry note above).
        other_w = (remaining // n_others) if n_others else 0
        widths: Dict[int, int] = {focused_hwnd: focused_w}
        for h, _ in others:
            widths[h] = other_w

        targets: Dict[int, Rect] = {}
        x = mwa[0]
        for i, (h, _) in enumerate(ordered):
            w   = widths[h]
            ins = _cached_shadow_insets(h)
            # Extend each rect by the window's invisible left/right DWM shadow
            # insets so the *visible* frames of adjacent tiles touch exactly —
            # the same compensation the portrait branch applies to the bottom
            # edge.  x tracks logical (visible) positions; only the emitted
            # rect is widened, so insets never accumulate across tiles.
            r = mwa[2] if i == len(ordered) - 1 else x + w
            targets[h] = (x - ins[0], mwa[1], r + ins[2], mwa[3])
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
            k = ratio / max(1e-6, 1.0 - ratio)
            effective_ratio = k / (k + n_others)
            focused_h = max(
                config.MIN_WINDOW_HEIGHT,
                min(int(mh * effective_ratio),
                    mh - n_others * config.MIN_WINDOW_HEIGHT),
            )
        else:
            focused_h = int(mh * ratio)
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
    mon: int = 0,
) -> Dict[int, Rect]:
    """Even, gap-free split — every window gets an equal share of the monitor.

    Ordering and edge/shadow handling mirror the focus layouts so the
    return-to-center layout is visually consistent with focus tiling.
    """
    preset = _resolve_preset(len(mon_wins), mwa, mon)
    if preset != "strip":
        return _layout_bands_focus(None, mon_wins, mwa, preset,
                                   _effective_ratio(mon))

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
            ins = _cached_shadow_insets(h)
            r = mwa[2] if i == n - 1 else x + each
            # Same left/right shadow compensation as _layout_focus_targets.
            targets[h] = (x - ins[0], mwa[1], r + ins[2], mwa[3])
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
    mon: int = 0,
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

    Band presets get the same guarantee via _current_tiled_bands, which
    measures band/cell weights from the windows' current logical sizes.
    """
    preset = _resolve_preset(len(mon_wins), mwa, mon)
    if preset != "strip":
        return _current_tiled_bands(mon_wins, mwa, preset)

    mw = mwa[2] - mwa[0]
    mh = mwa[3] - mwa[1]

    if mw >= mh:   # ── Landscape ────────────────────────────────────────
        ordered = sorted(
            mon_wins.items(),
            key=lambda kv: (kv[1].original_rect[0] + kv[1].original_rect[2]) // 2,
        )
        n          = len(ordered)
        insets_map = {h: _cached_shadow_insets(h) for h, _ in ordered}
        # Strip the invisible left/right shadow insets so proportions are
        # computed from the logical (visible) width, not the physical rect.
        total_cw = sum(
            max(1, s.current_rect[2] - s.current_rect[0]
                   - insets_map[h][0] - insets_map[h][2])
            for h, s in ordered
        ) or 1
        min_w    = config.MIN_WINDOW_WIDTH   # hoist: avoid repeated attribute lookup in loop
        targets: Dict[int, Rect] = {}
        x      = mwa[0]
        budget = mw
        for i, (h, s) in enumerate(ordered):
            ins = insets_map[h]
            if i == n - 1:
                r = mwa[2]   # last tile always snaps to monitor edge
            else:
                cw     = max(1, s.current_rect[2] - s.current_rect[0]
                                - ins[0] - ins[2])
                n_left = n - i
                cap    = budget - (n_left - 1) * min_w
                w      = max(min_w, min(round(mw * cw / total_cw), cap))
                budget -= w
                r = x + w
            # Same left/right shadow compensation as _layout_focus_targets,
            # so the snap state and the animation target line up exactly.
            targets[h] = (x - ins[0], mwa[1], r + ins[2], mwa[3])
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
    now    = time.monotonic()
    cached = _work_area_cache.get(hmonitor)
    if cached is not None and now - cached[1] < _WORK_AREA_TTL_S:
        return cached[0]
    try:
        info = win32api.GetMonitorInfo(hmonitor)
        wa   = info["Work"]
        result: Rect = (wa[0], wa[1], wa[2], wa[3])
    except Exception:
        sw = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
        sh = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
        result = (0, 0, sw, sh)
    _work_area_cache[hmonitor] = (result, now)
    return result


def _is_cloaked(hwnd: int) -> bool:
    """True if DWM reports the window as cloaked — technically visible to
    IsWindowVisible but hidden from the user (another virtual desktop,
    shell-suspended UWP app, etc.)."""
    try:
        cloaked = ctypes.c_int(0)
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        return hr == 0 and bool(cloaked.value)
    except Exception:
        return False


def _quick_is_still_manageable(hwnd: int) -> bool:
    """Fast re-validation for windows already tracked as managed.

    Skips expensive class/exe/style queries — only rechecks conditions that
    can change after a window is first admitted (visibility, iconic,
    maximized, cloaked).
    """
    try:
        if not win32gui.IsWindowVisible(hwnd):
            return False
        if win32gui.IsIconic(hwnd):
            return False
        if win32gui.GetWindowPlacement(hwnd)[1] == win32con.SW_SHOWMAXIMIZED:
            return False
        # A window moved to another virtual desktop becomes cloaked but stays
        # "visible" — without this recheck it keeps consuming a tile slot.
        if _is_cloaked(hwnd):
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
    if _is_cloaked(hwnd):
        log.debug("Skipping cloaked window hwnd=%d  cls=%r", hwnd, cls)
        return False

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


