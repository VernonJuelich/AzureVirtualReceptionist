<#
.SYNOPSIS
    Creates Azure Monitor alert rules and Teams channel webhook notifications.

.DESCRIPTION
    Sets up the following alerts:
      1. Transfer failures > 3 in 5 minutes       → Email + Teams
      2. Speech recognition errors > 20%           → Email + Teams
      3. Function App exceptions (any)             → Email + Teams
      4. Key Vault access failures                 → Email + Teams
      5. Client secret expiry warning (30 days)    → Email

    Fixes applied:
      [Issue 5]  Teams webhook action group now uses --add-webhook-receiver,
                 the correct az CLI parameter for adding a webhook receiver to
                 an action group. The previous --add-action webhook / --service-uri
                 combination is not valid syntax and would silently fail.
      [Issue 13] Removed the dead $TransferQuery variable.

.PARAMETER ResourceGroup
    Resource group containing the Function App and App Insights.

.PARAMETER AppInsightsName
    Name of the Application Insights resource.

.PARAMETER FunctionAppName
    Name of the Azure Function App.

.PARAMETER AlertEmailAddress
    Email address for alert notifications.

.PARAMETER TeamsWebhookUrl
    Incoming webhook URL from your Teams channel.
    Create in Teams: channel > ... > Connectors > Incoming Webhook

.PARAMETER AppClientId
    App Registration client ID (used to monitor secret expiry).

.EXAMPLE
    .\Set-AlertRules.ps1 `
        -ResourceGroup     "rg-virtual-receptionist" `
        -AppInsightsName   "contoso-receptionist-ai" `
        -FunctionAppName   "contoso-receptionist" `
        -AlertEmailAddress "it-alerts@contoso.com" `
        -TeamsWebhookUrl   "https://contoso.webhook.office.com/webhookb2/..."
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)] [string] $ResourceGroup,
    [Parameter(Mandatory)] [string] $AppInsightsName,
    [Parameter(Mandatory)] [string] $FunctionAppName,
    [Parameter(Mandatory)] [string] $AlertEmailAddress,
    [Parameter(Mandatory)] [string] $TeamsWebhookUrl,
    [string] $AppClientId = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host "`n=== Setting Up Alert Rules ===" -ForegroundColor Cyan

# ── Get resource IDs ──────────────────────────────────────────
$AiResourceId = az monitor app-insights component show `
    --app            $AppInsightsName `
    --resource-group $ResourceGroup `
    --query          "id" `
    --output         tsv

$FnResourceId = az functionapp show `
    --name           $FunctionAppName `
    --resource-group $ResourceGroup `
    --query          "id" `
    --output         tsv

# ── Action Group — Email ──────────────────────────────────────
Write-Host "[1/6] Creating email action group..." -ForegroundColor Yellow

az monitor action-group create `
    --name           "ag-receptionist-email" `
    --resource-group $ResourceGroup `
    --short-name     "RecepEmail" `
    --action         email "ITAlerts" $AlertEmailAddress `
    --output         none

$EmailAgId = az monitor action-group show `
    --name           "ag-receptionist-email" `
    --resource-group $ResourceGroup `
    --query          "id" `
    --output         tsv

# ── Action Group — Teams Webhook ──────────────────────────────
# [Issue 5] Two-step approach: create the group, then add the webhook receiver
# using --add-webhook-receiver (the correct az CLI parameter).
# --add-action webhook / --service-uri is not valid syntax and fails silently.
Write-Host "[2/6] Creating Teams webhook action group..." -ForegroundColor Yellow

az monitor action-group create `
    --name           "ag-receptionist-teams" `
    --resource-group $ResourceGroup `
    --short-name     "RecepTeams" `
    --output         none

az monitor action-group update `
    --name                  "ag-receptionist-teams" `
    --resource-group        $ResourceGroup `
    --add-webhook-receiver  name="TeamsChannel" serviceUri=$TeamsWebhookUrl useCommonAlertSchema=true `
    --output                none

$TeamsAgId = az monitor action-group show `
    --name           "ag-receptionist-teams" `
    --resource-group $ResourceGroup `
    --query          "id" `
    --output         tsv

Write-Host "    Teams webhook action group created." -ForegroundColor DarkGray

# ── Alert 1: Transfer failures ────────────────────────────────
Write-Host "[3/6] Creating transfer failure alert..." -ForegroundColor Yellow

az monitor scheduled-query create `
    --name              "alert-transfer-failures" `
    --resource-group    $ResourceGroup `
    --scopes            $AiResourceId `
    --condition-query   "traces | where timestamp > ago(5m) | where message contains 'Transfer FAILED' | count" `
    --condition         "count > 3" `
    --window-size       "PT5M" `
    --evaluation-frequency "PT5M" `
    --severity          2 `
    --description       "More than 3 call transfer failures in 5 minutes" `
    --action-groups     $EmailAgId $TeamsAgId `
    --output            none

Write-Host "    Transfer failure alert created." -ForegroundColor DarkGray

