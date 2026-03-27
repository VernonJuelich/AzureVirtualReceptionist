<#
.SYNOPSIS
    Deploys the full Azure Virtual Receptionist infrastructure.

.DESCRIPTION
    Creates all required Azure resources in dependency order:
      1.  Resource Group
      2.  Azure Communication Services
      3.  Azure Key Vault
      4.  Azure App Configuration
      5.  Storage Account (required by Function App)
      6.  Application Insights
      7.  Azure Function App (Python 3.11 / Consumption)
      8.  AD App Registration + API permissions
      9.  AD Security Group (staff directory)
      10. Key Vault secrets
      11. Role assignments (Key Vault + App Config → Function App MI)
      12. App Configuration seed values

.PARAMETER TenantId
    Azure AD Tenant ID.

.PARAMETER SubscriptionId
    Azure Subscription ID.

.PARAMETER ResourceGroup
    Resource group name. Created if not exists.

.PARAMETER Location
    Azure region. Example: australiaeast, uksouth, eastus

.PARAMETER OrgPrefix
    Short prefix for resource names. Example: gennet
    Must be lowercase letters and numbers only, max 12 chars.

.PARAMETER AcsDataLocation
    ACS data residency location. Default: Australia
    Options: Australia, UnitedStates, Europe, UnitedKingdom, Asia

.EXAMPLE
    .\Deploy-Infrastructure.ps1 `
        -TenantId       "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" `
        -SubscriptionId "yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy" `
        -ResourceGroup  "rg-virtual-receptionist" `
        -Location       "australiaeast" `
        -OrgPrefix      "gennet"
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)] [string] $TenantId,
    [Parameter(Mandatory)] [string] $SubscriptionId,
    [Parameter(Mandatory)] [string] $ResourceGroup,
    [Parameter(Mandatory)] [string] $Location,
    [Parameter(Mandatory)]
    [ValidatePattern("^[a-z0-9]{1,12}$")]
    [string] $OrgPrefix,
    [ValidateSet("Australia","UnitedStates","Europe","UnitedKingdom","Asia")]
    [string] $AcsDataLocation = "Australia"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Derived resource names ─────────────────────────────────────────
# Storage account: letters/numbers only, 3-24 chars, globally unique
$StorageSuffix   = "fnstore"
$StorageRaw      = ($OrgPrefix -replace '[^a-z0-9]','') + $StorageSuffix
$StorageAcctName = $StorageRaw.Substring(0, [Math]::Min($StorageRaw.Length, 24))

$AcsName         = "$OrgPrefix-acs-receptionist"
$KvName          = "$OrgPrefix-receptionist-kv"
$AppConfigName   = "$OrgPrefix-receptionist-config"
$FunctionAppName = "$OrgPrefix-receptionist"
$AppInsightsName = "$OrgPrefix-receptionist-ai"
$AppRegName      = "VirtualReceptionist-$OrgPrefix"
$GroupName       = "VirtualReceptionist-Staff-$OrgPrefix"

Write-Host "`n=== Azure Virtual Receptionist — Infrastructure Deployment ===" -ForegroundColor Cyan
Write-Host "Tenant:          $TenantId"
Write-Host "Subscription:    $SubscriptionId"
Write-Host "Resource Group:  $ResourceGroup"
Write-Host "Location:        $Location"
Write-Host "Org Prefix:      $OrgPrefix"
Write-Host "Storage Account: $StorageAcctName"
Write-Host "ACS Data Loc:    $AcsDataLocation`n"

# ── Login ──────────────────────────────────────────────────────────
Write-Host "[1/12] Logging in to Azure..." -ForegroundColor Yellow
az login --tenant $TenantId --output none
az account set --subscription $SubscriptionId

# ── Resource Group ─────────────────────────────────────────────────
Write-Host "[2/12] Creating resource group '$ResourceGroup'..." -ForegroundColor Yellow
az group create --name $ResourceGroup --location $Location --output none

# ── ACS ────────────────────────────────────────────────────────────
Write-Host "[3/12] Creating ACS resource '$AcsName'..." -ForegroundColor Yellow
az communication create `
    --name           $AcsName `
    --resource-group $ResourceGroup `
    --data-location  $AcsDataLocation `
    --output         none

$AcsConnString = az communication list-key `
    --name           $AcsName `
    --resource-group $ResourceGroup `
    --query          "primaryConnectionString" `
    --output         tsv

$AcsResourceId = az communication show `
    --name           $AcsName `
    --resource-group $ResourceGroup `
    --query          "id" `
    --output         tsv

Write-Host "    ACS Resource ID: $AcsResourceId" -ForegroundColor DarkGray

