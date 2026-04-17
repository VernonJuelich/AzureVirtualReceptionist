<#
.SYNOPSIS
    Rotates the Azure AD App Registration client secret and updates Key Vault.

.DESCRIPTION
    Lifecycle management script for the App Registration client secret.
    Run this every 24 months (before the current secret expires).

    Steps:
      1. Lists existing secrets and records key IDs
      2. Creates a new client secret (--append keeps old one valid during transition)
      3. Updates Key Vault via temp file — secret never appears in shell history
      4. Verifies the Function App health endpoint (no restart required — the bot
         reads the secret from Key Vault at call time with no caching)
      5. Removes the old secret from the App Registration
      6. Sends a confirmation to Teams (optional)

.PARAMETER AppObjectId
    The App Registration Object ID (NOT client ID).
    Found at: Azure AD > App Registrations > select app > Object ID

.PARAMETER KeyVaultName
    Name of the Key Vault to update.

.PARAMETER FunctionAppName
    Function App name — used for the health check verification step.

.PARAMETER ResourceGroup
    Resource group name.

.PARAMETER TeamsWebhookUrl
    Optional Power Automate workflow webhook URL to notify on completion.

.EXAMPLE
    .\Rotate-ClientSecret.ps1 `
        -AppObjectId      "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" `
        -KeyVaultName     "contoso-receptionist-kv" `
        -FunctionAppName  "contoso-receptionist" `
        -ResourceGroup    "rg-virtual-receptionist"
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)] [string] $AppObjectId,
    [Parameter(Mandatory)] [string] $KeyVaultName,
    [Parameter(Mandatory)] [string] $FunctionAppName,
    [Parameter(Mandatory)] [string] $ResourceGroup,
    [string] $TeamsWebhookUrl = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host "`n=== Client Secret Rotation ===" -ForegroundColor Cyan
Write-Host "App Object ID:  $AppObjectId"
Write-Host "Key Vault:      $KeyVaultName"
Write-Host "Function App:   $FunctionAppName`n"

# ── Step 1: List current secrets ─────────────────────────────
Write-Host "[1/5] Checking existing secrets..." -ForegroundColor Yellow
$Existing = az ad app credential list --id $AppObjectId --output json | ConvertFrom-Json

foreach ($s in $Existing) {
    $Expiry   = [datetime]$s.endDateTime
    $DaysLeft = ($Expiry - (Get-Date)).Days
    Write-Host "    Existing secret: keyId=$($s.keyId) expires=$($Expiry.ToString('yyyy-MM-dd')) ($DaysLeft days)"
}

$OldKeyId = $Existing | Sort-Object endDateTime | Select-Object -Last 1 |
    Select-Object -ExpandProperty keyId

# ── Step 2: Create new secret ─────────────────────────────────
Write-Host "`n[2/5] Creating new client secret (24 month expiry)..." -ForegroundColor Yellow
Write-Host "    Using --append so old secret remains valid until Step 5." -ForegroundColor DarkGray

$NewSecretValue  = $null
$NewSecretExpiry = $null

if ($PSCmdlet.ShouldProcess($AppObjectId, "az ad app credential reset --append")) {
    $NewSecret = az ad app credential reset `
        --id     $AppObjectId `
        --years  2 `
        --append `
        --output json | ConvertFrom-Json

    $NewSecretValue  = $NewSecret.password
    $NewSecretExpiry = (Get-Date).AddYears(2).ToString("yyyy-MM-dd")
    Write-Host "    New secret created. Expires: $NewSecretExpiry" -ForegroundColor Green
}

# ── Step 3: Update Key Vault ──────────────────────────────────
# Write secret to temp file so value never appears in shell history or
# process listings. Temp file is deleted immediately after the update.
Write-Host "[3/5] Updating Key Vault secret 'app-client-secret'..." -ForegroundColor Yellow

