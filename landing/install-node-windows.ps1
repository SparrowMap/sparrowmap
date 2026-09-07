# RavenMap desktop camera - turn-key installer for Windows.
#
# Turns a Windows PC with a webcam into a RavenMap camera that contributes to
# the configured RavenMap hub. Installs Python and the camera
# code, sets up the detector, and starts you at the "aim your camera" step.
#
#   Configure RAVEN_HUB and RAVEN_REPO, then run this script from the checkout.
#
#   Or download and run:
#     powershell -ExecutionPolicy Bypass -File install-node-windows.ps1
#
# It installs entirely under your user account (no admin needed for the app
# itself; winget may prompt to install Python/Git the first time). Re-running it
# updates an existing install. Set RAVEN_HUB (or legacy SPARROW_HUB) to your
# hub before running; there is no implicit default.

$ErrorActionPreference = 'Stop'
$Hub = if ($env:RAVEN_HUB) { $env:RAVEN_HUB.TrimEnd('/') } elseif ($env:SPARROW_HUB) { $env:SPARROW_HUB.TrimEnd('/') } else { $null }
if (-not $Hub) {
  Write-Error "No RavenMap hub configured. Set RAVEN_HUB to your hub URL. Legacy SPARROW_HUB is also accepted."
  exit 1
}
$Repo = if ($env:RAVEN_REPO) { $env:RAVEN_REPO } elseif ($env:SPARROW_REPO) { $env:SPARROW_REPO } else { $null }
if (-not $Repo) {
  Write-Error "No RavenMap source repository configured. Set RAVEN_REPO. Legacy SPARROW_REPO is also accepted; no upstream default is used."
  exit 1
}
$Root = Join-Path $env:LOCALAPPDATA 'SparrowMap'
$App  = Join-Path $Root 'app'

function Say($m)  { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "    $m" -ForegroundColor Yellow }

Write-Host ""
Write-Host "  RavenMap desktop camera setup" -ForegroundColor Cyan
Write-Host "  Contributes a real camera to $Hub"
Write-Host ""

# --- prerequisites: git + python (installed via winget if missing) ----------
function Have($name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }
function Winget-Install($id, $label) {
  if (-not (Have winget)) {
    throw "Need $label but winget is not available. Install $label manually, then re-run."
  }
  Say "Installing $label..."
  winget install --id $id -e --source winget --accept-package-agreements --accept-source-agreements --disable-interactivity
  # winget updates PATH for new shells, not this one - find the exe directly.
  $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' +
              [Environment]::GetEnvironmentVariable('Path','User')
}

if (-not (Have git)) { Winget-Install 'Git.Git' 'Git' }

function Python-Cmd {
  foreach ($c in @('python','py')) {
    if (Have $c) {
      try {
        $v = & $c -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
        if ($v -and [version]$v -ge [version]'3.10') { return $c }
      } catch {}
    }
  }
  return $null
}
if (-not (Python-Cmd)) { Winget-Install 'Python.Python.3.12' 'Python 3.12' }
$PY = Python-Cmd
if (-not $PY) { throw "Python 3.10+ still not found after install. Open a new terminal and re-run." }

# --- get / update the code ---------------------------------------------------
if (Test-Path (Join-Path $App '.git')) {
  Say "Updating RavenMap in $App"
  git -C $App pull --ff-only
} else {
  Say "Downloading RavenMap to $App"
  New-Item -ItemType Directory -Force $Root | Out-Null
  git clone --depth 1 $Repo $App
}

# --- python environment + dependencies --------------------------------------
$Venv = Join-Path $App '.venv'
if (-not (Test-Path (Join-Path $Venv 'Scripts\python.exe'))) {
  Say "Creating a Python environment"
  & $PY -m venv $Venv
}
$Py = Join-Path $Venv 'Scripts\python.exe'
& $Py -m pip install --upgrade pip --quiet

# Torch first, matched to the machine: CUDA build if an NVIDIA GPU is present
# (much faster), CPU build otherwise (works fine for one camera).
Say "Installing the recognition models (this is ~2.5 GB and can take a while)"
if (Have nvidia-smi) {
  Write-Host "    NVIDIA GPU detected - installing the CUDA build of PyTorch."
  & $Py -m pip install torch==2.5.1 --index-url https://download.pytorch.org/whl/cu121
} else {
  Write-Host "    No NVIDIA GPU - installing the CPU build of PyTorch (fine for one camera)."
  & $Py -m pip install torch==2.5.1
}
& $Py -m pip install -r (Join-Path $App 'requirements-node.txt')

# --- point this machine at the configured hub --------------------------------
[Environment]::SetEnvironmentVariable('RAVEN_HUB', $Hub, 'User')
$env:RAVEN_HUB = $Hub

# --- shortcut + start at login ----------------------------------------------
$Launcher = Join-Path $App 'desktop\run-node.ps1'
$Cmd = "powershell.exe"
$Args = "-ExecutionPolicy Bypass -NoExit -File `"$Launcher`""

Say "Creating a 'RavenMap Camera' shortcut and starting it at login"
try {
  $ws = New-Object -ComObject WScript.Shell
  foreach ($dir in @([Environment]::GetFolderPath('Desktop'),
                     (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))) {
    $lnk = $ws.CreateShortcut((Join-Path $dir 'SparrowMap Camera.lnk'))
    $lnk.TargetPath = $Cmd
    $lnk.Arguments = $Args
    $lnk.WorkingDirectory = $App
    $lnk.IconLocation = "$Cmd,0"
    $lnk.Description = "RavenMap camera node"
    $lnk.Save()
  }
} catch { Warn "Could not create a shortcut ($_). You can still run desktop\run-node.ps1." }

# Start at login (per-user scheduled task; no admin, no UAC prompt at login).
try {
  $action  = New-ScheduledTaskAction -Execute $Cmd -Argument $Args -WorkingDirectory $App
  $trigger = New-ScheduledTaskTrigger -AtLogOn
  $set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
  Register-ScheduledTask -TaskName 'SparrowMap Camera' -Action $action -Trigger $trigger `
    -Settings $set -Force -ErrorAction Stop | Out-Null
} catch { Warn "Could not register the login task ($_). Use the desktop shortcut to start it." }

# --- go ----------------------------------------------------------------------
Say "Done. Opening the camera setup - aim your camera and draw the road it watches."
Write-Host ""
Write-Host "  To review your camera's catches later, ask for a reviewer link to" -ForegroundColor Green
Write-Host "  $Hub/rv" -ForegroundColor Green
Write-Host ""
Start-Process powershell -ArgumentList "-ExecutionPolicy Bypass -NoExit -File `"$Launcher`"" -WorkingDirectory $App
