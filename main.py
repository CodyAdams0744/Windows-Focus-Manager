# main.py — entry point for Windows Focus Manager

import ctypes
import logging
import logging.handlers
import os
import sys
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Frozen-aware paths
#   _BASE_DIR     — next to the .exe / .py  (user-editable files: config.json)
#   _RESOURCE_DIR — bundled assets extracted by PyInstaller
# ---------------------------------------------------------------------------
if getattr(sys, 'frozen', False):
    _BASE_DIR     = Path(sys.executable).parent
    _RESOURCE_DIR = Path(getattr(sys, '_MEIPASS', _BASE_DIR))
else:
    _BASE_DIR     = Path(__file__).parent
    _RESOURCE_DIR = Path(__file__).parent

_CONFIG_JSON = _BASE_DIR / "config.json"

# Handle for the single-instance mutex.  Kept in a module global so it lives
# for the whole process — releasing it (or letting it be GC'd) would drop the
# guard and let a second instance start.
_SINGLE_INSTANCE_MUTEX = None

# Named mutex identifying a running instance, and the Win32 error returned by
# CreateMutexW when one already exists.
_MUTEX_NAME = "WindowFocusManager_SingleInstance"
_ERROR_ALREADY_EXISTS = 183


def _acquire_single_instance() -> bool:
    """Create the named single-instance mutex.

    Returns True if this is the only instance (mutex acquired), False if
    another instance already holds it.  Fails open: if the mutex cannot be
    created at all, returns True so the app still starts.
    """
    global _SINGLE_INSTANCE_MUTEX
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                          ctypes.c_wchar_p]
        handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        if not handle:
            return True
        if kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
            return False
        _SINGLE_INSTANCE_MUTEX = handle
        return True
    except Exception:
        return True


