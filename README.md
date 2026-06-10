# Windows Focus Manager

A lightweight tiling window manager for Windows that automatically expands your focused window to fill its share of the screen — no keyboard shortcuts required. It lives in the system tray and arranges windows as gap-free tiles whenever you switch focus.

<p align="center">
  <img src="docs/demo.gif" alt="Windows Focus Manager tiling windows as focus changes" width="720">
</p>

## Features

- **Click-to-tile** — focus a window and every window on that monitor snaps into contiguous, gap-free tiles; the focused window gets the largest share.
- **Hover mode** *(optional)* — windows tile as your cursor rests on them.
- **Smooth animations** — gap-free transitions with no visible desktop flash mid-animation.
- **Per-monitor** — each monitor tiles independently; landscape splits left/right, portrait splits top/bottom.
- **App exclusions** — skip specific windows by title or executable (e.g. Spotify, picture-in-picture).
- **Native settings window** — configure everything from a real desktop window (no browser, no background server).
- **System tray** — Pause/Resume, Open Settings, Exit.
- **Start with Windows** — optional auto-launch at login.
- **No admin rights** — installs per-user with no UAC prompt.

## Requirements

- Windows 10 or 11
- No Python required — the installer bundles everything.

## Installation

1. Download **`WindowFocusManager_Setup.exe`** from the [Releases](https://github.com/CodyAdams0744/Windows-Focus-Manager/releases) page.
2. Run it. Windows Focus Manager installs per-user (no admin prompt) and starts in your system tray.

> [!NOTE]
> **"Windows protected your PC"?** This app isn't code-signed (signing certificates are expensive for a free project), so Windows SmartScreen may warn you the first time you run it. This is expected for small open-source tools. To proceed, click **More info → Run anyway**. The source is all here if you'd rather build it yourself.

## Usage

Once running, it works automatically:

- **Click** any window to focus it — the windows on that monitor retile, with the focused one getting the most space.
- **Right-click the system tray icon** to Pause/Resume, open Settings, or exit.

### Settings

Open Settings from the tray icon to configure:

- **Expand ratio** — how much of the screen the focused window gets.
- **Hover mode** and hover delay.
- **Animation** on/off and duration.
- **Monitors** — choose which displays to manage.
- **App exclusions** — skip windows by title or executable.
- **Start with Windows.**

Changes apply immediately on save — no restart needed.

<p align="center">
  <img src="docs/WFMNewUi.png" alt="Windows Focus Manager settings dashboard" width="920">
</p>

## Building from Source

> This section is **only for developers** who want to build the app themselves. If you just want to use it, download the installer from [Releases](https://github.com/CodyAdams0744/Windows-Focus-Manager/releases) — no Python needed.

**To build, you'll need:** Python 3.8+, and [Inno Setup 6](https://jrsoftware.org/isdl.php) (for the installer stage).

```bash
pip install -r requirements.txt
python Scripts/pywin32_postinstall.py -install   # one-time pywin32 setup
python build.py
```

`build.py` runs four stages — icon → PyInstaller exe → Inno Setup wizard → zip — and produces:

- `dist/WindowFocusManager.exe` — the standalone app
- `installer_output/WindowFocusManager_Setup.exe` — the installer wizard
- `release/WindowFocusManager_Setup.zip` — zipped installer

To run from source without building:

```bash
python main.py
```

## Support

Windows Focus Manager is free and open source. If you find it useful, you can [buy me a coffee](https://buymeacoffee.com/cody0744) ☕ — it's appreciated but never required.

## License

[MIT](LICENSE)
