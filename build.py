"""build.py — four-stage build for Windows Focus Manager.

Stages
------
  1. Generate icon.ico   (dark 4-tile grid, multi-resolution)
  2. PyInstaller         → dist/WindowFocusManager.exe
  3. Inno Setup          → installer_output/WindowFocusManager_Setup.exe
  4. Zip                 → release/WindowFocusManager_Setup.zip (the file you share)

Usage
-----
    python build.py

Requirements
------------
  pip install -r requirements.txt   (pywin32, pystray, Pillow, PySide6)
  pip install pyinstaller
  Inno Setup 6  https://jrsoftware.org/isdl.php  (for the installer stage)
"""

import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent

# ---------------------------------------------------------------------------
# Stage 0 — helpers
# ---------------------------------------------------------------------------

def _kill_running_exe(name: str = "WindowFocusManager.exe") -> None:
    """Terminate any running instance so Windows releases the file lock."""
    result = subprocess.run(
        ["taskkill", "/F", "/IM", name],
        capture_output=True, text=True,
    )
    if "SUCCESS" in result.stdout:
        print(f"  Stopped running {name}.")
        time.sleep(1)   # give Windows a moment to release the lock


# ---------------------------------------------------------------------------
# Stage 1 — icon
# ---------------------------------------------------------------------------