# ── Key Vault ──────────────────────────────────────────────────────
Write-Host "[4/12] Creating Key Vault '$KvName'..." -ForegroundColor Yellow
az keyvault create `
    --name           $KvName `
    --resource-group $ResourceGroup `
    --location       $Location `
    --sku            standard `
    --output         none

# ── App Configuration ──────────────────────────────────────────────
Write-Host "[5/12] Creating App Configuration '$AppConfigName'..." -ForegroundColor Yellow
az appconfig create `
    --name           $AppConfigName `
    --resource-group $ResourceGroup `
    --location       $Location `
    --sku            Standard `
    --output         none

$AppConfigEndpoint = az appconfig show `
    --name           $AppConfigName `
    --resource-group $ResourceGroup `
    --query          "endpoint" `
    --output         tsv

# ── Storage Account ────────────────────────────────────────────────
Write-Host "[6/12] Creating storage account '$StorageAcctName'..." -ForegroundColor Yellow
az storage account create `
    --name           $StorageAcctName `
    --resource-group $ResourceGroup `
    --location       $Location `
    --sku            Standard_LRS `
    --output         none

# ── Application Insights ───────────────────────────────────────────
Write-Host "[7/12] Creating Application Insights '$AppInsightsName'..." -ForegroundColor Yellow
az monitor app-insights component create `
    --app            $AppInsightsName `
    --resource-group $ResourceGroup `
    --location       $Location `
    --kind           web `
    --output         none

$AiConnString = az monitor app-insights component show `
    --app            $AppInsightsName `
    --resource-group $ResourceGroup `
    --query          "connectionString" `
    --output         tsv

# ── Function App ───────────────────────────────────────────────────
Write-Host "[8/12] Creating Function App '$FunctionAppName'..." -ForegroundColor Yellow
az functionapp create `
    --name                      $FunctionAppName `
    --resource-group            $ResourceGroup `
    --storage-account           $StorageAcctName `
    --consumption-plan-location $Location `
    --runtime                   python `
    --runtime-version           3.11 `
    --functions-version         4 `
    --output                    none

# Enable Managed Identity
az functionapp identity assign `
    --name           $FunctionAppName `
    --resource-group $ResourceGroup `
    --output         none

$FunctionPrincipalId = az functionapp identity show `
    --name           $FunctionAppName `
    --resource-group $ResourceGroup `
    --query          "principalId" `
    --output         tsv

# Set Application Settings (non-secret values only)
az functionapp config appsettings set `
    --name           $FunctionAppName `
    --resource-group $ResourceGroup `
    --settings `
        "AZURE_APPCONFIG_ENDPOINT=$AppConfigEndpoint" `
        "AZURE_KEYVAULT_URL=https://$KvName.vault.azure.net/" `
        "APPLICATIONINSIGHTS_CONNECTION_STRING=$AiConnString" `
        "FUNCTIONS_WORKER_RUNTIME=python" `
    --output none

# ── App Registration ───────────────────────────────────────────────
Write-Host "[9/12] Creating App Registration '$AppRegName'..." -ForegroundColor Yellow

$AppRegJson   = az ad app create `
    --display-name      $AppRegName `
    --sign-in-audience  AzureADMyOrg `
    --output            json | ConvertFrom-Json

$AppClientId = $AppRegJson.appId
$AppObjectId = $AppRegJson.id

# Create service principal
az ad sp create --id $AppClientId --output none

# Add Graph API permissions (Application type)
$GraphApi = "00000003-0000-0000-c000-000000000000"
$Perms = @(
    "bc024368-1153-4739-b217-4326f2e966d0",  # GroupMember.Read.All
    "df021288-bdef-4463-88db-98f22de89214",  # User.Read.All
    "284383ee-7f6e-4e40-a2a8-e85dcb029101",  # Calls.Initiate.All
    "f6b49018-60ab-4f12-bec5-6d2120a4f3f1",  # Calls.JoinGroupCall.All
    "9c7a330d-35b3-4aa1-963d-cb2b9f927841"   # Presence.Read.All
)

foreach ($p in $Perms) {
    az ad app permission add `
        --id              $AppObjectId `
        --api             $GraphApi `
        --api-permissions "$p=Role" `
        --output          none
}

# Create client secret (2 year expiry)
$SecretJson   = az ad app credential reset `
    --id     $AppObjectId `
    --years  2 `
    --output json | ConvertFrom-Json

$ClientSecret = $SecretJson.password
$SecretExpiry = (Get-Date).AddYears(2).ToString("yyyy-MM-dd")

Write-Host "    Client secret expires: $SecretExpiry" -ForegroundColor DarkYellow
Write-Host "    *** ADD A CALENDAR REMINDER 4 WEEKS BEFORE THIS DATE ***" -ForegroundColor Red

# ── AD Security Group ──────────────────────────────────────────────
Write-Host "[10/12] Creating AD Security Group '$GroupName'..." -ForegroundColor Yellow

$GroupJson    = az ad group create `
    --display-name  $GroupName `
    --mail-nickname ($GroupName -replace '\s','-') `
    --output        json | ConvertFrom-Json

