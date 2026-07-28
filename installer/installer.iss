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
; WebView2 Evergreen bootstrapper -- extracted to {tmp}, run during install (see
; [Run]/[Code]), removed after. Guarded so the installer still compiles if the
; build did not fetch it (app's MessageBox fallback still covers that machine).
#ifdef WebView2Setup
Source: "{#WebView2Setup}"; DestDir: "{tmp}"; Flags: deleteafterinstall
#endif

[Icons]
Name: "{userprograms}\KiCad-MCP Launcher"; Filename: "{app}\KiCad-MCP Launcher.exe"
Name: "{userdesktop}\KiCad-MCP Launcher"; Filename: "{app}\KiCad-MCP Launcher.exe"; Tasks: desktopicon

[Run]
; Provision the WebView2 runtime first (QU4-4, zero manual steps) -- only when it
; is absent; the bootstrapper is a per-user, idempotent no-op when present.
#ifdef WebView2Setup
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Installing Microsoft WebView2 runtime (one-time)..."; Check: NeedsWebView2
#endif
Filename: "{app}\KiCad-MCP Launcher.exe"; Description: "Launch KiCad-MCP Launcher"; Flags: nowait postinstall skipifsilent

; Uninstall removes only what this installer added ({app} + shortcuts + registry
; uninstall entry -- automatic). Deliberately untouched (REQ-U4-004): the bridge
; the wizard installed into KiCad's plugin dirs and any MCP client config --
; those belong to the user's KiCad/client setup, not to this app. The bundled
; WebView2 runtime is a shared Microsoft component and is left installed too.

; [Code] must be the final section — everything below it is parsed as Pascal.
[Code]
{ True when the WebView2 Evergreen runtime is NOT installed -- same client GUID
  and pv-value probe launcher/app.py uses (HKLM 64/32-bit views + HKCU). }
function HasRuntime(RootKey: Integer; SubKey: String): Boolean;
var
  pv: String;
begin
  Result := RegQueryStringValue(RootKey, SubKey, 'pv', pv) and (pv <> '') and (pv <> '0.0.0.0');
end;

function NeedsWebView2: Boolean;
begin
  Result := not (
    HasRuntime(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}') or
    HasRuntime(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}') or
    HasRuntime(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'));
end;
