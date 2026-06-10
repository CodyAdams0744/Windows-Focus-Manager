"""config.py — loads settings from config.json (written by the settings UI).

To open the settings UI:
    python settings_window.py
"""
import json
import sys
from pathlib import Path

DEFAULTS: dict = {
    "poll_interval_ms":      100,
    "anim_frame_ms":         1,
    "animation_duration_ms": 125,
    "animate":               True,
    "expand_ratio":          0.65,
    "min_window_width":      200,
    "min_window_height":     200,
    "hover_enabled":         False,  # True = expand on hover; False = expand on click/focus
    "hover_delay_ms":        300,    # ms the cursor must rest before hover triggers
    "return_to_center":      False,  # hover mode: monitors with no focus tile evenly
    "skip_classes": [
        "Shell_TrayWnd", "Progman", "WorkerW", "DV2ControlHost",
        "MsgrIMEWindowClass", "SysShadow", "tooltips_class32",
        "IME", "MSCTFIME UI", "ConsoleWindowClass",
        "CASCADIA_HOSTING_WINDOW_CLASS",
    ],
    "skip_titles": [],   # substring match on window title (case-insensitive)
    "skip_exe":    [],   # exact match on executable filename e.g. "spotify.exe"
    "log_level":        "INFO",
    "enabled_monitors": None,   # null / None = all monitors; list of {left,top} = specific
}

# Frozen-aware path: config.json lives next to the .exe, not in the temp
# extraction directory that PyInstaller uses for bundled module files.
if getattr(sys, 'frozen', False):
    _cfg_path = Path(sys.executable).parent / "config.json"
else:
    _cfg_path = Path(__file__).parent / "config.json"


def _load() -> dict:
    if _cfg_path.exists():
        try:
            data = json.loads(_cfg_path.read_text(encoding="utf-8"))
            result = dict(DEFAULTS)
            result.update(data)
            return result
        except Exception:
            pass
    return dict(DEFAULTS)


_cfg = _load()

POLL_INTERVAL_MS      : int       = int(_cfg["poll_interval_ms"])
ANIM_FRAME_MS         : int       = int(_cfg["anim_frame_ms"])
ANIMATION_DURATION_MS : int       = int(_cfg["animation_duration_ms"])
ANIMATE               : bool      = bool(_cfg["animate"])
EXPAND_RATIO          : float     = float(_cfg["expand_ratio"])
MIN_WINDOW_WIDTH      : int       = int(_cfg["min_window_width"])
MIN_WINDOW_HEIGHT     : int       = int(_cfg["min_window_height"])
SKIP_CLASSES          : frozenset = frozenset(_cfg["skip_classes"])
SKIP_TITLES           : tuple     = tuple(_cfg.get("skip_titles", []))
SKIP_EXE              : tuple     = tuple(s.lower() for s in _cfg.get("skip_exe", []))
LOG_LEVEL             : str       = str(_cfg["log_level"])
HOVER_ENABLED         : bool      = bool(_cfg.get("hover_enabled", False))
HOVER_DELAY_MS        : int       = int(_cfg.get("hover_delay_ms", 300))
RETURN_TO_CENTER      : bool      = bool(_cfg.get("return_to_center", False))
# None = all monitors; list of {"left": x, "top": y} = only those monitors
ENABLED_MONITORS                  = _cfg.get("enabled_monitors") or None
