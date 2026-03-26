<#
.SYNOPSIS
    Deploys the full Azure Virtual Receptionist infrastructure into a customer tenant.

.DESCRIPTION
    Creates all required Azure resources in the correct order:
      1. Resource Group
      2. Azure Communication Services
      3. Azure Key Vault
      4. Azure App Configuration
      5. Azure Function App (Python 3.11 / Consumption)
      6. Application Insights
      7. AD App Registration + API permissions
      8. AD Security Group (staff directory)
      9. Key Vault secrets (ACS connection string, App Registration credentials)
     10. App Configuration — seeds default values from appconfig-seed.json
     11. Managed Identity → Key Vault role assignment

.PARAMETER TenantId
    Azure AD Tenant ID of the customer.

.PARAMETER SubscriptionId
    Azure Subscription ID to deploy into.

.PARAMETER ResourceGroup
    Resource group name. Created if it does not exist.

.PARAMETER Location
    Azure region. Example: australiaeast, uksouth, eastus

.PARAMETER OrgPrefix
    Short prefix used to name all resources. Example: contoso

.EXAMPLE
    .\Deploy-Infrastructure.ps1 `
        -TenantId       "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" `
        -SubscriptionId "yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy" `
        -ResourceGroup  "rg-virtual-receptionist" `
        -Location       "australiaeast" `
        -OrgPrefix      "contoso"
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)] [string] $TenantId,
    [Parameter(Mandatory)] [string] $SubscriptionId,
    [Parameter(Mandatory)] [string] $ResourceGroup,
    [Parameter(Mandatory)] [string] $Location,
    [Parameter(Mandatory)] [string] $OrgPrefix
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Derived resource names ────────────────────────────────────
$AcsName          = "$OrgPrefix-acs-receptionist"
$KvName           = "$OrgPrefix-receptionist-kv"
$AppConfigName    = "$OrgPrefix-receptionist-config"
$FunctionAppName  = "$OrgPrefix-receptionist"
$StorageAcctName  = ($OrgPrefix -replace '-','') + "fnstore"   # storage for Function App
$AppInsightsName  = "$OrgPrefix-receptionist-ai"
$AppRegName       = "VirtualReceptionist-$OrgPrefix"
$GroupName        = "VirtualReceptionist-Staff-$OrgPrefix"

Write-Host "`n=== Azure Virtual Receptionist — Infrastructure Deployment ===" -ForegroundColor Cyan
Write-Host "Tenant:         $TenantId"
Write-Host "Subscription:   $SubscriptionId"
Write-Host "Resource Group: $ResourceGroup"
Write-Host "Location:       $Location"
Write-Host "Org Prefix:     $OrgPrefix`n"

# ── Login & set subscription ──────────────────────────────────
Write-Host "[1/11] Logging in to Azure..." -ForegroundColor Yellow
az login --tenant $TenantId --output none
az account set --subscription $SubscriptionId

# ── Resource Group ────────────────────────────────────────────
Write-Host "[2/11] Creating resource group '$ResourceGroup'..." -ForegroundColor Yellow
az group create `
    --name     $ResourceGroup `
    --location $Location `
    --output   none

# ── Azure Communication Services ─────────────────────────────
Write-Host "[3/11] Creating ACS resource '$AcsName'..." -ForegroundColor Yellow
az communication create `
    --name           $AcsName `
    --resource-group $ResourceGroup `
    --data-location  "Australia" `
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

# ── Key Vault ─────────────────────────────────────────────────
Write-Host "[4/11] Creating Key Vault '$KvName'..." -ForegroundColor Yellow
az keyvault create `
    --name           $KvName `
    --resource-group $ResourceGroup `
    --location       $Location `
    --sku            standard `
    --output         none

# ── App Configuration ─────────────────────────────────────────
Write-Host "[5/11] Creating App Configuration '$AppConfigName'..." -ForegroundColor Yellow
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

