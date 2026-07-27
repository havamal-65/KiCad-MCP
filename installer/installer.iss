; U4 installer (REQ-U4-002/004): per-user, no admin, bundled Python runtime.
; Compiled by installer\build.ps1:  ISCC installer.iss /DAppVersion=x /DDistDir=...

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef DistDir
  #define DistDir "dist\KiCad-MCP Launcher"
#endif

[Setup]
AppId={{8D2C1B7E-4A9F-4E3B-9C6D-2F1E7A5B0C43}
AppName=KiCad-MCP Launcher
AppVersion={#AppVersion}
AppPublisher=Evan Bonsignori
DefaultDirName={localappdata}\Programs\KiCad-MCP Launcher
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
DisableDirPage=yes
OutputDir=out
OutputBaseFilename=KiCad-MCP-Launcher-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
UninstallDisplayIcon={app}\KiCad-MCP Launcher.exe
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{userprograms}\KiCad-MCP Launcher"; Filename: "{app}\KiCad-MCP Launcher.exe"
Name: "{userdesktop}\KiCad-MCP Launcher"; Filename: "{app}\KiCad-MCP Launcher.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\KiCad-MCP Launcher.exe"; Description: "Launch KiCad-MCP Launcher"; Flags: nowait postinstall skipifsilent

; Uninstall removes only what this installer added ({app} + shortcuts + registry
; uninstall entry — automatic). Deliberately untouched (REQ-U4-004): the bridge
; the wizard installed into KiCad's plugin dirs and any MCP client config —
; those belong to the user's KiCad/client setup, not to this app.
