<#
.SYNOPSIS
    Deploys the Python bot code to Azure Function App.
    Run after infrastructure is deployed, or for manual code updates.

.PARAMETER FunctionAppName
    Name of the Azure Function App.

.PARAMETER ResourceGroup
    Resource group name.

.PARAMETER BotPath
    Path to the bot folder. Default: .\bot (relative to repo root)

.EXAMPLE
    .\Deploy-BotCode.ps1 -FunctionAppName "contoso-receptionist" -ResourceGroup "rg-virtual-receptionist"
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $FunctionAppName,
    [Parameter(Mandatory)] [string] $ResourceGroup,
    [string] $BotPath = "$PSScriptRoot\..\bot"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host "`n=== Deploying Bot Code ===" -ForegroundColor Cyan
Write-Host "Function App: $FunctionAppName"
Write-Host "Bot Path:     $BotPath`n"

# Check Azure Functions Core Tools
if (-not (Get-Command func -ErrorAction SilentlyContinue)) {
    Write-Error "Azure Functions Core Tools not found. Install from: https://learn.microsoft.com/en-us/azure/azure-functions/functions-run-local"
}

Push-Location $BotPath

try {
    Write-Host "Deploying to Azure..." -ForegroundColor Yellow
    func azure functionapp publish $FunctionAppName --python --force

    Write-Host "`nDeployment complete." -ForegroundColor Green

    # Quick health check
    Write-Host "Running health check..." -ForegroundColor Yellow
    Start-Sleep -Seconds 20  # Wait for cold start

    $FnKey = az functionapp keys list `
        --name           $FunctionAppName `
        --resource-group $ResourceGroup `
        --query          "functionKeys.default" `
        --output         tsv

    $HealthUrl = "https://$FunctionAppName.azurewebsites.net/api/health?code=$FnKey"
    $Response  = Invoke-RestMethod -Uri $HealthUrl -Method Get -TimeoutSec 30

    if ($Response.status -eq "ok") {
        Write-Host "Health check passed." -ForegroundColor Green
        Write-Host "  Company:  $($Response.company)"
        Write-Host "  Timezone: $($Response.timezone)"
        Write-Host "  Voice:    $($Response.voice)"
    } else {
        Write-Warning "Health check returned: $($Response.status)"
    }
} finally {
    Pop-Location
}
