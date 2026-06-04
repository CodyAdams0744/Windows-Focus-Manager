"""config_io.py — config + system I/O for the native settings window.

This module holds the logic that used to live in the HTTP config server:
reading/writing config.json, enumerating monitors, and the Windows
autostart registry entry.  The settings UI (settings_window.py) calls these
functions directly — no localhost server, no browser.

The running tiler picks up saved changes automatically via the live config
watcher in main.py, so saving here applies settings with no restart.
"""

import json
import sys
import winreg
from pathlib import Path

import win32api

from config import DEFAULTS

# ---------------------------------------------------------------------------
# Paths — config.json lives next to the .exe (survives updates); when running
# from source it sits next to the .py files.
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    _APP_DIR = Path(sys.executable).parent
else:
    _APP_DIR = Path(__file__).parent

CONFIG_FILE = _APP_DIR / "config.json"


# ---------------------------------------------------------------------------
# config.json read / write
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Return config.json merged over DEFAULTS (defaults fill any missing keys)."""
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            merged = dict(DEFAULTS)
            merged.update(data)
            return merged
        except Exception:
            pass
    return dict(DEFAULTS)


def save_config(data: dict) -> None:
    """Write config.json.  skip_classes is preserved from the existing file
    if the caller didn't supply it (the UI never edits the built-in class list)."""
    existing = load_config()
    if "skip_classes" not in data:
        data["skip_classes"] = existing.get("skip_classes", DEFAULTS["skip_classes"])
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Autostart (HKCU Run key)
# ---------------------------------------------------------------------------

_AUTOSTART_REG_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_REG_NAME = "WindowFocusManager"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def get_autostart() -> bool:
    """Return True if the autostart registry value is set."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_REG_KEY) as k:
            winreg.QueryValueEx(k, _AUTOSTART_REG_NAME)
            return True
    except (FileNotFoundError, OSError):
        return False


def set_autostart(enabled: bool) -> None:
    """Write or delete the HKCU Run registry value.

    Autostart only makes sense for the bundled .exe — pointing the Run key at
    a python.exe + script path is fragile, so it's blocked when running from
    source (the UI greys the toggle out in that case).
    """
    if enabled:
        if not is_frozen():
            raise ValueError("Autostart can only be enabled from the bundled .exe.")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_REG_KEY,
                            access=winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, _AUTOSTART_REG_NAME, 0,
                              winreg.REG_SZ, str(sys.executable))
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_REG_KEY,
                                access=winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, _AUTOSTART_REG_NAME)
        except FileNotFoundError:
            pass   # already not set


# ---------------------------------------------------------------------------
# Monitor enumeration
# ---------------------------------------------------------------------------

def get_monitors() -> list:
    """Return a list of monitor info dicts for the settings UI."""
    monitors = []
    try:
        for i, (hmon, _hdc, _rect) in enumerate(win32api.EnumDisplayMonitors()):
            info  = win32api.GetMonitorInfo(hmon)
            mr    = info["Monitor"]   # (left, top, right, bottom)
            wr    = info["Work"]
            flags = info.get("Flags", 0)
            mw    = mr[2] - mr[0]
            mh    = mr[3] - mr[1]
            monitors.append({
                "index":       i,
                "handle":      int(hmon),
                "left":        mr[0],
                "top":         mr[1],
                "width":       mw,
                "height":      mh,
                "primary":     bool(flags & 1),          # MONITORINFOF_PRIMARY
                "orientation": "portrait" if mh > mw else "landscape",
                "work_left":   wr[0],
                "work_top":    wr[1],
                "work_width":  wr[2] - wr[0],
                "work_height": wr[3] - wr[1],
            })
    except Exception as exc:
        print(f"[config_io] Monitor enumeration error: {exc}")
    return monitors