def _install_excepthooks(log: logging.Logger) -> None:
    """Route otherwise-unhandled exceptions (main thread and worker threads)
    to the root logger with a full traceback.  KeyboardInterrupt is passed
    straight through to the original hooks."""
    root = logging.getLogger()

    _orig_sys_hook = sys.excepthook

    def _sys_excepthook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            _orig_sys_hook(exc_type, exc_value, exc_tb)
            return
        root.critical("Unhandled exception in main thread",
                      exc_info=(exc_type, exc_value, exc_tb))

    sys.excepthook = _sys_excepthook

    _orig_thread_hook = threading.excepthook

    def _thread_excepthook(args) -> None:
        if issubclass(args.exc_type, KeyboardInterrupt):
            _orig_thread_hook(args)
            return
        name = args.thread.name if args.thread is not None else "<unknown>"
        root.critical("Unhandled exception in thread %r", name,
                      exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = _thread_excepthook


def _watchdog(log: logging.Logger, manager, tiler: threading.Thread) -> None:
    """Restart the tiler thread if it ever dies.  Gives up after 5 restarts."""
    max_restarts = 5
    restarts = 0
    current = tiler
    while True:
        time.sleep(2.0)
        if current.is_alive():
            continue
        if restarts >= max_restarts:
            log.error("Tiler thread has died 5 times — giving up; "
                      "restart the app.")
            return
        restarts += 1
        log.error("Tiler thread is not alive — restarting (%d/%d).",
                  restarts, max_restarts)
        current = threading.Thread(target=manager.run, daemon=True,
                                   name="tiler")
        current.start()


# ---------------------------------------------------------------------------
# Config file watcher — live-reloads settings without restarting
# ---------------------------------------------------------------------------

def _watch_config(log: logging.Logger) -> None:
    """Background thread: live-reload config.json using Win32 directory notifications.

    Uses FindFirstChangeNotification so the thread sleeps until the OS signals
    a write in the config directory — no polling, instant response.
    Falls back to 1-second stat polling if the notification handle cannot be created.
    """
    import config as _cfg_mod

    _event_driven = False
    handle = None
    try:
        import win32file
        import win32event
        handle = win32file.FindFirstChangeNotification(
            str(_CONFIG_JSON.parent),
            False,          # do not watch subdirectories
            # LAST_WRITE | FILE_NAME — FILE_NAME is needed because the settings
            # UI saves atomically via os.replace (a rename, not a write).
            0x00000010 | 0x00000001,
        )
        _event_driven = True
    except Exception as exc:
        log.warning("Config watcher: directory notification unavailable (%s) — using polling.", exc)

    try:
        mtime = _CONFIG_JSON.stat().st_mtime if _CONFIG_JSON.exists() else 0.0
    except Exception:
        mtime = 0.0

    while True:
        if _event_driven:
            try:
                result = win32event.WaitForSingleObject(handle, 2000)  # 2 s safety timeout
                if result == 0x00000000:  # WAIT_OBJECT_0 — something in directory changed
                    win32file.FindNextChangeNotification(handle)
                # WAIT_TIMEOUT just loops and waits again
            except Exception as exc:
                log.warning("Config watcher notification error: %s — falling back to polling.", exc)
                _event_driven = False
        else:
            time.sleep(1)

        try:
            new_mtime = _CONFIG_JSON.stat().st_mtime if _CONFIG_JSON.exists() else 0.0
        except Exception:
            continue
        if new_mtime == mtime:
            continue

        mtime = new_mtime
        time.sleep(0.15)   # let the write fully flush before reading
        try:
            # In-place attribute update — avoids importlib.reload re-executing
            # the module while the tiler thread is reading it.
            _cfg_mod.reload()
            logging.getLogger().setLevel(
                getattr(logging, _cfg_mod.LOG_LEVEL, logging.INFO))
            log.info("Config reloaded — new settings applied (no restart needed).")
            log.info("  Expand ratio : %.0f%%", _cfg_mod.EXPAND_RATIO * 100)
            log.info("  Hover        : %s  (delay %d ms)",
                     _cfg_mod.HOVER_ENABLED, _cfg_mod.HOVER_DELAY_MS)
            log.info("  Animation    : %s  (%d ms)",
                     _cfg_mod.ANIMATE, _cfg_mod.ANIMATION_DURATION_MS)
        except Exception as exc:
            log.error("Failed to reload config: %s", exc)


# ---------------------------------------------------------------------------
# Settings window launcher
# ---------------------------------------------------------------------------

def _launch_settings_window(log: logging.Logger) -> None:
    """Open the native settings window as a separate process.

    Running the Qt window in its own process keeps its event loop fully
    isolated from the tiler loop and the tray thread — no cross-thread Qt
    issues, and the window can be closed/reopened freely.  The child writes
    config.json, which the live watcher in this process reloads automatically.
    """
    import subprocess
    try:
        if getattr(sys, 'frozen', False):
            # Re-launch our own exe with the --settings switch.
            args = [sys.executable, "--settings"]
        else:
            args = [sys.executable, str(_BASE_DIR / "settings_window.py")]
        subprocess.Popen(args, close_fds=True)
    except Exception as exc:
        log.error("Could not open settings window: %s", exc)


# ---------------------------------------------------------------------------
# Startup helpers
# ---------------------------------------------------------------------------

def _is_first_run() -> bool:
    """True until the user finishes the welcome screen (welcome_seen saved)."""
    try:
        import json
        return not json.loads(
            _CONFIG_JSON.read_text(encoding="utf-8")).get("welcome_seen", False)
    except Exception:
        return True


def _set_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _check_deps() -> None:
    try:
        import win32gui  # noqa: F401
        import win32api  # noqa: F401
        import win32con  # noqa: F401
    except ImportError:
        print("Missing dependency: pywin32\nInstall with:  pip install pywin32")
        sys.exit(1)


def _setup_logging(level_name: str) -> None:
    """Configure logging.

    Always writes a rotating ``wfm.log`` next to the exe / script, so a
    windowless launch (frozen exe, or ``pythonw main.py`` from an autostart
    entry) still leaves a trail.  When not frozen and a console is attached,
    also echo to stderr for live dev feedback.
    """
    level = getattr(logging, level_name, logging.INFO)
    fmt   = "%(asctime)s  %(levelname)-8s  %(message)s"
    dt    = "%H:%M:%S"

    handlers: list = []
    try:
        handlers.append(logging.handlers.RotatingFileHandler(
            _BASE_DIR / "wfm.log", maxBytes=2 * 1024 * 1024,
            backupCount=3, encoding="utf-8"))
    except Exception:
        pass
    if not getattr(sys, 'frozen', False) and sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    if not handlers:
        handlers.append(logging.StreamHandler())

    # force=True replaces any handlers a 3rd-party import may have added,
    # preventing duplicate log lines.
    logging.basicConfig(level=level, format=fmt, datefmt=dt,
                        handlers=handlers, force=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    _set_dpi_awareness()
    _check_deps()

    import config
    from window_manager import WindowManager, start_event_hooks

    _setup_logging(config.LOG_LEVEL)

    log = logging.getLogger(__name__)

    # Refuse to start a second copy — two tilers fighting over the same
    # windows produces relentless flicker.  (--settings is never gated.)
    if not _acquire_single_instance():
        log.warning("Another instance is already running — exiting.")
        sys.exit(0)

    _install_excepthooks(log)
    log.info("=" * 56)
    log.info("Windows Focus Manager")
    if getattr(sys, 'frozen', False):
        log.info("  Running as  : bundled exe")
        log.info("  App dir     : %s", _BASE_DIR)
        log.info("  Log file    : %s", _BASE_DIR / 'wfm.log')
    if config.HOVER_ENABLED:
        log.info("  Trigger      : hover  (delay %d ms)", config.HOVER_DELAY_MS)
    else:
        log.info("  Trigger      : foreground window change (click / alt-tab)")
    log.info("  Expand ratio : %.0f%%", config.EXPAND_RATIO * 100)
    log.info("  Anim duration: %d ms  (ANIMATE=%s)",
             config.ANIMATION_DURATION_MS, config.ANIMATE)
    log.info("=" * 56)

    manager = WindowManager()

    # Optional focus-outline overlay (opt-in).  The controller runs on its own
    # thread and honours config.FOCUS_OUTLINE_ENABLED internally — it draws
    # nothing until the user turns it on in Settings.  Independent of the tiler
    # thread's lifecycle (it attaches via the manager's focus observer).
    try:
        import focus_outline
        focus_outline.attach(manager)
    except Exception as exc:
        log.debug("Focus outline overlay unavailable: %s", exc)

    watcher = threading.Thread(target=_watch_config, args=(log,),
                               daemon=True, name="config-watcher")
    watcher.start()

    # Event-driven foreground detection + display-topology handling.  Runs on
    # its own daemon thread (a Win32 message loop); a failure here is not
    # fatal — the tiler still polls the foreground window every frame.
    try:
        start_event_hooks(manager)
    except Exception as exc:
        log.warning("Foreground event hook unavailable (%s) — "
                    "using poll cadence only.", exc)

    # The tiler runs on a background thread so the Qt tray can own the main
    # thread (QSystemTrayIcon + the bento flyout need the Qt event loop).
    tiler = threading.Thread(target=manager.run, daemon=True, name="tiler")
    tiler.start()

    # Watchdog: revive the tiler thread if an unexpected crash slips past its
    # own per-frame guard.  Daemon so it never holds up exit.
    watchdog = threading.Thread(target=_watchdog, args=(log, manager, tiler),
                                daemon=True, name="tiler-watchdog")
    watchdog.start()

    # Open the settings window on first run only — on subsequent launches
    # (including autostart at login) the app starts quietly in the tray.
    if _is_first_run():
        _launch_settings_window(log)

    import tray
    tray.run_tray(manager, log)


def _run_settings() -> None:
    """Entry point for the `--settings` switch: show the native window only."""
    _set_dpi_awareness()
    import settings_window
    sys.exit(settings_window.run())


if __name__ == "__main__":
    if "--settings" in sys.argv:
        _run_settings()
    else:
        main()
