<#
.SYNOPSIS
    Install or update Agentic Preflight and its agent integrations on Windows.

.DESCRIPTION
    The PowerShell counterpart to install.sh. It exists because the bash
    installer cannot run on a stock Windows machine, and requiring a POSIX
    shell to install a tool that no longer needs one would be a strange first
    impression.

    Behaviour is deliberately identical to install.sh: install the CLI from this
    checkout with uv, then refresh the agent skills for the requested agents,
    defaulting to all five.

.PARAMETER Agents
    Which agent integrations to install. Defaults to every supported agent.

.EXAMPLE
    .\install.ps1

.EXAMPLE
    .\install.ps1 codex claude
#>

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Agents = @()
)

$ErrorActionPreference = 'Stop'

function Find-AgenticPreflight {
    <#
        The launcher uv writes is an .exe today, but the extension is uv's
        choice rather than a contract. Probing the forms Windows can execute
        keeps this working if that changes, and lets the installer be tested
        without fabricating a real executable.
    #>
    param([string]$Directory)

    foreach ($extension in @('.exe', '.cmd', '.bat', '')) {
        $candidate = Join-Path $Directory "agentic-preflight$extension"
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    return $null
}

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error 'uv is required; install it from https://docs.astral.sh/uv/'
    exit 1
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Error 'git is required; install Git for Windows from https://git-scm.com/download/win'
    exit 1
}

if ($Agents.Count -eq 0) {
    $Agents = @('codex', 'claude', 'cursor', 'opencode', 'amp')
}

Write-Host "Installing agentic-preflight from $repoRoot"
uv tool install --force --reinstall $repoRoot
if ($LASTEXITCODE -ne 0) {
    Write-Error 'uv tool install failed'
    exit 1
}

$toolBinDir = (uv tool dir --bin).Trim()
$agenticPreflightBin = Find-AgenticPreflight $toolBinDir
if (-not $agenticPreflightBin) {
    Write-Error "uv installed the tool, but no agentic-preflight launcher is in $toolBinDir"
    exit 1
}

Write-Host "Installing or updating agent skills for: $($Agents -join ' ')"
& $agenticPreflightBin integrations install @Agents
if ($LASTEXITCODE -ne 0) {
    Write-Error 'installing the agent integrations failed'
    exit 1
}

Write-Host 'agentic-preflight is installed and up to date.'

# Compared case-insensitively and without trailing separators: PATH entries on
# Windows differ in both without meaning anything different.
$normalisedTarget = $toolBinDir.TrimEnd('\', '/').ToLowerInvariant()
$onPath = $env:PATH -split ';' | ForEach-Object { $_.TrimEnd('\', '/').ToLowerInvariant() }
if ($onPath -notcontains $normalisedTarget) {
    Write-Host "Add $toolBinDir to PATH, or run: uv tool update-shell"
}
