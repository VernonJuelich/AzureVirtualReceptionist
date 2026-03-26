<#
.SYNOPSIS
    Creates and configures a Teams Resource Account linked to an ACS resource.

.DESCRIPTION
    Uses Teams PowerShell module to:
      1. Create the resource account (New-CsOnlineApplicationInstance)
      2. Set the ACS Resource ID on it (Set-CsOnlineApplicationInstance)
      3. Sync provisioning (Sync-CsOnlineApplicationInstance)
      4. Assign the phone number (Set-CsPhoneNumberAssignment)

    IMPORTANT: This CANNOT be done via Teams Admin Center UI.
    Requires the Microsoft Teams PowerShell module and Global Admin rights.

.PARAMETER UPN
    UPN for the resource account. Example: reception@contoso.com

.PARAMETER DisplayName
    Display name shown in Teams. Example: "Virtual Receptionist"

.PARAMETER AppId
    The App Registration Application (client) ID.

.PARAMETER AcsResourceId
    The ACS Immutable Resource ID.
    Found at: ACS resource > Settings > Properties > Immutable Resource Id
    Format: /subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Communication/communicationServices/{name}

.PARAMETER PhoneNumber
    The E.164 format phone number. Example: +61299999999

.PARAMETER PhoneNumberType
    DirectRouting or CallingPlan. Default: DirectRouting

.EXAMPLE
    .\New-TeamsResourceAccount.ps1 `
        -UPN           "reception@contoso.com" `
        -DisplayName   "Virtual Receptionist" `
        -AppId         "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" `
        -AcsResourceId "/subscriptions/.../communicationServices/contoso-acs" `
        -PhoneNumber   "+61299999999"
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)] [string] $UPN,
    [Parameter(Mandatory)] [string] $DisplayName,
    [Parameter(Mandatory)] [string] $AppId,
    [Parameter(Mandatory)] [string] $AcsResourceId,
    [Parameter(Mandatory)] [string] $PhoneNumber,
    [string] $PhoneNumberType = "DirectRouting"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Check Teams module ────────────────────────────────────────
Write-Host "`n=== Teams Resource Account Setup ===" -ForegroundColor Cyan

if (-not (Get-Module -ListAvailable -Name MicrosoftTeams)) {
    Write-Host "Installing MicrosoftTeams PowerShell module..." -ForegroundColor Yellow
    Install-Module MicrosoftTeams -Force -AllowClobber -Scope CurrentUser
}

Import-Module MicrosoftTeams

# ── Connect ───────────────────────────────────────────────────
Write-Host "Connecting to Microsoft Teams (Global Admin required)..." -ForegroundColor Yellow
Connect-MicrosoftTeams

# ── Step 1: Create resource account ──────────────────────────
Write-Host "`n[1/5] Creating resource account '$UPN'..." -ForegroundColor Yellow

# Check if it already exists
$Existing = Get-CsOnlineApplicationInstance -Identity $UPN -ErrorAction SilentlyContinue
if ($Existing) {
    Write-Host "    Resource account already exists — updating." -ForegroundColor DarkYellow
} else {
    if ($PSCmdlet.ShouldProcess($UPN, "New-CsOnlineApplicationInstance")) {
        New-CsOnlineApplicationInstance `
            -UserPrincipalName $UPN `
            -ApplicationId     $AppId `
            -DisplayName       $DisplayName
    }
}

Start-Sleep -Seconds 5   # Allow provisioning to settle

# ── Step 2: Set ACS Resource ID ───────────────────────────────
Write-Host "[2/5] Setting ACS Resource ID on resource account..." -ForegroundColor Yellow
Write-Host "    ACS Resource ID: $AcsResourceId"

if ($PSCmdlet.ShouldProcess($UPN, "Set-CsOnlineApplicationInstance -AcsResourceId")) {
    Set-CsOnlineApplicationInstance `
        -Identity      $UPN `
        -ApplicationId $AppId `
        -AcsResourceId $AcsResourceId
}

Start-Sleep -Seconds 3

# ── Step 3: Sync provisioning ─────────────────────────────────
Write-Host "[3/5] Syncing provisioning..." -ForegroundColor Yellow

$Instance = Get-CsOnlineApplicationInstance -Identity $UPN
$ObjectId  = $Instance.ObjectId

Write-Host "    Object ID: $ObjectId"

if ($PSCmdlet.ShouldProcess($ObjectId, "Sync-CsOnlineApplicationInstance")) {
    Sync-CsOnlineApplicationInstance `
        -ObjectId      $ObjectId `
        -ApplicationId $AppId
}

Start-Sleep -Seconds 10  # Sync takes a few seconds

# ── Step 4: Assign phone number ───────────────────────────────
Write-Host "[4/5] Assigning phone number $PhoneNumber ($PhoneNumberType)..." -ForegroundColor Yellow

if ($PSCmdlet.ShouldProcess($UPN, "Set-CsPhoneNumberAssignment")) {
    Set-CsPhoneNumberAssignment `
        -Identity        $UPN `
        -PhoneNumber     $PhoneNumber `
        -PhoneNumberType $PhoneNumberType
}

# ── Step 5: Verify ────────────────────────────────────────────
Write-Host "[5/5] Verifying configuration..." -ForegroundColor Yellow
Start-Sleep -Seconds 5

$Final = Get-CsOnlineApplicationInstance -Identity $UPN

Write-Host "`n=== Verification ===" -ForegroundColor Green
Write-Host "UPN:           $($Final.UserPrincipalName)"
Write-Host "Display Name:  $($Final.DisplayName)"
Write-Host "Application ID:$($Final.ApplicationId)"
Write-Host "ACS Resource:  $($Final.AcsResourceId)"
Write-Host "Phone Number:  $($Final.PhoneNumber)"

# Validation checks
$AllGood = $true

if ($Final.ApplicationId -ne $AppId) {
    Write-Warning "ApplicationId mismatch! Expected: $AppId | Got: $($Final.ApplicationId)"
    $AllGood = $false
}

if ([string]::IsNullOrEmpty($Final.AcsResourceId)) {
    Write-Warning "AcsResourceId is empty. Wait 2-3 minutes and re-run Get-CsOnlineApplicationInstance to verify."
    $AllGood = $false
}

if ($AllGood) {
    Write-Host "`nAll values confirmed. Teams resource account is correctly linked to ACS." -ForegroundColor Green
} else {
    Write-Host "`nSome values need verification — see warnings above." -ForegroundColor Red
    Write-Host "If AcsResourceId is blank, wait 5 minutes and run:"
    Write-Host "  Get-CsOnlineApplicationInstance -Identity '$UPN' | Format-List"
}

Write-Host "`nReminder: Assign the 'Microsoft Teams Phone Resource Account' (free) license" -ForegroundColor Yellow
Write-Host "  Microsoft 365 Admin Center > Users > Active Users > $UPN > Licenses"