# ── Storage Account (required by Function App) ────────────────
Write-Host "[6/11] Creating storage account for Function App..." -ForegroundColor Yellow
az storage account create `
    --name           $StorageAcctName `
    --resource-group $ResourceGroup `
    --location       $Location `
    --sku            Standard_LRS `
    --output         none

# ── Application Insights ──────────────────────────────────────
Write-Host "[7/11] Creating Application Insights '$AppInsightsName'..." -ForegroundColor Yellow
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

# ── Function App ──────────────────────────────────────────────
Write-Host "[8/11] Creating Function App '$FunctionAppName'..." -ForegroundColor Yellow
az functionapp create `
    --name                  $FunctionAppName `
    --resource-group        $ResourceGroup `
    --storage-account       $StorageAcctName `
    --consumption-plan-location $Location `
    --runtime               python `
    --runtime-version       3.11 `
    --functions-version     4 `
    --output                none

# Enable System-assigned Managed Identity
az functionapp identity assign `
    --name           $FunctionAppName `
    --resource-group $ResourceGroup `
    --output         none

$FunctionPrincipalId = az functionapp identity show `
    --name           $FunctionAppName `
    --resource-group $ResourceGroup `
    --query          "principalId" `
    --output         tsv

# Set Function App application settings (non-secret)
az functionapp config appsettings set `
    --name           $FunctionAppName `
    --resource-group $ResourceGroup `
    --settings `
        "AZURE_APPCONFIG_ENDPOINT=$AppConfigEndpoint" `
        "AZURE_KEYVAULT_URL=https://$KvName.vault.azure.net/" `
        "APPLICATIONINSIGHTS_CONNECTION_STRING=$AiConnString" `
    --output none

# ── App Registration ──────────────────────────────────────────
Write-Host "[9/11] Creating App Registration '$AppRegName'..." -ForegroundColor Yellow

$AppRegJson = az ad app create `
    --display-name $AppRegName `
    --sign-in-audience AzureADMyOrg `
    --output json | ConvertFrom-Json

$AppClientId = $AppRegJson.appId
$AppObjectId = $AppRegJson.id

Write-Host "    App Client ID: $AppClientId" -ForegroundColor DarkGray

# Create service principal
az ad sp create --id $AppClientId --output none

# Add API permissions
$Permissions = @(
    @{ api="00000003-0000-0000-c000-000000000000"; permission="bc024368-1153-4739-b217-4326f2e966d0" }, # GroupMember.Read.All
    @{ api="00000003-0000-0000-c000-000000000000"; permission="df021288-bdef-4463-88db-98f22de89214" }, # User.Read.All
    @{ api="00000003-0000-0000-c000-000000000000"; permission="284383ee-7f6e-4e40-a2a8-e85dcb029101" }, # Calls.Initiate.All
    @{ api="00000003-0000-0000-c000-000000000000"; permission="f6b49018-60ab-4f12-bec5-6d2120a4f3f1" }, # Calls.JoinGroupCall.All
    @{ api="00000003-0000-0000-c000-000000000000"; permission="9c7a330d-35b3-4aa1-963d-cb2b9f927841" }  # Presence.Read.All
)

foreach ($p in $Permissions) {
    az ad app permission add `
        --id   $AppObjectId `
        --api  $p.api `
        --api-permissions "$($p.permission)=Role" `
        --output none
}

# Create client secret
$SecretJson = az ad app credential reset `
    --id     $AppObjectId `
    --years  2 `
    --output json | ConvertFrom-Json

$ClientSecret = $SecretJson.password
$SecretExpiry = (Get-Date).AddYears(2).ToString("yyyy-MM-dd")

Write-Host "    Client secret expires: $SecretExpiry" -ForegroundColor DarkYellow
Write-Host "    *** RECORD THIS DATE — set a calendar reminder 4 weeks before expiry ***" -ForegroundColor Red

# ── AD Security Group ─────────────────────────────────────────
Write-Host "[10/11] Creating AD Security Group '$GroupName'..." -ForegroundColor Yellow
$GroupJson = az ad group create `
    --display-name   $GroupName `
    --mail-nickname  $GroupName.Replace(' ','-') `
    --output         json | ConvertFrom-Json

$StaffGroupId = $GroupJson.id
Write-Host "    Staff Group ID: $StaffGroupId" -ForegroundColor DarkGray

