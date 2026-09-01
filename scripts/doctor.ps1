$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$failures = New-Object System.Collections.Generic.List[string]
$warnings = New-Object System.Collections.Generic.List[string]

function Test-CommandVersion {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$VersionCommand
    )

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        $failures.Add("Missing command: $Name")
        return $null
    }

    try {
        $output = & $VersionCommand 2>&1
        if ($LASTEXITCODE -ne 0) {
            $failures.Add("Could not read $Name version: exit code $LASTEXITCODE")
            return $null
        }
        return $output.Trim()
    } catch {
        $failures.Add("Could not read $Name version: $($_.Exception.Message)")
        return $null
    }
}

Write-Host "[FMIndex doctor] repository: $repoRoot"

$gitVersion = Test-CommandVersion -Name 'git' -VersionCommand { git --version }
$nodeVersion = Test-CommandVersion -Name 'node' -VersionCommand { node --version }
$pnpmVersion = Test-CommandVersion -Name 'pnpm' -VersionCommand { pnpm --version }

if ($gitVersion) { Write-Host "  git:  $gitVersion" }
if ($nodeVersion) {
    Write-Host "  node: $nodeVersion"
    $nodeMajor = [int]($nodeVersion.TrimStart('v').Split('.')[0])
    if ($nodeMajor -ne 24) {
        $failures.Add("Node.js 24 LTS required; found $nodeVersion")
    }
}
if ($pnpmVersion) {
    Write-Host "  pnpm: $pnpmVersion"
    $pnpmMajor = [int]($pnpmVersion.Split('.')[0])
    if ($pnpmMajor -ne 11) {
        $failures.Add("pnpm 11 required; found $pnpmVersion")
    }
}

# Python checks
$pythonVersion = Test-CommandVersion -Name 'python' -VersionCommand { python --version }
if ($pythonVersion) {
    Write-Host "  python: $pythonVersion"
} else {
    $failures.Add('Python is required but not found.')
}

# pytest readiness
if ($pythonVersion) {
    try {
        $pytestCheck = python -c "import pytest; print(pytest.__version__)" 2>&1
        $pytestExit = $LASTEXITCODE
        if ($pytestExit -ne 0) {
            $failures.Add("pytest import check failed (exit $pytestExit): $pytestCheck")
        } else {
            Write-Host "  pytest: $pytestCheck"
        }
    } catch {
        $failures.Add('pytest import check failed: ' + $_.Exception.Message)
    }
}

# fmindex import readiness
if ($pythonVersion) {
    try {
        $fmindexCheck = python -c "import fmindex; print(fmindex.__version__)" 2>&1
        $fmindexExit = $LASTEXITCODE
        if ($fmindexExit -ne 0) {
            $failures.Add("fmindex import check failed (exit $fmindexExit): $fmindexCheck")
        } else {
            Write-Host "  fmindex: v$fmindexCheck"
        }
    } catch {
        $failures.Add('fmindex import check failed: ' + $_.Exception.Message)
    }
}

if (-not (Test-Path '.git')) {
    $failures.Add('Current directory is not a Git checkout.')
} else {
    $remote = (git remote get-url origin 2>$null)
    if (-not $remote) {
        $warnings.Add('Git remote origin is not configured.')
    } elseif ($remote -notmatch 'skerishKang/fmindex') {
        $warnings.Add("Unexpected origin remote: $remote")
    } else {
        Write-Host "  origin: $remote"
    }
}

$requiredFiles = @(
    'README.md',
    '.env.example',
    'package.json',
    'pnpm-workspace.yaml',
    'docs/PRODUCT.md',
    'docs/ARCHITECTURE.md',
    'docs/OPERATIONS.md',
    'docs/DATA_POLICY.md',
    'docs/LABELING_GUIDE.md',
    'docs/LOCAL_SETUP.md',
    'docs/ROADMAP.md'
)

foreach ($file in $requiredFiles) {
    if (-not (Test-Path $file)) {
        $failures.Add("Missing required file: $file")
    }
}

$requiredDirectories = @(
    'data/raw',
    'data/normalized',
    'data/aggregates',
    'data/checkpoints',
    'data/quarantine',
    'logs',
    'run'
)

foreach ($directory in $requiredDirectories) {
    if (-not (Test-Path $directory)) {
        $warnings.Add("Runtime directory not created yet: $directory")
    }
}

if (-not (Test-Path '.env')) {
    $warnings.Add('.env is missing. Run scripts/bootstrap.ps1 or copy .env.example to .env.')
} else {
    $envText = Get-Content '.env' -Raw
    if ($envText -match '(?m)^COLLECTOR_ENABLED=true\s*$') {
        $warnings.Add('Collector is enabled. Confirm source policy and request limits before running it.')
    }
    if ($envText -match '(?m)^LLM_MODEL=\s*$') {
        $warnings.Add('LLM_MODEL is empty; analysis cannot run until a local model is selected.')
    }
}

if (Test-Path 'node_modules') {
    Write-Host '  dependencies: installed'
} else {
    $warnings.Add('node_modules is missing. Run pnpm install.')
}

foreach ($warning in $warnings) {
    Write-Warning $warning
}

if ($failures.Count -gt 0) {
    Write-Host ''
    Write-Host '[FMIndex doctor] FAIL' -ForegroundColor Red
    foreach ($failure in $failures) {
        Write-Host "  - $failure" -ForegroundColor Red
    }
    exit 1
}

Write-Host ''
Write-Host '[FMIndex doctor] PASS' -ForegroundColor Green
if ($warnings.Count -gt 0) {
    Write-Host "Warnings: $($warnings.Count)"
}
exit 0
