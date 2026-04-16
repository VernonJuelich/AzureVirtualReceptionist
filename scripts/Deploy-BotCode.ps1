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

if (-not (Get-Command func -ErrorAction SilentlyContinue)) {
    Write-Error "Azure Functions Core Tools not found. Install from: https://learn.microsoft.com/en-us/azure/azure-functions/functions-run-local"
}

Push-Location $BotPath

try {
    Write-Host "Deploying to Azure..." -ForegroundColor Yellow
    func azure functionapp publish $FunctionAppName --python --force

    Write-Host "`nDeployment complete." -ForegroundColor Green

    Write-Host "Running health check..." -ForegroundColor Yellow
    Start-Sleep -Seconds 20  # Wait for cold start

    # Use Kudu SCM API for key retrieval — works independently of Functions
    # runtime initialisation state, unlike az functionapp keys list.
    Write-Host "Retrieving function key via Kudu SCM API..." -ForegroundColor DarkGray

    $Creds = az functionapp deployment list-publishing-credentials `
        --name           $FunctionAppName `
        --resource-group $ResourceGroup `
        --query          "[publishingUserName, publishingPassword]" `
        --output         tsv

    $KuduUser = ($Creds -split "`n")[0].Trim()
    $KuduPass = ($Creds -split "`n")[1].Trim()

    $AuthHeader = "Basic " + [Convert]::ToBase64String(
        [Text.Encoding]::ASCII.GetBytes("${KuduUser}:${KuduPass}"))

    $MasterKeyResponse = Invoke-RestMethod `
        -Uri     "https://$FunctionAppName.scm.azurewebsites.net/api/functions/admin/masterkey" `
        -Method  Get `
        -Headers @{ Authorization = $AuthHeader }

    $FnKey = $MasterKeyResponse.masterKey

    $HealthUrl = "https://$FunctionAppName.azurewebsites.net/api/health?code=$FnKey"
    $Response  = Invoke-RestMethod -Uri $HealthUrl -Method Get -TimeoutSec 30

    if ($Response.status -eq "ok") {
        Write-Host "Health check passed." -ForegroundColor Green
        Write-Host "  Company: $($Response.company)"
    } else {
        Write-Warning "Health check returned: $($Response.status)"
    }
} finally {
    Pop-Location
}
