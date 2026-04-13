<#
.SYNOPSIS
    Seeds or updates Azure App Configuration values from a JSON file.

.DESCRIPTION
    Reads key-value pairs from a JSON config file and writes them to
    Azure App Configuration. Safe to re-run — existing values are updated.

    This is the PRIMARY way to change customer-facing settings:
      - Company name / greeting messages
      - Business hours per day
      - TTS voice and language
      - Fuzzy match threshold
      - Staff group ID and reception fallback

    Changes are live within 5 minutes (Function App config cache TTL).
    No redeployment required.

.PARAMETER AppConfigName
    Name of the Azure App Configuration resource.

.PARAMETER ConfigFile
    Path to the JSON file containing key-value pairs.
    Default: ..\config\appconfig-seed.json

.PARAMETER ResourceGroup
    Resource group containing the App Configuration resource.

.EXAMPLE
    # Seed from the default template
    .\Set-AppConfiguration.ps1 -AppConfigName "contoso-receptionist-config"

    # Update a single value interactively
    .\Set-AppConfiguration.ps1 -AppConfigName "contoso-receptionist-config" -Interactive
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $AppConfigName,
    [string] $ConfigFile    = "$PSScriptRoot\..\config\appconfig-seed.json",
    [string] $ResourceGroup = "",
    [switch] $Interactive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host "`n=== Azure App Configuration Update ===" -ForegroundColor Cyan
Write-Host "App Config: $AppConfigName"
Write-Host "Config file: $ConfigFile`n"

# ── Load config file ──────────────────────────────────────────
if (-not (Test-Path $ConfigFile)) {
    Write-Error "Config file not found: $ConfigFile"
    exit 1
}

$Config = Get-Content $ConfigFile -Raw | ConvertFrom-Json

# ── Write all key-value pairs ─────────────────────────────────
foreach ($prop in $Properties) {
    $Current++
    $Key   = $prop.Name
    $Value = $prop.Value

    Write-Host "  [$Current/$Total] Setting '$Key'..." -ForegroundColor DarkGray

    if ([string]::IsNullOrEmpty($Value)) {
        # Empty string must be passed as explicit empty quotes
        az appconfig kv set `
            --name      $AppConfigName `
            --key       $Key `
            --value     "" `
            --yes `
            --output    none
    } else {
        az appconfig kv set `
            --name      $AppConfigName `
            --key       $Key `
            --value     $Value `
            --yes `
            --output    none
    }
}

Write-Host "`nDone — $Total keys written to '$AppConfigName'." -ForegroundColor Green
Write-Host "Changes will be live within 5 minutes (Function App cache TTL)." -ForegroundColor DarkGray

# ── Interactive mode: update a single key ─────────────────────
if ($Interactive) {
    Write-Host "`n=== Interactive Update Mode ===" -ForegroundColor Cyan

    # Show current values
    Write-Host "`nCurrent values:" -ForegroundColor Yellow
    az appconfig kv list --name $AppConfigName --key "receptionist:*" --output table

    $Key = Read-Host "`nEnter key to update (e.g. receptionist:company_name)"
    $Val = Read-Host "Enter new value"

    az appconfig kv set `
        --name   $AppConfigName `
        --key    $Key `
        --value  $Val `
        --yes    `
        --output none

    Write-Host "Updated '$Key' = '$Val'" -ForegroundColor Green
    Write-Host "Live within 5 minutes." -ForegroundColor DarkGray
}