# ── Key Vault secrets ─────────────────────────────────────────
Write-Host "[11/11] Storing secrets in Key Vault..." -ForegroundColor Yellow

az keyvault secret set --vault-name $KvName --name "acs-connection-string" --value $AcsConnString --output none
az keyvault secret set --vault-name $KvName --name "app-client-id"         --value $AppClientId   --output none
az keyvault secret set --vault-name $KvName --name "app-client-secret"     --value $ClientSecret  --output none

# Grant Function App Managed Identity access to Key Vault
$KvScope = az keyvault show --name $KvName --resource-group $ResourceGroup --query "id" --output tsv

az role assignment create `
    --role               "Key Vault Secrets User" `
    --assignee-object-id $FunctionPrincipalId `
    --assignee-principal-type ServicePrincipal `
    --scope              $KvScope `
    --output             none

# Grant Function App access to App Configuration (Reader)
$AppConfigScope = az appconfig show --name $AppConfigName --resource-group $ResourceGroup --query "id" --output tsv

az role assignment create `
    --role               "App Configuration Data Reader" `
    --assignee-object-id $FunctionPrincipalId `
    --assignee-principal-type ServicePrincipal `
    --scope              $AppConfigScope `
    --output             none

# ── Seed App Configuration ────────────────────────────────────
Write-Host "`nSeeding App Configuration with default values..." -ForegroundColor Yellow
& "$PSScriptRoot\Set-AppConfiguration.ps1" `
    -AppConfigName $AppConfigName `
    -ConfigFile    "$PSScriptRoot\..\config\appconfig-seed.json"

# ── Summary ───────────────────────────────────────────────────
Write-Host "`n=== Deployment Complete ===" -ForegroundColor Green
Write-Host ""
Write-Host "Resources created:" -ForegroundColor Cyan
Write-Host "  ACS Resource:       $AcsName"
Write-Host "  ACS Resource ID:    $AcsResourceId"
Write-Host "  Key Vault:          $KvName"
Write-Host "  App Configuration:  $AppConfigName ($AppConfigEndpoint)"
Write-Host "  Function App:       $FunctionAppName"
Write-Host "  App Insights:       $AppInsightsName"
Write-Host "  App Registration:   $AppRegName  (Client ID: $AppClientId)"
Write-Host "  Staff Group:        $GroupName  (Group ID: $StaffGroupId)"
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "  1. Grant admin consent: Azure AD > App Registrations > $AppRegName > API Permissions > Grant admin consent"
Write-Host "  2. Run New-TeamsResourceAccount.ps1 to link Teams resource account"
Write-Host "  3. Edit config\appconfig-seed.json and run Set-AppConfiguration.ps1"
Write-Host "  4. Add staff to the '$GroupName' group in Azure AD"
Write-Host "  5. Push code to GitHub — Actions will deploy the Function App"
Write-Host "  6. Run Test-EndToEnd.ps1 to validate the deployment"
Write-Host ""
Write-Host "SECRET EXPIRY: $SecretExpiry — add a calendar reminder now!" -ForegroundColor Red

# Save deployment summary to file for reference
$Summary = @{
    DeployedAt       = (Get-Date -Format "o")
    OrgPrefix        = $OrgPrefix
    TenantId         = $TenantId
    SubscriptionId   = $SubscriptionId
    ResourceGroup    = $ResourceGroup
    AcsName          = $AcsName
    AcsResourceId    = $AcsResourceId
    KeyVaultName     = $KvName
    AppConfigName    = $AppConfigName
    AppConfigEndpoint= $AppConfigEndpoint
    FunctionAppName  = $FunctionAppName
    AppInsightsName  = $AppInsightsName
    AppClientId      = $AppClientId
    AppObjectId      = $AppObjectId
    StaffGroupId     = $StaffGroupId
    SecretExpiry     = $SecretExpiry
}

$Summary | ConvertTo-Json | Out-File -FilePath "$PSScriptRoot\..\deployment-summary-$OrgPrefix.json" -Encoding utf8
Write-Host "`nDeployment summary saved to: deployment-summary-$OrgPrefix.json" -ForegroundColor DarkGray
