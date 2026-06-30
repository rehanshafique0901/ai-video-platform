<#
.SYNOPSIS
  Run the full CI quality gate locally (Windows).

.DESCRIPTION
  Single-command equivalent of the GitHub Actions workflow at
  .github/workflows/ci.yml. Runs the same 10 stages, in the same order,
  using the same Python entrypoint (`scripts/ci_gate.py`).

  Live-DB stages (5–9) require either:
    - DATABASE_URL exported in the current shell, OR
    - backend/.env.validation containing DATABASE_URL=...
  When neither is present the live-DB stages are SKIPPED (reported as
  such in the summary) but the gate still passes on stages 1–4 + 10.

.PARAMETER Stages
  Stage range to run; same syntax as ci_gate.py --stages.
  Examples:  -Stages "1-4"   -Stages "1-4,10"   -Stages "8-9"

.EXAMPLE
  .\scripts\run_ci_gate.ps1
  # Run all 10 stages.

.EXAMPLE
  .\scripts\run_ci_gate.ps1 -Stages "1-4"
  # Quick pre-push check; no DB needed.
#>

[CmdletBinding()]
param(
    [string]$Stages = ""
)

$ErrorActionPreference = "Stop"

# Always operate from the backend root regardless of where the user
# invoked the script — paths inside ci_gate.py are anchored to this dir.
$BackendRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $BackendRoot

# Ensure the .validation artefact directory exists (ci_gate.py also does
# this, but creating it up-front lets the wrapper log artefact paths).
$ValidationDir = Join-Path $BackendRoot ".validation"
if (-not (Test-Path $ValidationDir)) {
    New-Item -ItemType Directory -Path $ValidationDir | Out-Null
}

Write-Host ""
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host " CI Quality Gate (local run)" -ForegroundColor Cyan
Write-Host " Backend root: $BackendRoot"
if ($env:DATABASE_URL) {
    $redacted = $env:DATABASE_URL -replace "//([^:]+):[^@]+@", "//`$1:***@"
    Write-Host " DATABASE_URL: $redacted"
} elseif (Test-Path (Join-Path $BackendRoot ".env.validation")) {
    Write-Host " DATABASE_URL: (loaded from backend/.env.validation at runtime)"
} else {
    Write-Host " DATABASE_URL: (unset; live-DB stages will be skipped)" -ForegroundColor Yellow
}
Write-Host "================================================================" -ForegroundColor Cyan

$args = @("--no-color")
if ($Stages) { $args += @("--stages", $Stages) }

# Stream Python output through directly so the user sees the same
# progress they'd see in CI logs. `python` is preferred over `py` so we
# pick up the active venv if one is activated. ci_gate.py loads
# DATABASE_URL from backend/.env.validation on its own (via _load_env),
# so we deliberately do NOT source the env file into the PowerShell
# session here.
python (Join-Path $BackendRoot "scripts\ci_gate.py") @args
$gateExit = $LASTEXITCODE

Write-Host ""
if ($gateExit -eq 0) {
    Write-Host "================================================================" -ForegroundColor Green
    Write-Host " Gate result: PASSED" -ForegroundColor Green
    Write-Host "================================================================" -ForegroundColor Green
} else {
    Write-Host "================================================================" -ForegroundColor Red
    Write-Host " Gate result: FAILED (exit $gateExit)" -ForegroundColor Red
    Write-Host " Artefacts: $ValidationDir"
    Write-Host "================================================================" -ForegroundColor Red
}

exit $gateExit