def _generate_icon() -> Path | None:
    """Create icon.ico using the same dark 4-tile grid design as the tray icon.

    Draws at 256×256 and lets Pillow resize down to all standard Windows icon
    sizes (16, 32, 48, 64, 128, 256).  This is the reliable multi-resolution
    ICO approach — Pillow's append_images mode for ICO is inconsistent.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("  [!] Pillow not installed (pip install pillow) — skipping icon.")
        return None

    S = 256   # master canvas size

    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)

    # Dark rounded-square background
    pad    = 8
    radius = 52
    d.rounded_rectangle(
        [pad, pad, S - pad - 1, S - pad - 1],
        radius=radius,
        fill=(28, 28, 28, 255),
    )

    # 4-tile grid with white tiles at varying opacity (top-left brightest)
    inset = 36
    gap   = 18
    tile  = (S - inset * 2 - gap) // 2
    r     = 16  # tile corner radius

    tl = (inset,          inset,          inset + tile,          inset + tile)
    tr = (inset+tile+gap, inset,          inset+tile+gap+tile,   inset + tile)
    bl = (inset,          inset+tile+gap, inset + tile,          inset+tile+gap+tile)
    br = (inset+tile+gap, inset+tile+gap, inset+tile+gap+tile,   inset+tile+gap+tile)

    d.rounded_rectangle(tl, radius=r, fill=(255, 255, 255, 220))
    d.rounded_rectangle(tr, radius=r, fill=(255, 255, 255, 128))
    d.rounded_rectangle(bl, radius=r, fill=(255, 255, 255, 128))
    d.rounded_rectangle(br, radius=r, fill=(255, 255, 255,  60))

    # Pillow resizes from the 256×256 master to produce each size.
    ico_sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    icon_path = HERE / "icon.ico"
    img.save(icon_path, format="ICO", sizes=ico_sizes)

    size_kb = icon_path.stat().st_size // 1024
    print(f"  icon.ico  ({size_kb} KB,  {len(ico_sizes)} sizes)")
    return icon_path


# ---------------------------------------------------------------------------
# Stage 2 — PyInstaller
# ---------------------------------------------------------------------------

def _build_exe(icon_path: Path | None) -> Path:
    """Run PyInstaller and return the path to the produced exe."""
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("  PyInstaller not found — installing...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "pyinstaller"], check=True
        )

    # PySide6 must be importable in the build environment or the settings
    # window won't be bundled.
    try:
        import PySide6  # noqa: F401
    except ImportError:
        print("  PySide6 not found — installing...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "PySide6"], check=True
        )

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",                          # single .exe — easy to distribute
        "--windowed",                         # no console window (logs → wfm.log)
        "--name", "WindowFocusManager",

        # The native settings window lives in its own module; PyInstaller follows
        # the lazy `import settings_window` in main.py, but name it explicitly too.
        "--hidden-import", "settings_window",
        "--hidden-import", "config_io",
        "--hidden-import", "tray",

        # pywin32 hidden imports that PyInstaller misses
        "--hidden-import", "win32timezone",
        "--hidden-import", "win32com",
        "--hidden-import", "win32com.client",
        "--hidden-import", "win32process",

        # pystray Windows backend + Pillow
        "--hidden-import", "pystray._win32",
        "--collect-all", "pystray",
        "--collect-all", "PIL",

        # PySide6 — the Qt settings window. The PyInstaller hook pulls in the
        # required Qt plugins; we only need the three submodules we import.
        "--hidden-import", "PySide6.QtWidgets",
        "--hidden-import", "PySide6.QtGui",
        "--hidden-import", "PySide6.QtCore",
    ]

    if icon_path and icon_path.exists():
        cmd += ["--icon", str(icon_path)]

    cmd.append(str(HERE / "main.py"))

    print(f"  {' '.join(cmd[:6])} ...")
    result = subprocess.run(cmd, cwd=str(HERE))
    if result.returncode != 0:
        print("\n  [!] PyInstaller failed — see output above.")
        sys.exit(result.returncode)

    exe = HERE / "dist" / "WindowFocusManager.exe"
    if not exe.exists():
        print("\n  [!] Expected exe not found:", exe)
        sys.exit(1)

    size_mb = exe.stat().st_size / 1024 / 1024
    print(f"  dist/WindowFocusManager.exe  ({size_mb:.1f} MB)")
    return exe


# ---------------------------------------------------------------------------
# Stage 3 — Inno Setup
# ---------------------------------------------------------------------------

_ISCC_CANDIDATES = [
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
    r"C:\Program Files (x86)\Inno Setup 5\ISCC.exe",
    r"C:\Program Files\Inno Setup 5\ISCC.exe",
]


def _find_iscc() -> Path | None:
    for c in _ISCC_CANDIDATES:
        p = Path(c)
        if p.exists():
            return p
    return None


def _build_installer() -> None:
    """Run Inno Setup to produce the installer exe."""
    iscc = _find_iscc()
    if iscc is None:
        print("  [!] Inno Setup not found.")
        print("      Download: https://jrsoftware.org/isdl.php")
        print("      Then run: ISCC.exe installer.iss")
        return

    iss = HERE / "installer.iss"
    print(f"  Using: {iscc}")
    result = subprocess.run([str(iscc), str(iss)], cwd=str(HERE))
    if result.returncode != 0:
        print("\n  [!] Inno Setup failed — see output above.")
        sys.exit(result.returncode)

    installer = HERE / "installer_output" / "WindowFocusManager_Setup.exe"
    if installer.exists():
        size_mb = installer.stat().st_size / 1024 / 1024
        print(f"  installer_output/WindowFocusManager_Setup.exe  ({size_mb:.1f} MB)")
    else:
        print("  [!] Installer output not found — check installer.iss OutputDir.")


# ---------------------------------------------------------------------------
# Stage 4 — distributable zip
# ---------------------------------------------------------------------------

def _zip_release() -> None:
    """Package the installer into a zip the user downloads, unzips, and runs."""
    import zipfile

    installer = HERE / "installer_output" / "WindowFocusManager_Setup.exe"
    if not installer.exists():
        print("  [!] No installer to package — skipping zip stage.")
        return

    release_dir = HERE / "release"
    release_dir.mkdir(exist_ok=True)
    zip_path = release_dir / "WindowFocusManager_Setup.zip"

    readme = (
        "Windows Focus Manager — Installation\n"
        "====================================\n\n"
        "1. Double-click  WindowFocusManager_Setup.exe\n"
        "2. Follow the setup wizard (no admin rights needed).\n"
        "3. The app starts in your system tray. Right-click the tray icon\n"
        "   and choose 'Open Settings' to configure it.\n"
    )

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(installer, arcname="WindowFocusManager_Setup.exe")
        z.writestr("README.txt", readme)

    size_mb = zip_path.stat().st_size / 1024 / 1024
    print(f"  release/WindowFocusManager_Setup.zip  ({size_mb:.1f} MB)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("━" * 56)
    print("  Windows Focus Manager — Build")
    print("━" * 56)

    _kill_running_exe()

    print("\n[1/4] Generating icon ...")
    icon = _generate_icon()

    print("\n[2/4] Building exe with PyInstaller ...")
    _build_exe(icon)

    print("\n[3/4] Building installer with Inno Setup ...")
    _build_installer()

    print("\n[4/4] Packaging distributable zip ...")
    _zip_release()

    print("\n" + "━" * 56)
    print("  Done.  Share: release/WindowFocusManager_Setup.zip")
    print("━" * 56)


if __name__ == "__main__":
    main()
