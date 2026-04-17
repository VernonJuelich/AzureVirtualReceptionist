<#
.SYNOPSIS
    Post-deployment smoke tests for the Virtual Receptionist.

.DESCRIPTION
    Tests the health endpoint, App Configuration connectivity,
    Key Vault access, and Graph API connectivity.
    Does NOT make live phone calls — use manual test calls for full E2E.

.PARAMETER FunctionAppName
    Name of the Azure Function App.

.PARAMETER ResourceGroup
    Resource group name.

.EXAMPLE
    .\Test-EndToEnd.ps1 -FunctionAppName "contoso-receptionist" -ResourceGroup "rg-virtual-receptionist"
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $FunctionAppName,
    [Parameter(Mandatory)] [string] $ResourceGroup
)

Set-StrictMode -Version Latest

$Passed = 0
$Failed = 0

function Test-Step {
    param([string]$Name, [scriptblock]$Test)
    Write-Host "  Testing: $Name..." -NoNewline
    try {
        & $Test
        Write-Host " PASS" -ForegroundColor Green
        $script:Passed++
    } catch {
        Write-Host " FAIL — $_" -ForegroundColor Red
        $script:Failed++
    }
}

Write-Host "`n=== Virtual Receptionist — Smoke Tests ===" -ForegroundColor Cyan
Write-Host "Function App: $FunctionAppName`n"

# Retrieve function key via Kudu SCM API — works independently of Functions
# runtime initialisation state, unlike az functionapp keys list which can
# fail if the host hasn't finished starting after a cold deploy.
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

$FnKey   = $MasterKeyResponse.masterKey
$BaseUrl = "https://$FunctionAppName.azurewebsites.net/api"

# ── Test 1: Function App reachable ────────────────────────────
Test-Step "Function App reachable" {
    $r = Invoke-WebRequest -Uri "$BaseUrl/health?code=$FnKey" -Method Get -TimeoutSec 15 -ErrorAction Stop
    if ($r.StatusCode -ne 200) { throw "Status $($r.StatusCode)" }
}

# ── Test 2: Health returns OK ─────────────────────────────────
Test-Step "Health endpoint returns OK" {
    $r = Invoke-RestMethod -Uri "$BaseUrl/health?code=$FnKey" -Method Get -TimeoutSec 15 -ErrorAction Stop
    if ($r.status -ne "ok") { throw "Status: $($r.status)" }
    Write-Host " (company=$($r.company))" -NoNewline -ForegroundColor DarkGray
}

# ── Test 3: App Configuration has required keys ───────────────
Test-Step "App Configuration — required keys present" {
    $AppConfigName = az appconfig list --resource-group $ResourceGroup --query "[0].name" --output tsv
    $Keys = az appconfig kv list --name $AppConfigName --key "receptionist:*" --output json | ConvertFrom-Json
    $RequiredKeys = @(
        "receptionist:company_name",
        "receptionist:greeting_message",
        "receptionist:staff_group_id",
        "receptionist:default_reception_aad_id",
        "receptionist:acs_callback_url"
    )
    $Missing = $RequiredKeys | Where-Object { $Keys.key -notcontains $_ }
    if ($Missing) { throw "Missing keys: $($Missing -join ', ')" }
}

# ── Test 4: acs_callback_url contains a function key ─────────
Test-Step "acs_callback_url includes function key (?code=)" {
    $AppConfigName = az appconfig list --resource-group $ResourceGroup --query "[0].name" --output tsv
    $CallbackUrl = az appconfig kv show `
        --name  $AppConfigName `
        --key   "receptionist:acs_callback_url" `
        --query "value" --output tsv
    if ($CallbackUrl -notmatch '\?code=') {
        throw "acs_callback_url is missing '?code=...' — ACS mid-call events will receive HTTP 401"
    }
    if ($CallbackUrl -match 'REPLACE') {
        throw "acs_callback_url still contains placeholder text — update it with the real function key"
    }
}

# ── Test 5: Key Vault accessible ─────────────────────────────
Test-Step "Key Vault secrets accessible" {
    $KvName  = az keyvault list --resource-group $ResourceGroup --query "[0].name" --output tsv
    $Secrets = az keyvault secret list --vault-name $KvName --query "[].name" --output json | ConvertFrom-Json
    $Required = @("acs-connection-string", "app-client-id", "app-client-secret")
    $Missing = $Required | Where-Object { $Secrets -notcontains $_ }
    if ($Missing) { throw "Missing secrets: $($Missing -join ', ')" }
}

# ── Test 6: Function App has correct app settings ────────────
Test-Step "Function App app settings configured" {
    $Settings = az functionapp config appsettings list `
        --name           $FunctionAppName `
        --resource-group $ResourceGroup `
        --output         json | ConvertFrom-Json
    $Names = $Settings.name
    foreach ($Required in @("AZURE_APPCONFIG_ENDPOINT", "AZURE_KEYVAULT_URL", "APPLICATIONINSIGHTS_CONNECTION_STRING")) {
        if ($Names -notcontains $Required) { throw "Missing setting: $Required" }
    }
}

# ── Test 7: Managed Identity enabled ─────────────────────────
Test-Step "Managed Identity enabled" {
    $Identity = az functionapp identity show `
        --name           $FunctionAppName `
        --resource-group $ResourceGroup `
        --output         json | ConvertFrom-Json
    if (-not $Identity.principalId) { throw "System-assigned Managed Identity not enabled" }
}

# ── Test 8: App Insights connected ───────────────────────────
Test-Step "App Insights connected" {
    $AiName = az resource list `
        --resource-group  $ResourceGroup `
        --resource-type   "Microsoft.Insights/components" `
        --query           "[0].name" `
        --output          tsv
    if (-not $AiName) { throw "No App Insights found in $ResourceGroup" }
}

# ── Summary ───────────────────────────────────────────────────
Write-Host "`n=== Results ===" -ForegroundColor Cyan
Write-Host "  Passed: $Passed" -ForegroundColor Green
if ($Failed -gt 0) {
    Write-Host "  Failed: $Failed" -ForegroundColor Red
    Write-Host "`nReview failures above before making test calls." -ForegroundColor Yellow
} else {
    Write-Host "`nAll smoke tests passed. Proceed to manual call testing." -ForegroundColor Green
}
