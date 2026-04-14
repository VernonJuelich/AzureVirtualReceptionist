<#
.SYNOPSIS
    Seeds or updates Azure App Configuration values from a JSON file.
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

if (-not (Test-Path $ConfigFile)) {
    Write-Error "Config file not found: $ConfigFile"
    exit 1
}

$Config = Get-Content $ConfigFile -Raw | ConvertFrom-Json
$Properties = $Config.PSObject.Properties
$Total = $Properties.Count
$Current = 0

foreach ($prop in $Properties) {
    $Current++
    $Key   = [string]$prop.Name
    $Value = [string]$prop.Value

    Write-Host "  [$Current/$Total] Setting '$Key'..." -ForegroundColor DarkGray

    if ([string]::IsNullOrEmpty($Value)) {
        az appconfig kv set `
            --name   $AppConfigName `
            --key    $Key `
            --value  "" `
            --yes `
            --output none
    }
    else {
        az appconfig kv set `
            --name   $AppConfigName `
            --key    $Key `
            --value  $Value `
            --yes `
            --output none
    }
}

Write-Host "`nDone — $Total keys written to '$AppConfigName'." -ForegroundColor Green
Write-Host "Changes will be live within 5 minutes (Function App cache TTL)." -ForegroundColor DarkGray

if ($Interactive) {
    Write-Host "`n=== Interactive Update Mode ===" -ForegroundColor Cyan
    Write-Host "`nCurrent values:" -ForegroundColor Yellow
    az appconfig kv list --name $AppConfigName --key "receptionist:*" --output table

    $Key = Read-Host "`nEnter key to update (e.g. receptionist:company_name)"
    $Val = Read-Host "Enter new value"

    az appconfig kv set `
        --name   $AppConfigName `
        --key    $Key `
        --value  $Val `
        --yes `
        --output none

    Write-Host "Updated '$Key' = '$Val'" -ForegroundColor Green
    Write-Host "Live within 5 minutes." -ForegroundColor DarkGray
}
