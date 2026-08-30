"""config.py — loads settings from config.json (written by the settings UI).

To open the settings UI:
    python settings_window.py
"""
import json
import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

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
    # Layouts — see docs/layout-design.md
    "layout_mode":  "auto",   # "auto" (pick by window count) | "strip" | "quadrant" | "triptych" | "fgrid"
    "layout_tiers": {         # preset per window-count tier (used when layout_mode == "auto")
        "1_2":    "strip",
        "3_4":    "quadrant",
        "5_6":    "triptych",
        "7_plus": "fgrid",
    },
    "layout_large_first": {   # uneven counts: put the larger cell first (left/top)?
        "quadrant": True,     # 3 windows: full-height window on the left
        "triptych": True,     # 5 windows: full-height window leads on the left
        "fgrid":    False,    # 7 windows: roomier row on the bottom
    },
    "layout_focus_bias_max": 3.2,   # cap on how dominant the focused band/cell gets
    "layout_gap_px":         0,     # visible gap between tiles (band layouts only)
    "layout_debounce_ms":    800,   # count must be stable this long before preset switches
    # Per-monitor overrides: [{left, top, layout_mode?, expand_ratio?}]
    # Monitors identified by monitor-rect left/top (same scheme as enabled_monitors).
    # Missing keys / missing entry → the global setting applies.
    "monitor_overrides":     [],
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
        except Exception as exc:
            log.warning("config.json could not be parsed (%s) — using defaults.", exc)
    return dict(DEFAULTS)


def _apply(cfg: dict) -> None:
    """Bind cfg values to the module attributes the tiler reads.

    Called once at import and again by reload().  Updating attributes in
    place (instead of importlib.reload) avoids re-executing the module while
    another thread is reading it.
    """
    global POLL_INTERVAL_MS, ANIM_FRAME_MS, ANIMATION_DURATION_MS, ANIMATE
    global EXPAND_RATIO, MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT
    global SKIP_CLASSES, SKIP_TITLES, SKIP_EXE, LOG_LEVEL
    global HOVER_ENABLED, HOVER_DELAY_MS, RETURN_TO_CENTER, ENABLED_MONITORS
    global LAYOUT_MODE, LAYOUT_TIERS, LAYOUT_LARGE_FIRST
    global LAYOUT_BIAS_MAX, LAYOUT_GAP_PX, LAYOUT_DEBOUNCE_MS
    global MONITOR_OVERRIDES

    POLL_INTERVAL_MS      = int(cfg["poll_interval_ms"])
    ANIM_FRAME_MS         = int(cfg["anim_frame_ms"])
    ANIMATION_DURATION_MS = int(cfg["animation_duration_ms"])
    ANIMATE               = bool(cfg["animate"])
    EXPAND_RATIO          = float(cfg["expand_ratio"])
    MIN_WINDOW_WIDTH      = int(cfg["min_window_width"])
    MIN_WINDOW_HEIGHT     = int(cfg["min_window_height"])
    SKIP_CLASSES          = frozenset(cfg["skip_classes"])
    SKIP_TITLES           = tuple(cfg.get("skip_titles", []))
    SKIP_EXE              = tuple(s.lower() for s in cfg.get("skip_exe", []))
    LOG_LEVEL             = str(cfg["log_level"])
    HOVER_ENABLED         = bool(cfg.get("hover_enabled", False))
    HOVER_DELAY_MS        = int(cfg.get("hover_delay_ms", 300))
    RETURN_TO_CENTER      = bool(cfg.get("return_to_center", False))
    # None = all monitors; list of {"left": x, "top": y} = only those monitors
    ENABLED_MONITORS      = cfg.get("enabled_monitors") or None
    # Tiling layouts (see docs/layout-design.md).  "grid"/"master" are
    # retired modes from v1.2 — migrate them to their nearest successor.
    presets = ("strip", "quadrant", "triptych", "fgrid")
    migrate = {"grid": "fgrid", "master": "strip", "columns": "strip"}

    mode = str(cfg.get("layout_mode", "auto")).lower()
    mode = migrate.get(mode, mode)
    LAYOUT_MODE = mode if mode in presets + ("auto",) else "auto"

    tiers_in = cfg.get("layout_tiers") or {}
    LAYOUT_TIERS = {}
    for key, dflt in (("1_2", "strip"), ("3_4", "quadrant"),
                      ("5_6", "triptych"), ("7_plus", "fgrid")):
        v = str(tiers_in.get(key, dflt)).lower()
        v = migrate.get(v, v)
        LAYOUT_TIERS[key] = v if v in presets else dflt

    lf = cfg.get("layout_large_first") or {}
    LAYOUT_LARGE_FIRST = {
        "quadrant": bool(lf.get("quadrant", True)),
        "triptych": bool(lf.get("triptych", True)),
        "fgrid":    bool(lf.get("fgrid", False)),
    }

    LAYOUT_BIAS_MAX    = max(1.3, min(6.0, float(cfg.get("layout_focus_bias_max", 3.2))))
    LAYOUT_GAP_PX      = max(0, min(48, int(cfg.get("layout_gap_px", 0))))
    LAYOUT_DEBOUNCE_MS = max(0, min(5000, int(cfg.get("layout_debounce_ms", 800))))

    overrides = []
    for o in (cfg.get("monitor_overrides") or []):
        try:
            entry = {"left": int(o["left"]), "top": int(o["top"])}
            ov_mode = migrate.get(str(o.get("layout_mode", "")).lower(),
                                  str(o.get("layout_mode", "")).lower())
            if ov_mode in presets + ("auto",):
                entry["layout_mode"] = ov_mode
            if "expand_ratio" in o:
                entry["expand_ratio"] = max(0.35, min(0.85, float(o["expand_ratio"])))
            if len(entry) > 2:        # keep only entries that override something
                overrides.append(entry)
        except Exception:
            continue
    MONITOR_OVERRIDES = overrides


def reload() -> None:
    """Re-read config.json and update module attributes in place."""
    _apply(_load())


_apply(_load())   # bind settings at import
