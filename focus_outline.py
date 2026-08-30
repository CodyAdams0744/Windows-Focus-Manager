"""focus_outline.py — a subtle highlight around a window while the tiler moves it.

When ``config.FOCUS_OUTLINE_ENABLED`` is on, a thin border is drawn around each
focused window while :class:`window_manager.WindowManager` animates it into its
tiled position, so it is obvious the move is app-driven.  Opt-in; colour,
opacity and width are read live from ``config`` so Settings changes apply
without a restart.

Implementation — native Win32 layered popups
--------------------------------------------
The outline is one borderless ``WS_POPUP`` layered window per focused window,
made hollow with ``SetWindowRgn`` (outer rect minus an inset inner rect) so only
a frame is painted.  Native windows live in the same physical-pixel space the
tiler uses, so the payload rects can be applied verbatim — no Qt/DPI conversion.

Threading model
---------------
* **Tiler thread** — calls :func:`_observer` once per animation frame with
  ``{hwnd: Rect}``.  It only copies that into a lock-protected slot and posts a
  message to a message-only window; it never creates or touches a Win32 window.
* **Outline thread** — a dedicated daemon running a classic
  ``GetMessage``/``DispatchMessage`` loop.  It owns every outline window and
  creates / moves / reshapes / fades / destroys them in response to the posted
  sync messages and ``WM_TIMER`` (fade-out).  Daemon, so process exit is clean;
  it also unregisters the class and destroys windows on ``WM_QUIT``.

Live config re-read
-------------------
:func:`_observer` checks ``config.FOCUS_OUTLINE_ENABLED`` on every frame and
forces the slot empty when off.  Colour / opacity / width are re-read every time
an outline is (re)shown — which, during a move, is every frame — so Settings
changes take effect immediately.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Dict, Optional, Tuple

import config

log = logging.getLogger(__name__)

Rect = Tuple[int, int, int, int]

# ---------------------------------------------------------------------------
# Win32 plumbing
# ---------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t

WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

LWA_ALPHA = 0x00000002
SW_HIDE = 0
RGN_DIFF = 4

WM_DESTROY = 0x0002
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_TIMER = 0x0113
WM_APP = 0x8000

_WM_SYNC = WM_APP + 1       # posted to the control window: reconcile outlines
_WM_SHUTDOWN = WM_APP + 2   # posted to the control window: quit the loop

_FADE_TOTAL_MS = 150
_FADE_STEP_MS = 25
_FADE_TIMER_ID = 1

HWND_TOPMOST = wintypes.HWND(-1)
HWND_MESSAGE = wintypes.HWND(-3)

GCLP_HBRBACKGROUND = -10  # unused (per-window brushes) — kept for reference


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wintypes.HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


def _proto() -> None:
    """Pin restypes/argtypes so 64-bit handles are not truncated to int."""
    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetLayeredWindowAttributes.argtypes = [
        wintypes.HWND, wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.InvalidateRect.argtypes = [
        wintypes.HWND, ctypes.c_void_p, wintypes.BOOL]
    user32.BeginPaint.restype = wintypes.HDC
    user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
    user32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
    user32.FillRect.argtypes = [
        wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    user32.PostMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.SetTimer.restype = ctypes.c_void_p
    user32.SetTimer.argtypes = [
        wintypes.HWND, ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p]
    user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_void_p]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
    gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
    gdi32.CreateRectRgn.restype = wintypes.HRGN
    gdi32.CreateRectRgn.argtypes = [ctypes.c_int] * 4
    gdi32.CombineRgn.argtypes = [
        wintypes.HRGN, wintypes.HRGN, wintypes.HRGN, ctypes.c_int]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]


_proto()


def _colorref(hex_color: str) -> int:
    """'#rrggbb' → Win32 COLORREF (0x00bbggrr).  Falls back to lavender."""
    try:
        s = str(hex_color).strip().lstrip("#")
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        return r | (g << 8) | (b << 16)
    except Exception:
        return 0xFA | (0x8B << 8) | (0xA7 << 16)   # #a78bfa


def _alpha_byte() -> int:
    try:
        a = int(round(float(config.FOCUS_OUTLINE_OPACITY) * 255))
    except Exception:
        a = 140
    return max(13, min(255, a))


def _width_px() -> int:
    try:
        w = int(config.FOCUS_OUTLINE_WIDTH)
    except Exception:
        w = 3
    return max(1, min(12, w))


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class _Controller:
    """Owns the outline thread, the control window and the outline-window pool."""

    CLASS_NAME = "WFMFocusOutlineWnd"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._desired: Dict[int, Rect] = {}
        self._ready = threading.Event()
        self._ctrl_hwnd: int = 0
        self._thread: Optional[threading.Thread] = None

        # Outline-thread-only state -----------------------------------------
        self._hinst: int = 0
        self._registered = False
        self._wndproc = WNDPROC(self._wnd_proc)   # keep a strong reference
        # target hwnd -> entry dict {hwnd, rect, width, color, brush, alpha, fade}
        self._pool: Dict[int, dict] = {}
        self._by_hwnd: Dict[int, dict] = {}

    # ---- public API (any thread) -------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="focus-outline", daemon=True)
        self._thread.start()

    def set_desired(self, mapping: Dict[int, Rect]) -> None:
        """Store the wanted {hwnd: Rect} and wake the outline thread if it changed."""
        with self._lock:
            if mapping == self._desired:
                return
            self._desired = dict(mapping)
        hwnd = self._ctrl_hwnd
        if hwnd:
            user32.PostMessageW(wintypes.HWND(hwnd), _WM_SYNC, 0, 0)

    def shutdown(self) -> None:
        hwnd = self._ctrl_hwnd
        if hwnd:
            user32.PostMessageW(wintypes.HWND(hwnd), _WM_SHUTDOWN, 0, 0)

    # ---- outline thread --------------------------------------------------

    def _run(self) -> None:
        try:
            self._hinst = kernel32.GetModuleHandleW(None) or 0
            self._register_class()
            self._ctrl_hwnd = self._create_window(message_only=True)
            if not self._ctrl_hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
        except Exception:
            log.exception("focus outline: thread init failed — feature disabled")
            return

        self._ready.set()
        self._sync()   # apply anything queued before the window existed

        msg = MSG()
        try:
            while True:
                got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got in (0, -1):
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            self._cleanup()

    def _register_class(self) -> None:
        wc = WNDCLASS()
        wc.style = 0
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = self._hinst
        wc.hbrBackground = wintypes.HBRUSH(0)
        wc.lpszClassName = self.CLASS_NAME
        if not user32.RegisterClassW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err not in (0, 1410):   # ERROR_CLASS_ALREADY_EXISTS
                raise ctypes.WinError(err)
        self._registered = True

    def _create_window(self, message_only: bool = False) -> int:
        if message_only:
            hwnd = user32.CreateWindowExW(
                0, self.CLASS_NAME, "wfm-focus-outline-ctrl", 0,
                0, 0, 0, 0, HWND_MESSAGE, None, self._hinst, None)
        else:
            ex = (WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
                  | WS_EX_TOOLWINDOW | WS_EX_TOPMOST)
            hwnd = user32.CreateWindowExW(
                ex, self.CLASS_NAME, "wfm-focus-outline", WS_POPUP,
                0, 0, 10, 10, None, None, self._hinst, None)
        return int(hwnd) if hwnd else 0

    # ---- reconcile -----------------------------------------------------

    def _sync(self) -> None:
        with self._lock:
            desired = dict(self._desired)

        for target in list(self._pool):
            if target not in desired:
                self._begin_fade(target)

        for target, rect in desired.items():
            try:
                self._show(target, tuple(int(v) for v in rect))
            except Exception:
                log.debug("focus outline: show failed for hwnd=%s", target,
                          exc_info=True)

    def _show(self, target: int, rect: Rect) -> None:
        color = _colorref(config.FOCUS_OUTLINE_COLOR)
        width = _width_px()
        alpha = _alpha_byte()

        ent = self._pool.get(target)
        if ent is None:
            hwnd = self._create_window()
            if not hwnd:
                return
            ent = {"hwnd": hwnd, "rect": None, "width": None,
                   "color": None, "brush": 0, "alpha": 0, "fade": None}
            self._pool[target] = ent
            self._by_hwnd[hwnd] = ent
        hwnd = ent["hwnd"]

        if ent["fade"] is not None:            # cancel an in-progress fade-out
            user32.KillTimer(wintypes.HWND(hwnd), _FADE_TIMER_ID)
            ent["fade"] = None

        if ent["color"] != color:
            brush = gdi32.CreateSolidBrush(color)
            old = ent["brush"]
            ent["brush"] = brush
            ent["color"] = color
            if old:
                gdi32.DeleteObject(wintypes.HGDIOBJ(old))
            user32.InvalidateRect(wintypes.HWND(hwnd), None, True)

        l, t, r, b = rect
        w = max(1, r - l)
        h = max(1, b - t)
        if ent["rect"] != rect or ent["width"] != width:
            user32.SetWindowPos(wintypes.HWND(hwnd), HWND_TOPMOST, l, t, w, h,
                                SWP_NOACTIVATE | SWP_SHOWWINDOW)
            self._apply_frame_region(hwnd, w, h, width)
            user32.InvalidateRect(wintypes.HWND(hwnd), None, True)
            ent["rect"] = rect
            ent["width"] = width
        else:
            user32.SetWindowPos(
                wintypes.HWND(hwnd), HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)

        if ent["alpha"] != alpha:
            user32.SetLayeredWindowAttributes(
                wintypes.HWND(hwnd), 0, alpha & 0xFF, LWA_ALPHA)
            ent["alpha"] = alpha

    def _apply_frame_region(self, hwnd: int, w: int, h: int, width: int) -> None:
        """Hollow the window: outer rect minus an inset inner rect."""
        inset = max(1, min(width, max(1, min(w, h) // 2)))
        outer = gdi32.CreateRectRgn(0, 0, w, h)
        inner = gdi32.CreateRectRgn(inset, inset, w - inset, h - inset)
        if outer and inner:
            gdi32.CombineRgn(outer, outer, inner, RGN_DIFF)
        if inner:
            gdi32.DeleteObject(wintypes.HGDIOBJ(inner))
        # SetWindowRgn takes ownership of `outer` — do not delete it here.
        user32.SetWindowRgn(wintypes.HWND(hwnd), outer, True)

    # ---- fade-out -----------------------------------------------------

    def _begin_fade(self, target: int) -> None:
        ent = self._pool.get(target)
        if ent is None:
            return
        if ent["fade"] is not None:
            return
        ent["fade"] = ent.get("alpha", 0) or _alpha_byte()
        user32.SetTimer(wintypes.HWND(ent["hwnd"]), _FADE_TIMER_ID,
                        _FADE_STEP_MS, None)

    def _on_timer(self, hwnd: int) -> None:
        ent = self._by_hwnd.get(hwnd)
        if ent is None or ent["fade"] is None:
            user32.KillTimer(wintypes.HWND(hwnd), _FADE_TIMER_ID)
            return
        start = ent.get("alpha", 0) or 1
        step = max(1, int(start * _FADE_STEP_MS / max(1, _FADE_TOTAL_MS)))
        ent["fade"] -= step
        if ent["fade"] <= 0:
            user32.KillTimer(wintypes.HWND(hwnd), _FADE_TIMER_ID)
            self._destroy(ent)
        else:
            user32.SetLayeredWindowAttributes(
                wintypes.HWND(hwnd), 0, ent["fade"] & 0xFF, LWA_ALPHA)

    def _destroy(self, ent: dict) -> None:
        hwnd = ent["hwnd"]
        self._by_hwnd.pop(hwnd, None)
        for k, v in list(self._pool.items()):
            if v is ent:
                del self._pool[k]
        if ent["brush"]:
            gdi32.DeleteObject(wintypes.HGDIOBJ(ent["brush"]))
            ent["brush"] = 0
        user32.DestroyWindow(wintypes.HWND(hwnd))

    # ---- window procedure --------------------------------------------

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        hwnd_i = int(hwnd) if hwnd else 0
        try:
            if hwnd_i == self._ctrl_hwnd:
                if msg == _WM_SYNC:
                    self._sync()
                    return 0
                if msg == _WM_SHUTDOWN:
                    user32.PostQuitMessage(0)
                    return 0
            else:
                if msg == WM_ERASEBKGND:
                    return 1   # painted in WM_PAINT
                if msg == WM_PAINT:
                    self._on_paint(hwnd_i)
                    return 0
                if msg == WM_TIMER:
                    self._on_timer(hwnd_i)
                    return 0
                if msg == WM_DESTROY:
                    return 0
        except Exception:
            log.debug("focus outline: wndproc error (msg=%#x)", msg,
                      exc_info=True)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _on_paint(self, hwnd: int) -> None:
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(wintypes.HWND(hwnd), ctypes.byref(ps))
        try:
            ent = self._by_hwnd.get(hwnd)
            brush = ent["brush"] if ent else 0
            if brush:
                rc = wintypes.RECT()
                user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rc))
                user32.FillRect(hdc, ctypes.byref(rc), wintypes.HBRUSH(brush))
        finally:
            user32.EndPaint(wintypes.HWND(hwnd), ctypes.byref(ps))

    # ---- teardown ---------------------------------------------------

    def _cleanup(self) -> None:
        for ent in list(self._by_hwnd.values()):
            try:
                if ent["brush"]:
                    gdi32.DeleteObject(wintypes.HGDIOBJ(ent["brush"]))
                user32.DestroyWindow(wintypes.HWND(ent["hwnd"]))
            except Exception:
                pass
        self._pool.clear()
        self._by_hwnd.clear()
        if self._ctrl_hwnd:
            try:
                user32.DestroyWindow(wintypes.HWND(self._ctrl_hwnd))
            except Exception:
                pass
            self._ctrl_hwnd = 0
        if self._registered:
            try:
                user32.UnregisterClassW(self.CLASS_NAME, self._hinst)
            except Exception:
                pass
            self._registered = False


# ---------------------------------------------------------------------------
# Module entry point
# ---------------------------------------------------------------------------

_controller: Optional[_Controller] = None


def _observer(payload) -> None:
    """Focus-observer callback — runs on the tiler thread; must be fast.

    ``payload`` is ``{hwnd: Rect}`` for focused windows that moved this frame
    (``{}`` when nothing is animating).  Self-gates on
    ``config.FOCUS_OUTLINE_ENABLED``: forces every outline hidden when off.
    """
    c = _controller
    if c is None:
        return
    try:
        if not config.FOCUS_OUTLINE_ENABLED:
            c.set_desired({})
            return
        c.set_desired({int(h): tuple(int(v) for v in r)
                       for h, r in dict(payload).items()})
    except Exception:
        pass


def attach(manager) -> None:
    """Start the outline controller and register its focus observer on `manager`.

    Safe to call more than once; the controller thread is created only once.
    Any failure is swallowed — the tiler runs unaffected.
    """
    global _controller
    if _controller is None:
        c = _Controller()
        try:
            c.start()
        except Exception:
            log.exception("focus outline: controller thread failed to start")
            return
        _controller = c
        log.debug("focus outline: controller started")
    try:
        manager.set_focus_observer(_observer)
    except Exception:
        log.exception("focus outline: could not register focus observer")
