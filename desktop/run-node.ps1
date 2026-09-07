# RavenMap camera node - launcher (Windows).
#
# Starts the camera-control UI (which owns the webcam and serves its video to
# the detector) and then the detector, which posts sightings to the public
# RavenMap network. Installed by install-node-windows.ps1; also run at login
# and from the desktop shortcut.
#
# Set RAVEN_HUB (or legacy SPARROW_HUB) to point this at your hub before it
# runs (e.g. setx RAVEN_HUB http://localhost:8150). There is no implicit
# default hub; an unconfigured install fails clearly instead of guessing.

$ErrorActionPreference = 'Stop'
$App   = Split-Path $PSScriptRoot -Parent
$Py    = Join-Path $App '.venv\Scripts\python.exe'
$Place = Join-Path $App 'camctl\placement.json'
$Hub   = if ($env:RAVEN_HUB) { $env:RAVEN_HUB.TrimEnd('/') } elseif ($env:SPARROW_HUB) { $env:SPARROW_HUB.TrimEnd('/') } else { $null }

if (-not (Test-Path $Py)) {
  Write-Host "RavenMap is not installed yet. Run install-node-windows.ps1 first." -ForegroundColor Yellow
  Read-Host "Press Enter to close"; exit 1
}
if (-not $Hub) {
  Write-Host "No RavenMap hub configured. Set RAVEN_HUB to your hub URL." -ForegroundColor Yellow
  Write-Host "Legacy SPARROW_HUB is also accepted." -ForegroundColor Yellow
  Read-Host "Press Enter to close"; exit 1
}

function Camctl-Up {
  try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://localhost:8160/').StatusCode -eq 200 }
  catch { $false }
}
function Enrolled {
  if (-not (Test-Path $Place)) { return $false }
  try { [bool]((Get-Content $Place -Raw | ConvertFrom-Json).enrolled) } catch { $false }
}

Write-Host ""
Write-Host "  RavenMap camera node" -ForegroundColor Cyan
Write-Host "    posts to  $Hub"
Write-Host ""

# 1) Camera control: owns the webcam, re-serves it as MJPEG for the detector,
#    and hosts the aiming page. Start it first, or the detector opens the camera
#    first and the control page shows a black rectangle.
if (-not (Camctl-Up)) {
  Write-Host "  Starting camera control..."
  Start-Process -WindowStyle Minimized $Py -ArgumentList 'camctl\camctl.py' -WorkingDirectory $App | Out-Null
  for ($i = 0; $i -lt 40 -and -not (Camctl-Up); $i++) { Start-Sleep 1 }
}
if (-not (Camctl-Up)) {
  Write-Host "  Camera control did not start - is a webcam connected?" -ForegroundColor Yellow
  Read-Host "Press Enter to close"; exit 1
}

# 2) First run: aim the camera and draw the road. Saving enrolls the camera on
#    the network and writes its id + token to placement.json.
if (-not (Enrolled)) {
  Write-Host "  Opening the setup page. Aim your camera, draw the stretch of"
  Write-Host "  public road it watches, and click Save. Waiting for that..."
  Start-Process 'http://localhost:8160/'
  while (-not (Enrolled)) { Start-Sleep 3 }
  Write-Host "  Camera enrolled." -ForegroundColor Green
}

# Show the owner where to review their OWN camera's catches. A camera enrolled
# through camctl already has its reviewer token saved; one enrolled before that
# existed fetches it now, proving ownership with its node token. Minted once.
try {
  $place = Get-Content $Place -Raw | ConvertFrom-Json
  $rtok = $place.review_token
  if (-not $rtok -and $place.node_id -and $place.token) {
    $resp = Invoke-RestMethod -Method Post -Uri "$Hub/api/rv/my-token" `
      -Headers @{ Authorization = "Bearer $($place.token)" } -ContentType 'application/json' `
      -Body (@{ node_id = $place.node_id } | ConvertTo-Json) -TimeoutSec 15
    if ($resp.new -and $resp.review_token) {
      $rtok = $resp.review_token
      $place | Add-Member -NotePropertyName review_token -NotePropertyValue $rtok -Force
      $place | ConvertTo-Json | Set-Content $Place -Encoding UTF8
    }
  }
  if ($rtok) {
    Write-Host ""
    Write-Host "  Review your camera's catches:" -ForegroundColor Green
    Write-Host "    open $Hub/rv  and paste this token:" -ForegroundColor Green
    Write-Host "    $rtok" -ForegroundColor Green
    Write-Host ""
  }
} catch {}

# 3) Run the detector against the hub. It reads the node id + token from the
#    placement file (never pasted here), and is restarted if it dies for a real
#    reason. Exit code 3 - and only 3 - is the single-instance guard; do not
#    loop on it. It used to test 1, which is ALSO what Python returns for any
#    unhandled exception, so a crashing detector was reported to the volunteer
#    as "another detector is already running" and the loop stopped. Their camera
#    went dark behind a message pointing at the wrong thing.
while ($true) {
  $place = Get-Content $Place -Raw | ConvertFrom-Json
  Write-Host "  Detector running as $($place.node_id). Close this window to stop."
  & $Py 'detect\run_live.py' --post --visual --hub $Hub --node $place.node_id --token $place.token
  if ($LASTEXITCODE -eq 3) {
    Write-Host "  Another detector is already running for this camera. Close it first." -ForegroundColor Yellow
    Read-Host "Press Enter to close"; break
  }
  Write-Host "  Detector exited; restarting in 10s (close this window to stop)." -ForegroundColor Yellow
  Start-Sleep 10
}
