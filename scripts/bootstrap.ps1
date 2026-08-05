$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

Write-Host "[FMIndex] repository: $repoRoot"

$requiredCommands = @('git', 'node')
foreach ($command in $requiredCommands) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "Required command not found: $command"
    }
}

$nodeMajor = [int]((node --version).TrimStart('v').Split('.')[0])
if ($nodeMajor -ne 24) {
    throw "Node.js 24 LTS is required. Current version: $(node --version)"
}

if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
    if (-not (Get-Command corepack -ErrorAction SilentlyContinue)) {
        throw 'pnpm is missing and Corepack is unavailable. Install Node.js 24 LTS with Corepack support.'
    }

    Write-Host '[FMIndex] enabling Corepack and pnpm 11.4.0'
    corepack enable
    corepack prepare pnpm@11.4.0 --activate
}

$directories = @(
    'apps/collector',
    'apps/analyzer',
    'apps/api',
    'apps/web',
    'packages/contracts',
    'packages/taxonomy',
    'packages/source-adapters',
    'data/raw',
    'data/normalized',
    'data/aggregates',
    'data/checkpoints',
    'data/quarantine',
    'logs',
    'run'
)

foreach ($directory in $directories) {
    New-Item -ItemType Directory -Force -Path (Join-Path $repoRoot $directory) | Out-Null
}

if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Write-Host '[FMIndex] created .env from .env.example'
} else {
    Write-Host '[FMIndex] existing .env preserved'
}

Write-Host '[FMIndex] installing root workspace dependencies'
pnpm install

Write-Host '[FMIndex] running environment doctor'
powershell -NoProfile -ExecutionPolicy Bypass -File "$PSScriptRoot/doctor.ps1"

Write-Host ''
Write-Host '[FMIndex] bootstrap complete.'
Write-Host 'Collector remains disabled until COLLECTOR_ENABLED=true is set after source-policy review.'
