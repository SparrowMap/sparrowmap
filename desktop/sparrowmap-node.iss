; SparrowMap Camera - Windows installer (Inno Setup 6).
;
; This is a small BOOTSTRAPPER, not a bundle. It ships the two PowerShell
; scripts and, on finish, runs install-node-windows.ps1, which installs Python,
; downloads the camera code and the recognition models (~2.5 GB), and sets the
; camera up to contribute to a configured RavenMap hub. Bundling PyTorch + CLIP
; into the .exe would make it multi-gigabyte and fragile; bootstrapping keeps the
; installer tiny and always current.
;
; Build it:
;     iscc desktop\sparrowmap-node.iss        ->  Output\SparrowMapCameraSetup.exe
; CI builds and attaches it to each release: .github/workflows/build-node-installer.yml
;
; No admin: it installs per-user. Self-hosters set SPARROW_HUB before running the
; produced installer and it points at their own hub instead.

[Setup]
AppId={{7A1E2D64-5C3B-4F2A-9E7C-5B1D4C9A2F30}
AppName=RavenMap Camera
AppVersion=1.0
AppPublisher=RavenMap
DefaultDirName={userappdata}\SparrowMap\bootstrap
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=SparrowMapCameraSetup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
DisableWelcomePage=no

[Messages]
WelcomeLabel2=This will set up your webcam as a RavenMap camera.%n%nIt installs Python and the recognition models (about 2.5 GB downloaded on first setup) under your user account. No administrator rights are needed. Configure RAVEN_HUB (or legacy SPARROW_HUB) for your hub.

[Files]
Source: "install-node-windows.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "run-node.ps1";             DestDir: "{app}"; Flags: ignoreversion

[Run]
; Kick off the real installer once files are in place. runascurrentuser keeps it
; per-user; the PS1 does the heavy lifting (Python, clone, pip, shortcut, login task).
Filename: "powershell.exe"; \
  Parameters: "-ExecutionPolicy Bypass -NoProfile -File ""{app}\install-node-windows.ps1"""; \
  Description: "Set up the RavenMap camera now"; \
  Flags: nowait postinstall skipifsilent runascurrentuser