if ($PSCmdlet.ShouldProcess($KeyVaultName, "az keyvault secret set")) {
    $TempFile = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllText($TempFile, $NewSecretValue)
        az keyvault secret set `
            --vault-name $KeyVaultName `
            --name       "app-client-secret" `
            --file       $TempFile `
            --output     none
        Write-Host "    Key Vault updated." -ForegroundColor Green
    } finally {
        Remove-Item -Path $TempFile -Force -ErrorAction SilentlyContinue
    }
    $NewSecretValue = $null  # Clear from memory
}

# ── Step 4: Verify health endpoint ───────────────────────────
# The bot reads the client secret from Key Vault at call time (no caching),
# so no Function App restart is needed. A restart would cause unnecessary
# downtime for any calls in progress.
# Use Kudu SCM API for key retrieval — works independently of runtime state.
Write-Host "[4/5] Verifying health endpoint (no restart required)..." -ForegroundColor Yellow

try {
    $Creds = az functionapp deployment list-publishing-credentials `
        --name           $FunctionAppName `
        --resource-group $ResourceGroup `
        --query          "[publishingUserName, publishingPassword]" `
        --output         tsv

    $KuduUser = ($Creds -split "`n")[0].Trim()
    $KuduPass = ($Creds -split "`n")[1].Trim()
    $AuthHeader = "Basic " + [Convert]::ToBase64String(
        [Text.Encoding]::ASCII.GetBytes("${KuduUser}:${KuduPass}"))

    $MasterKeyJson = Invoke-RestMethod `
        -Uri     "https://$FunctionAppName.scm.azurewebsites.net/api/functions/admin/masterkey" `
        -Method  Get `
        -Headers @{ Authorization = $AuthHeader }

    $FnKey     = $MasterKeyJson.masterKey
    $HealthUrl = "https://$FunctionAppName.azurewebsites.net/api/health?code=$FnKey"
    $Response  = Invoke-RestMethod -Uri $HealthUrl -Method Get -TimeoutSec 30

    if ($Response.status -eq "ok") {
        Write-Host "    Health check passed. Company: $($Response.company)" -ForegroundColor Green
    } else {
        Write-Warning "Health check returned unexpected status: $($Response.status)"
    }
} catch {
    Write-Warning "Health check failed: $_ — verify manually at /api/health"
    Write-Warning "Key Vault has been updated. If health fails, check Managed Identity role assignments."
}

# ── Step 5: Remove old secret ─────────────────────────────────
if ($OldKeyId -and $PSCmdlet.ShouldProcess($OldKeyId, "Remove old client secret")) {
    Write-Host "[5/5] Removing old secret (keyId=$OldKeyId)..." -ForegroundColor Yellow
    az ad app credential delete --id $AppObjectId --key-id $OldKeyId --output none
    Write-Host "    Old secret removed." -ForegroundColor Green
} else {
    Write-Host "[5/5] No old secret to remove (or -WhatIf)." -ForegroundColor DarkGray
}

# ── Notify Teams ──────────────────────────────────────────────
# Uses Power Automate webhook JSON payload — not deprecated MessageCard format.
if ($TeamsWebhookUrl) {
    $Payload = @{
        status          = "Secret rotated"
        app             = $FunctionAppName
        new_expiry      = $NewSecretExpiry
        rotated_by      = $env:USERNAME
        rotated_at      = (Get-Date -Format "yyyy-MM-dd HH:mm UTC")
        next_rotation   = (Get-Date).AddMonths(22).ToString("yyyy-MM")
    } | ConvertTo-Json

    try {
        Invoke-RestMethod -Uri $TeamsWebhookUrl -Method Post `
            -Body $Payload -ContentType "application/json" | Out-Null
        Write-Host "Teams notification sent." -ForegroundColor DarkGray
    } catch {
        Write-Warning "Could not send Teams notification: $_"
    }
}

Write-Host "`n=== Secret Rotation Complete ===" -ForegroundColor Green
Write-Host "New secret expiry: $NewSecretExpiry"
Write-Host "IMPORTANT: Add a calendar reminder for $(((Get-Date).AddMonths(22)).ToString('MMMM yyyy')) to rotate again." -ForegroundColor Yellow
