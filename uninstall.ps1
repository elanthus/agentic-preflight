<#
.SYNOPSIS
    Remove Agentic Preflight's managed skills and CLI on Windows.

.DESCRIPTION
    The PowerShell counterpart to uninstall.sh, with the same ordering and the
    same deliberate pause: repository configuration and managed hook logic are
    removed by the skill itself, from inside each repository, before the CLI
    that provides the skill is taken away.

.PARAMETER Agents
    Which agent integrations to remove. Defaults to every supported agent.
#>

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Agents = @()
)

$ErrorActionPreference = 'Stop'

function Find-AgenticPreflight {
    <#
        Matches install.ps1: the launcher uv writes is an .exe today, but the
        extension is uv's choice rather than a contract.
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

function Write-HookInstructions {
    Write-Host @'

To remove the pre-push hook from each repository that used agentic-preflight:

  1. Enter the repository and resolve its actual hook path:

       cd C:\path\to\repository
       $hookPath = git rev-parse --git-path hooks/pre-push

  2. Inspect the hook before changing it:

       Get-Content $hookPath -TotalCount 120

  3. If it is the standalone generated hook (it says "Installed by
     agentic-preflight" and ends with "exec agentic-preflight hook-check"), remove it:

       Remove-Item $hookPath

     If it is a shared or custom hook, do not delete the file. Edit it and remove only
     the agentic-preflight hook-check invocation and its associated wrapper logic.

Repeat these steps for every clone where `agentic-preflight init` installed a hook.
Also remove that repository's .agentic-preflight.toml if it is still present. Run
history and Git-note attestations are preserved for audit history unless removed
separately after review.
'@
}

Write-Host @'
Before uninstalling the agent skill and CLI, remove agentic-preflight from every
repository where `agentic-preflight init` was run.

For each repository, open it in your coding agent and enter this exact trigger phrase:

  agentic-preflight:uninstall

The skill will remove that repository's .agentic-preflight.toml and its managed
pre-push hook logic while preserving run history, attestations, and unrelated hooks.

Return here after doing this in every repository, then press Enter to continue.
'@

# Read directly from the console host rather than Read-Host so a piped or
# redirected stdin reaching end-of-file is a refusal to continue, not a silent
# "yes" that uninstalls the skill before any repository has been cleaned up.
if ($null -eq [Console]::In.ReadLine()) {
    Write-Error 'uninstall paused; press Enter after project cleanup to continue'
    exit 1
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error 'uv is required to uninstall agentic-preflight'
    exit 1
}

if ($Agents.Count -eq 0) {
    $Agents = @('codex', 'claude', 'cursor', 'opencode', 'amp')
}

$toolBinDir = (uv tool dir --bin).Trim()
$agenticPreflightBin = Find-AgenticPreflight $toolBinDir
if (-not $agenticPreflightBin) {
    Write-Host "agentic-preflight is not installed in $toolBinDir; no CLI or skills were changed."
    Write-HookInstructions
    exit 0
}

# $ErrorActionPreference does not govern native commands: they report failure
# through $LASTEXITCODE and the script carries on regardless. Left unchecked,
# a failed skill removal would still be followed by removing the CLI that
# performs it, and a failed CLI removal would still print success.
Write-Host "Removing managed agent skills for: $($Agents -join ' ')"
& $agenticPreflightBin integrations uninstall @Agents
if ($LASTEXITCODE -ne 0) {
    Write-Error 'removing the managed agent skills failed; the CLI was left installed so it can be retried'
    exit 1
}

Write-Host 'Uninstalling the agentic-preflight CLI'
uv tool uninstall agentic-preflight
if ($LASTEXITCODE -ne 0) {
    Write-Error 'uninstalling the agentic-preflight CLI failed'
    exit 1
}

Write-Host 'agentic-preflight has been uninstalled.'
Write-Host 'Run history and attestations were left intact.'
Write-HookInstructions
