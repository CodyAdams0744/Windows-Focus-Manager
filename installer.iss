; installer.iss — Inno Setup script for Windows Focus Manager
;
; Produces a per-user installer that requires NO administrator / UAC prompt.
; Output: installer_output\WindowFocusManager_Setup.exe
;
; Build with:
;   ISCC.exe installer.iss
; or just run:
;   python build.py   (calls ISCC automatically if Inno Setup 6 is installed)
;
; Download Inno Setup 6 from: https://jrsoftware.org/isdl.php

#define AppName     "Windows Focus Manager"
#define AppVersion  "1.2.0"
#define AppExe      "WindowFocusManager.exe"
; Unique GUID that identifies this app in the Windows registry.
; Never change this value — doing so breaks upgrade detection.
#define AppId       "{{3E7B2C1A-9D4F-4E5B-8C6D-7E8F9A0B1C2D}"

; ---------------------------------------------------------------------------
; Setup metadata
; ---------------------------------------------------------------------------

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=ClaudeApps
AppPublisherURL=https://github.com/CodyAdams0744/windows-focus-manager

; Install to %LOCALAPPDATA%\WindowFocusManager — no admin rights needed.
DefaultDirName={localappdata}\WindowFocusManager
DisableDirPage=yes

; Skip the "Select Start Menu folder" page — use a sensible default.
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes

; Output
OutputDir=installer_output
OutputBaseFilename=WindowFocusManager_Setup
Compression=lzma2/ultra64
SolidCompression=yes

; Appearance
WizardStyle=modern
WizardSizePercent=100
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}

; Per-user install — no UAC prompt.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline

; Embed version info that shows in the installer's file Properties dialog.
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName} Setup
VersionInfoCompany=ClaudeApps

; ---------------------------------------------------------------------------
; Languages
; ---------------------------------------------------------------------------

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

; ---------------------------------------------------------------------------
; Optional tasks shown on the "Select Additional Tasks" wizard page
; ---------------------------------------------------------------------------

[Tasks]
Name: "desktopicon"; \
  Description: "Create a &desktop shortcut"; \
  GroupDescription: "Additional shortcuts:"; \
  Flags: unchecked

; ---------------------------------------------------------------------------
; Files to install
; ---------------------------------------------------------------------------

[Files]
; The single-file exe produced by PyInstaller.
Source: "dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion

; ---------------------------------------------------------------------------
; Shortcuts
; ---------------------------------------------------------------------------

[Icons]
; Start Menu
Name: "{autoprograms}\{#AppName}"; \
  Filename: "{app}\{#AppExe}"; \
  Comment: "Automatically expand the focused window"

; Desktop (only created if the user ticked the task above)
Name: "{autodesktop}\{#AppName}"; \
  Filename: "{app}\{#AppExe}"; \
  Comment: "Automatically expand the focused window"; \
  Tasks: desktopicon

; ---------------------------------------------------------------------------
; Registry — clean up autostart entry on uninstall
; (The app writes this itself when the user enables "Start with Windows";
;  the installer never creates it, but we remove it on uninstall so
;  nothing is left dangling in the Run key.)
; ---------------------------------------------------------------------------

[Registry]
Root: HKCU; \
  Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
  ValueName: "WindowFocusManager"; \
  Flags: uninsdeletevalue dontcreatekey

; ---------------------------------------------------------------------------
; Run after install
; ---------------------------------------------------------------------------

[Run]
; Offer to launch the app at the end of the installer wizard.
Filename: "{app}\{#AppExe}"; \
  Description: "&Launch {#AppName}"; \
  Flags: nowait postinstall skipifsilent

; ---------------------------------------------------------------------------
; Cleanup on uninstall
; ---------------------------------------------------------------------------

[UninstallRun]
; Stop the app before the uninstaller removes files.
Filename: "taskkill"; \
  Parameters: "/F /IM {#AppExe}"; \
  Flags: runhidden waituntilterminated; \
  RunOnceId: "KillWFM"

[UninstallDelete]
; Remove runtime files the app creates next to the exe.
; config.json is intentionally preserved so re-installs keep user settings;
; delete those lines if you want a full clean wipe instead.
Type: files; Name: "{app}\wfm.log"
Type: files; Name: "{app}\wfm.log.1"
Type: files; Name: "{app}\wfm.log.2"
Type: files; Name: "{app}\wfm.log.3"
; Remove the install directory if empty after uninstall.
Type: dirifempty; Name: "{app}"
