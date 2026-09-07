# SparrowMap - desktop camera, Windows.
#
#   powershell -ExecutionPolicy Bypass -File install-windows.ps1 https://your-hub.example
#
# ## What this installs, and what it deliberately does not
#
# It does NOT install SparrowMap. The camera runs entirely in the browser - there
# is no program to install, and adding one would mean asking people to trust a
# binary when they do not have to.
#
# What it fixes is the one real problem a browser cannot solve for itself:
# **a hidden or minimised tab is frozen by the browser and sees nothing.**
# The app says so on screen, but "leave this window visible" is a poor
# instruction to give someone setting up a camera for a month.
#
# So this creates a shortcut that opens SparrowMap as its OWN WINDOW (Chrome's
# --app mode: no tabs, no address bar, looks like a program), and optionally
# starts it at login. That is the whole difference between "a web page you
# have to remember" and "a thing that runs".

param(
  [string]$Url = $(if ($env:RAVEN_HUB) { $env:RAVEN_HUB } else { $env:SPARROW_HUB }),
  [switch]$NoAutostart
)

$ErrorActionPreference = 'Stop'
function Say($m) { Write-Host "==> $m" -ForegroundColor Cyan }

if (-not $Url) {
  throw "No RavenMap hub configured. Set RAVEN_HUB or pass the hub URL explicitly. Legacy SPARROW_HUB is also accepted; no upstream default is used."
}
if ($Url -notmatch '^https?://') { throw "Url must start with http:// or https://" }
$AppUrl = ($Url.TrimEnd('/')) + '/app'

# --- find a Chromium browser ------------------------------------------------
# Chromium's --app flag is what gives a chromeless window. Firefox has no
# equivalent, so it is not offered rather than silently producing a normal tab
# that will be minimised and freeze.
Say "Looking for Chrome or Edge"
$candidates = @(
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
  "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
  "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
  "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
)
$browser = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $browser) {
  throw "No Chrome or Edge found. Install either, then run this again."
}
Write-Host "    $browser"

# A separate profile directory, so the camera window has its own permissions
# and its own storage - the camera key included. Running in the daily profile
# means one 'clear browsing data' silently deletes the camera key.
$profileDir = Join-Path $env:LOCALAPPDATA 'SparrowCamera'
New-Item -ItemType Directory -Force -Path $profileDir | Out-Null

$argLine = "--app=$AppUrl --user-data-dir=`"$profileDir`" --start-maximized"

# --- shortcut ---------------------------------------------------------------
Say "Creating the RavenMap Camera shortcut"
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop 'SparrowMap Camera.lnk'
$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($lnk)
$s.TargetPath = $browser
$s.Arguments = $argLine
$s.WorkingDirectory = Split-Path $browser
$s.Description = 'RavenMap - watch the road from this computer'
$s.Save()
Write-Host "    $lnk"

# --- autostart --------------------------------------------------------------
if (-not $NoAutostart) {
  Say "Starting it at login"
  $startup = [Environment]::GetFolderPath('Startup')
  Copy-Item $lnk (Join-Path $startup 'SparrowMap Camera.lnk') -Force
  Write-Host "    $startup"
} else {
  Say "Skipping autostart (-NoAutostart)"
}

# --- sleep ------------------------------------------------------------------
# A machine that sleeps is a camera that stops. Say it rather than silently
# producing a node that works for two hours a day.
$plan = (powercfg /getactivescheme) -replace '.*GUID: ([0-9a-f-]+).*', '$1'
Say "Power"
Write-Host "    A sleeping computer stops watching. To keep this one awake:"
Write-Host "      powercfg /change standby-timeout-ac 0"
Write-Host "      powercfg /change monitor-timeout-ac 0"
Write-Host "    (the app also holds a screen wake lock while it is running)"

Write-Host ""
Say "Done"
Write-Host @"
    Open 'RavenMap Camera' on your desktop.

    First time: name the camera, allow the camera and location, press
    Register, then Start watching. The detector downloads once (~21 MB)
    and is cached after that.

    Nothing was installed. This is a shortcut to a web page in its own
    window - you can delete it and nothing is left behind except the
    browser profile at:
      $profileDir
"@
