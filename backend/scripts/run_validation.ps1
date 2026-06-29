# Phase 2 Step B schema validation — Windows / PowerShell entrypoint.
#
# Brings up the local pgvector Postgres via docker compose, then runs the
# Python orchestrator that performs upgrade / downgrade / re-upgrade /
# integrity checks / ERD regeneration.
#
# Usage (from the backend/ folder):
#   .\scripts\run_validation.ps1
#
# Requirements: Docker Desktop, Python 3.12+, ``pip install -e .[validation]``

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendDir = Split-Path -Parent $ScriptDir

Push-Location $BackendDir
try {
    Write-Host "== docker compose up -d (postgres + pgvector) =="
    docker compose -f docker-compose.db.yml up -d

    Write-Host "== installing python deps =="
    python -m pip install -e .

    Write-Host "== running validation =="
    $env:DATABASE_URL = "postgresql+psycopg://aivp:aivp@localhost:5432/aivp"
    python scripts/run_validation.py
}
finally {
    Pop-Location
}