$StaffGroupId = $GroupJson.id

# ── Key Vault secrets ──────────────────────────────────────────────
Write-Host "[11/12] Storing secrets in Key Vault and assigning roles..." -ForegroundColor Yellow

az keyvault secret set --vault-name $KvName --name "acs-connection-string" --value $AcsConnString --output none
az keyvault secret set --vault-name $KvName --name "app-client-id"         --value $AppClientId   --output none
az keyvault secret set --vault-name $KvName --name "app-client-secret"     --value $ClientSecret  --output none

# Role: Function App MI → Key Vault Secrets User
$KvScope = az keyvault show `
    --name           $KvName `
    --resource-group $ResourceGroup `
    --query          "id" --output tsv

az role assignment create `
    --role                    "Key Vault Secrets User" `
    --assignee-object-id      $FunctionPrincipalId `
    --assignee-principal-type ServicePrincipal `
    --scope                   $KvScope `
    --output                  none

# Role: Function App MI → App Configuration Data Reader
$AppConfigScope = az appconfig show `
    --name           $AppConfigName `
    --resource-group $ResourceGroup `
    --query          "id" --output tsv

az role assignment create `
    --role                    "App Configuration Data Reader" `
    --assignee-object-id      $FunctionPrincipalId `
    --assignee-principal-type ServicePrincipal `
    --scope                   $AppConfigScope `
    --output                  none

# ── Seed App Configuration ─────────────────────────────────────────
Write-Host "[12/12] Seeding App Configuration..." -ForegroundColor Yellow

& "$PSScriptRoot\Set-AppConfiguration.ps1" `
    -AppConfigName $AppConfigName `
    -ConfigFile    "$PSScriptRoot\..\config\appconfig-seed.json"

# ── Summary ────────────────────────────────────────────────────────
Write-Host "`n=== Deployment Complete ===" -ForegroundColor Green
Write-Host ""
Write-Host "Resources created:" -ForegroundColor Cyan
Write-Host "  ACS:              $AcsName"
Write-Host "  ACS Resource ID:  $AcsResourceId"
Write-Host "  Key Vault:        $KvName"
Write-Host "  App Config:       $AppConfigName"
Write-Host "  Function App:     $FunctionAppName"
Write-Host "  App Insights:     $AppInsightsName"
Write-Host "  App Reg:          $AppRegName (Client ID: $AppClientId)"
Write-Host "  Staff Group:      $GroupName (Group ID: $StaffGroupId)"
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "  1. Grant admin consent: Azure AD > App Registrations > $AppRegName > API Permissions"
Write-Host "  2. Run: .\New-TeamsResourceAccount.ps1"
Write-Host "  3. Update config\appconfig-seed.json with real values, re-run Set-AppConfiguration.ps1"
Write-Host "  4. Add staff to '$GroupName' in Azure AD"
Write-Host "  5. Add GitHub secrets (see README.md), then push to trigger deployment"
Write-Host "  6. Run: .\Test-EndToEnd.ps1"
Write-Host "  7. Run: .\Set-AlertRules.ps1"
Write-Host ""
Write-Host "SECRET EXPIRY: $SecretExpiry — set your calendar reminder NOW" -ForegroundColor Red

# Save deployment summary
$Summary = [ordered]@{
    DeployedAt        = (Get-Date -Format "o")
    OrgPrefix         = $OrgPrefix
    TenantId          = $TenantId
    SubscriptionId    = $SubscriptionId
    ResourceGroup     = $ResourceGroup
    Location          = $Location
    AcsName           = $AcsName
    AcsResourceId     = $AcsResourceId
    KeyVaultName      = $KvName
    AppConfigName     = $AppConfigName
    AppConfigEndpoint = $AppConfigEndpoint
    FunctionAppName   = $FunctionAppName
    AppInsightsName   = $AppInsightsName
    AppClientId       = $AppClientId
    AppObjectId       = $AppObjectId
    StaffGroupId      = $StaffGroupId
    SecretExpiry      = $SecretExpiry
}

$SummaryFile = "$PSScriptRoot\..\deployment-summary-$OrgPrefix.json"
$Summary | ConvertTo-Json | Out-File -FilePath $SummaryFile -Encoding utf8
Write-Host "`nDeployment summary saved to: $SummaryFile" -ForegroundColor DarkGray
Write-Host "Keep this file — you will need the values for subsequent scripts." -ForegroundColor DarkGray
