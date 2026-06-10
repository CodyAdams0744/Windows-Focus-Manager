# main.py — entry point for Windows Focus Manager

import ctypes
import importlib
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
            0x00000010,     # FILE_NOTIFY_CHANGE_LAST_WRITE
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
            importlib.reload(_cfg_mod)
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
    """Configure logging.  Frozen exe → rotating file log.  Dev → stderr."""
    level = getattr(logging, level_name, logging.INFO)
    fmt   = "%(asctime)s  %(levelname)-8s  %(message)s"
    dt    = "%H:%M:%S"

    if getattr(sys, 'frozen', False):
        log_path = _BASE_DIR / "wfm.log"
        handler  = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    else:
        handler = logging.StreamHandler()

    # force=True replaces any handlers a 3rd-party import may have added,
    # preventing duplicate log lines.
    logging.basicConfig(level=level, format=fmt, datefmt=dt,
                        handlers=[handler], force=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    _set_dpi_awareness()
    _check_deps()

    import config
    from window_manager import WindowManager

    _setup_logging(config.LOG_LEVEL)

    log = logging.getLogger(__name__)
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

    watcher = threading.Thread(target=_watch_config, args=(log,),
                               daemon=True, name="config-watcher")
    watcher.start()

    # The tiler runs on a background thread so the Qt tray can own the main
    # thread (QSystemTrayIcon + the bento flyout need the Qt event loop).
    tiler = threading.Thread(target=manager.run, daemon=True, name="tiler")
    tiler.start()

    # Open the settings window on launch.
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
