"""config.py — loads settings from config.json (written by the settings UI).

To open the settings UI:
    python settings_window.py

Live reload
-----------
The tiler reads settings as module attributes (``config.EXPAND_RATIO`` etc.).
Those attributes are served from a single dict, ``_current``, via PEP 562
``__getattr__``.  ``reload()`` rebuilds that dict from disk and rebinds it in
one assignment, so a concurrent reader on the tiler thread always sees a
fully-consistent set of values — never a mix of old and new mid-reload.
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
    # Focus outline: a thin highlight drawn around the focused window while the
    # tiler is resizing it, so it's obvious the move is app-driven.  Opt-in.
    "focus_outline_enabled":   False,
    "focus_outline_color":     "#a78bfa",   # hex "#rrggbb"
    "focus_outline_opacity":   0.55,        # 0.0 – 1.0
    "focus_outline_width":     3,           # px, 1 – 12
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

_PRESETS = ("strip", "quadrant", "triptych", "fgrid")
# "grid"/"master" are retired modes from v1.2 — migrate to their successor.
_MIGRATE = {"grid": "fgrid", "master": "strip", "columns": "strip"}

_HEX_DIGITS = set("0123456789abcdefABCDEF")


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


def _valid_hex_color(value, fallback: str) -> str:
    """Return value if it is a '#rrggbb' string, else fallback."""
    try:
        s = str(value).strip()
        if s.startswith("#") and len(s) == 7 and all(c in _HEX_DIGITS for c in s[1:]):
            return s.lower()
    except Exception:
        pass
    return fallback


def _build(cfg: dict) -> dict:
    """Validate cfg and return the flat {ATTR_NAME: value} dict the tiler reads.

    Pure: no module state touched.  reload() swaps the result into place with a
    single rebinding so readers never observe a half-updated config.
    """
    c: dict = {}

    c["POLL_INTERVAL_MS"]      = int(cfg["poll_interval_ms"])
    c["ANIM_FRAME_MS"]         = int(cfg["anim_frame_ms"])
    c["ANIMATION_DURATION_MS"] = int(cfg["animation_duration_ms"])
    c["ANIMATE"]               = bool(cfg["animate"])
    c["EXPAND_RATIO"]          = float(cfg["expand_ratio"])
    c["MIN_WINDOW_WIDTH"]      = int(cfg["min_window_width"])
    c["MIN_WINDOW_HEIGHT"]     = int(cfg["min_window_height"])
    c["SKIP_CLASSES"]          = frozenset(cfg["skip_classes"])
    c["SKIP_TITLES"]           = tuple(cfg.get("skip_titles", []))
    c["SKIP_EXE"]              = tuple(s.lower() for s in cfg.get("skip_exe", []))
    c["LOG_LEVEL"]             = str(cfg["log_level"])
    c["HOVER_ENABLED"]         = bool(cfg.get("hover_enabled", False))
    c["HOVER_DELAY_MS"]        = int(cfg.get("hover_delay_ms", 300))
    c["RETURN_TO_CENTER"]      = bool(cfg.get("return_to_center", False))
    # None = all monitors; list of {"left": x, "top": y} = only those monitors
    c["ENABLED_MONITORS"]      = cfg.get("enabled_monitors") or None

    # Tiling layouts (see docs/layout-design.md).
    mode = str(cfg.get("layout_mode", "auto")).lower()
    mode = _MIGRATE.get(mode, mode)
    c["LAYOUT_MODE"] = mode if mode in _PRESETS + ("auto",) else "auto"

    tiers_in = cfg.get("layout_tiers") or {}
    tiers: dict = {}
    for key, dflt in (("1_2", "strip"), ("3_4", "quadrant"),
                      ("5_6", "triptych"), ("7_plus", "fgrid")):
        v = str(tiers_in.get(key, dflt)).lower()
        v = _MIGRATE.get(v, v)
        tiers[key] = v if v in _PRESETS else dflt
    c["LAYOUT_TIERS"] = tiers

    lf = cfg.get("layout_large_first") or {}
    c["LAYOUT_LARGE_FIRST"] = {
        "quadrant": bool(lf.get("quadrant", True)),
        "triptych": bool(lf.get("triptych", True)),
        "fgrid":    bool(lf.get("fgrid", False)),
    }

    c["LAYOUT_BIAS_MAX"]    = max(1.3, min(6.0, float(cfg.get("layout_focus_bias_max", 3.2))))
    c["LAYOUT_GAP_PX"]      = max(0, min(48, int(cfg.get("layout_gap_px", 0))))
    c["LAYOUT_DEBOUNCE_MS"] = max(0, min(5000, int(cfg.get("layout_debounce_ms", 800))))

    overrides = []
    for o in (cfg.get("monitor_overrides") or []):
        try:
            entry = {"left": int(o["left"]), "top": int(o["top"])}
            ov_mode = _MIGRATE.get(str(o.get("layout_mode", "")).lower(),
                                   str(o.get("layout_mode", "")).lower())
            if ov_mode in _PRESETS + ("auto",):
                entry["layout_mode"] = ov_mode
            if "expand_ratio" in o:
                entry["expand_ratio"] = max(0.35, min(0.85, float(o["expand_ratio"])))
            if len(entry) > 2:        # keep only entries that override something
                overrides.append(entry)
        except Exception:
            continue
    c["MONITOR_OVERRIDES"] = overrides

    # Focus outline (opt-in UX feature).
    c["FOCUS_OUTLINE_ENABLED"]   = bool(cfg.get("focus_outline_enabled", False))
    c["FOCUS_OUTLINE_COLOR"]     = _valid_hex_color(
        cfg.get("focus_outline_color", "#a78bfa"), "#a78bfa")
    try:
        _op = float(cfg.get("focus_outline_opacity", 0.55))
    except Exception:
        _op = 0.55
    c["FOCUS_OUTLINE_OPACITY"]   = max(0.05, min(1.0, _op))
    try:
        _ow = int(cfg.get("focus_outline_width", 3))
    except Exception:
        _ow = 3
    c["FOCUS_OUTLINE_WIDTH"]     = max(1, min(12, _ow))

    return c


_current: dict = _build(_load())   # resolve settings at import


def __getattr__(name: str):
    """PEP 562 module attribute access — serve settings from the live dict."""
    try:
        return _current[name]
    except KeyError:
        raise AttributeError(f"module 'config' has no attribute {name!r}")


def as_dict() -> dict:
    """A copy of the currently-applied settings (debugging / diagnostics)."""
    return dict(_current)


def reload() -> None:
    """Re-read config.json and swap in the new settings atomically."""
    global _current
    _current = _build(_load())
