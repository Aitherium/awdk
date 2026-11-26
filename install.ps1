#Requires -Version 5.1
<#
.SYNOPSIS
    Aither installer (Windows) -- thin delegator to the ONE installer door.

.DESCRIPTION
    The real installer (every profile, every mode, AitherZero provisioning) lives in
    exactly one place: https://aitherium.com/install.ps1. This file only fetches it and
    forwards every argument, so an old link to this copy keeps working and there is
    still one source of truth.

.EXAMPLE
    irm https://aitherium.com/install.ps1 | iex

.EXAMPLE
    .\install.ps1 -Profile harness

.EXAMPLE
    .\install.ps1 -Profile team -AitherZero

.NOTES
    -SkipWizard (this file's old switch) is accepted and ignored.
#>
[CmdletBinding()]
param(
    [switch]$SkipWizard,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$DoorArgs
)

$ErrorActionPreference = 'Stop'
$base = if ($env:AITHER_INSTALL_BASE) { $env:AITHER_INSTALL_BASE } else { 'https://aitherium.com' }
try {
    $door = Invoke-RestMethod "$base/install.ps1"
} catch {
    Write-Host "  [XX] could not fetch $base/install.ps1 -- $($_.Exception.Message)" -ForegroundColor Red
    if ($PSCommandPath) { exit 2 }
    throw
}

# Forward by NAME: array splatting would bind '-Profile' as a positional string.
$named = @{}
$rest = @($DoorArgs | Where-Object { $_ -ne $null })
for ($i = 0; $i -lt $rest.Count; $i++) {
    $tok = [string]$rest[$i]
    if ($tok -notmatch '^-') { continue }
    $name = $tok.TrimStart('-')
    if (($i + 1) -lt $rest.Count -and ([string]$rest[$i + 1]) -notmatch '^-') {
        $named[$name] = [string]$rest[$i + 1]; $i++
    } else {
        $named[$name] = $true
    }
}
& ([scriptblock]::Create($door)) @named