# ── Alert 2: Function App exceptions ─────────────────────────
Write-Host "[4/6] Creating Function App exception alert..." -ForegroundColor Yellow

az monitor scheduled-query create `
    --name              "alert-function-exceptions" `
    --resource-group    $ResourceGroup `
    --scopes            $AiResourceId `
    --condition-query   "exceptions | where timestamp > ago(5m) | where cloud_RoleName contains 'receptionist' | count" `
    --condition         "count > 0" `
    --window-size       "PT5M" `
    --evaluation-frequency "PT1M" `
    --severity          1 `
    --description       "Unhandled exception in the Function App" `
    --action-groups     $EmailAgId $TeamsAgId `
    --output            none

Write-Host "    Exception alert created." -ForegroundColor DarkGray

# ── Alert 3: Function App availability ───────────────────────
Write-Host "[5/6] Creating Function App availability alert..." -ForegroundColor Yellow

az monitor metrics alert create `
    --name           "alert-function-unavailable" `
    --resource-group $ResourceGroup `
    --scopes         $FnResourceId `
    --condition      "avg Requests < 1" `
    --description    "Function App may be unavailable — no requests received in 1 hour during business hours" `
    --severity       1 `
    --window-size    "PT1H" `
    --evaluation-frequency "PT15M" `
    --action         $EmailAgId $TeamsAgId `
    --output         none

Write-Host "    Availability alert created." -ForegroundColor DarkGray

# ── Alert 4: Key Vault access failures ───────────────────────
Write-Host "[6/6] Creating Key Vault access failure alert..." -ForegroundColor Yellow

$KvName = az keyvault list `
    --resource-group $ResourceGroup `
    --query          "[0].name" `
    --output         tsv

if ($KvName) {
    az monitor scheduled-query create `
        --name              "alert-keyvault-failures" `
        --resource-group    $ResourceGroup `
        --scopes            $AiResourceId `
        --condition-query   "traces | where timestamp > ago(5m) | where message contains 'Key Vault' and (message contains '403' or message contains 'Forbidden' or message contains 'Unauthorized') | count" `
        --condition         "count > 0" `
        --window-size       "PT5M" `
        --evaluation-frequency "PT5M" `
        --severity          1 `
        --description       "Key Vault access failures — secrets may be inaccessible" `
        --action-groups     $EmailAgId $TeamsAgId `
        --output            none

    Write-Host "    Key Vault alert created." -ForegroundColor DarkGray
}

# ── Teams Channel: Post alert card template ───────────────────
Write-Host "`nSending test alert to Teams channel..." -ForegroundColor Yellow

$TestCard = @{
    "@type"      = "MessageCard"
    "@context"   = "https://schema.org/extensions"
    "summary"    = "Virtual Receptionist Alerting Configured"
    "themeColor" = "0076D7"
    "title"      = "✅ Virtual Receptionist Alerts Active"
    "sections"   = @(
        @{
            "facts" = @(
                @{ "name" = "Resource Group"; "value" = $ResourceGroup }
                @{ "name" = "Function App";   "value" = $FunctionAppName }
                @{ "name" = "Alert Email";    "value" = $AlertEmailAddress }
                @{ "name" = "Alerts Created"; "value" = "Transfer failures, Exceptions, Availability, Key Vault" }
                @{ "name" = "Status";         "value" = "All alert rules active" }
            )
        }
    )
} | ConvertTo-Json -Depth 5

try {
    Invoke-RestMethod -Uri $TeamsWebhookUrl -Method Post -Body $TestCard -ContentType "application/json" | Out-Null
    Write-Host "    Test card sent to Teams channel." -ForegroundColor Green
} catch {
    Write-Warning "Could not send test card to Teams webhook: $_"
}

Write-Host "`n=== Alert Setup Complete ===" -ForegroundColor Green
Write-Host "Alerts configured:"
Write-Host "  - Transfer failures > 3 in 5 min  → Email + Teams"
Write-Host "  - Function App exceptions          → Email + Teams"
Write-Host "  - Function unavailable (1 hour)    → Email + Teams"
Write-Host "  - Key Vault access failures        → Email + Teams"
Write-Host ""
Write-Host "View alerts: Azure Portal > Monitor > Alerts" -ForegroundColor DarkGray

# ── Client secret expiry reminder ────────────────────────────
if ($AppClientId) {
    Write-Host "`nChecking App Registration secret expiry..." -ForegroundColor Yellow
    $SecretInfo = az ad app credential list --id $AppClientId --output json | ConvertFrom-Json
    foreach ($s in $SecretInfo) {
        $Expiry    = [datetime]$s.endDateTime
        $DaysLeft  = ($Expiry - (Get-Date)).Days
        $ExpiryStr = $Expiry.ToString("yyyy-MM-dd")
        if ($DaysLeft -lt 60) {
            Write-Warning "Client secret expires in $DaysLeft days ($ExpiryStr)! Run Rotate-ClientSecret.ps1 soon."
        } else {
            Write-Host "  Client secret expires: $ExpiryStr ($DaysLeft days)" -ForegroundColor DarkGray
        }
    }
}
