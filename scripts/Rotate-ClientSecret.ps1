<#
.SYNOPSIS
    Rotates the Azure AD App Registration client secret and updates Key Vault.

.DESCRIPTION
    Lifecycle management script for the App Registration client secret.
    Run this every 24 months (before the current secret expires).

    Steps:
      1. Creates a new client secret on the App Registration
      2. Updates the secret in Key Vault
      3. Verifies the Function App can still reach Key Vault
      4. Removes the old secret from the App Registration
      5. Sends a confirmation to the Teams channel

    Fixes applied:
      [Issue 10] The new client secret is now written to a temporary file and
                 passed to Key Vault via --file, preventing the secret value
                 from appearing in shell history or process listings. The temp
                 file is deleted immediately after the Key Vault update.

.PARAMETER AppObjectId
    The App Registration Object ID (NOT client ID).
    Found at: Azure AD > App Registrations > select app > Object ID

.PARAMETER KeyVaultName
    Name of the Key Vault to update.

.PARAMETER FunctionAppName
    Function App name — used to restart and verify after rotation.

.PARAMETER ResourceGroup
    Resource group name.

.PARAMETER TeamsWebhookUrl
    Optional Teams channel webhook to notify on completion.

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

# ── List current secrets ──────────────────────────────────────
Write-Host "[1/5] Checking existing secrets..." -ForegroundColor Yellow
$Existing = az ad app credential list --id $AppObjectId --output json | ConvertFrom-Json

foreach ($s in $Existing) {
    $Expiry   = [datetime]$s.endDateTime
    $DaysLeft = ($Expiry - (Get-Date)).Days
    Write-Host "    Existing secret: key=$($s.keyId) expires=$($Expiry.ToString('yyyy-MM-dd')) ($DaysLeft days)"
}

$OldKeyId = $Existing | Sort-Object endDateTime | Select-Object -Last 1 | Select-Object -ExpandProperty keyId

# ── Create new secret ─────────────────────────────────────────
Write-Host "`n[2/5] Creating new client secret (24 month expiry)..." -ForegroundColor Yellow

$NewSecretValue  = $null
$NewSecretExpiry = $null

if ($PSCmdlet.ShouldProcess($AppObjectId, "az ad app credential reset")) {
    $NewSecret = az ad app credential reset `
        --id     $AppObjectId `
        --years  2 `
        --append `
        --output json | ConvertFrom-Json

    $NewSecretValue  = $NewSecret.password
    $NewSecretExpiry = (Get-Date).AddYears(2).ToString("yyyy-MM-dd")
    Write-Host "    New secret created. Expires: $NewSecretExpiry" -ForegroundColor Green
}

# ── Update Key Vault ──────────────────────────────────────────
# [Issue 10] Write secret to a temp file and use --file to keep the value out
# of shell history and process listings. Temp file is deleted immediately.
Write-Host "[3/5] Updating Key Vault secret 'app-client-secret'..." -ForegroundColor Yellow

if ($PSCmdlet.ShouldProcess($KeyVaultName, "az keyvault secret set")) {
    $TempFile = [System.IO.Path]::GetTempFileName()
    try {
        # Write without trailing newline to avoid corrupting the secret value
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

    # Clear from memory as soon as it's stored
    $NewSecretValue = $null
}

# ── Restart Function App to pick up new secret ───────────────
Write-Host "[4/5] Restarting Function App..." -ForegroundColor Yellow

if ($PSCmdlet.ShouldProcess($FunctionAppName, "az functionapp restart")) {
    az functionapp restart `
        --name           $FunctionAppName `
        --resource-group $ResourceGroup `
        --output         none

    Write-Host "    Function App restarted." -ForegroundColor Green
    Write-Host "    Waiting 60 seconds for startup..." -ForegroundColor DarkGray
    Start-Sleep -Seconds 60
}

# ── Verify health endpoint ────────────────────────────────────
Write-Host "[5/5] Verifying health endpoint..." -ForegroundColor Yellow

try {
    $FnKey = az functionapp keys list `
        --name           $FunctionAppName `
        --resource-group $ResourceGroup `
        --query          "functionKeys.default" `
        --output         tsv

    $HealthUrl = "https://$FunctionAppName.azurewebsites.net/api/health?code=$FnKey"
    $Response  = Invoke-RestMethod -Uri $HealthUrl -Method Get -TimeoutSec 30

    if ($Response.status -eq "ok") {
        Write-Host "    Health check passed. Company: $($Response.company)" -ForegroundColor Green
    } else {
        Write-Warning "Health check returned unexpected status: $($Response.status)"
    }
} catch {
    Write-Warning "Health check failed: $_ — verify manually at /api/health"
}

# ── Remove old secret ─────────────────────────────────────────
if ($OldKeyId -and $PSCmdlet.ShouldProcess($OldKeyId, "Remove old client secret")) {
    Write-Host "`nRemoving old secret (keyId=$OldKeyId)..." -ForegroundColor Yellow
    az ad app credential delete --id $AppObjectId --key-id $OldKeyId --output none
    Write-Host "    Old secret removed." -ForegroundColor Green
}

# ── Notify Teams ──────────────────────────────────────────────
if ($TeamsWebhookUrl) {
    $Card = @{
        "@type"    = "MessageCard"
        "@context" = "https://schema.org/extensions"
        "summary"  = "Secret Rotation Complete"
        "themeColor" = "00B050"
        "title"    = "🔑 Client Secret Rotated Successfully"
        "sections" = @(
            @{
                "facts" = @(
                    @{ "name" = "Function App";      "value" = $FunctionAppName }
                    @{ "name" = "New Expiry Date";   "value" = $NewSecretExpiry }
                    @{ "name" = "Rotated By";        "value" = $env:USERNAME }
                    @{ "name" = "Rotated At";        "value" = (Get-Date -Format "yyyy-MM-dd HH:mm UTC") }
                    @{ "name" = "Next Rotation Due"; "value" = (Get-Date).AddMonths(22).ToString("yyyy-MM") }
                )
            }
        )
    } | ConvertTo-Json -Depth 5

    Invoke-RestMethod -Uri $TeamsWebhookUrl -Method Post -Body $Card -ContentType "application/json" | Out-Null
    Write-Host "Teams notification sent." -ForegroundColor DarkGray
}

Write-Host "`n=== Secret Rotation Complete ===" -ForegroundColor Green
Write-Host "New secret expiry: $NewSecretExpiry"
Write-Host "IMPORTANT: Add a calendar reminder for $(((Get-Date).AddMonths(22)).ToString('MMMM yyyy')) to rotate again." -ForegroundColor Yellow
