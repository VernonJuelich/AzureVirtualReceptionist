<#
.SYNOPSIS
    Deploys the full Azure Virtual Receptionist infrastructure.

.DESCRIPTION
    Creates all required Azure resources in dependency order, including an
    Azure AI Services account so ACS speech recognition has a valid
    cognitive_services_endpoint.
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

$StorageSuffix   = "fnstore"
$StorageRaw      = ($OrgPrefix -replace '[^a-z0-9]','') + $StorageSuffix
$StorageAcctName = $StorageRaw.Substring(0, [Math]::Min($StorageRaw.Length, 24))

$AcsName         = "$OrgPrefix-acs-receptionist"
$KvName          = "$OrgPrefix-receptionist-kv"
$AppConfigName   = "$OrgPrefix-receptionist-config"
$FunctionAppName = "$OrgPrefix-receptionist"
$AppInsightsName = "$OrgPrefix-receptionist-ai"
$AiServicesName  = "$OrgPrefix-aiservices"
$AppRegName      = "VirtualReceptionist-$OrgPrefix"
$GroupName       = "VirtualReceptionist-Staff-$OrgPrefix"

Write-Host "`n=== Azure Virtual Receptionist -- Infrastructure Deployment ===" -ForegroundColor Cyan
Write-Host "Tenant:          $TenantId"
Write-Host "Subscription:    $SubscriptionId"
Write-Host "Resource Group:  $ResourceGroup"
Write-Host "Location:        $Location"
Write-Host "Org Prefix:      $OrgPrefix"
Write-Host "Storage Account: $StorageAcctName"
Write-Host "ACS Data Loc:    $AcsDataLocation`n"

Write-Host "[1/13] Logging in to Azure..." -ForegroundColor Yellow
az login --tenant $TenantId --output none
az account set --subscription $SubscriptionId

Write-Host "[2/13] Creating resource group '$ResourceGroup'..." -ForegroundColor Yellow
az group create --name $ResourceGroup --location $Location --output none

Write-Host "[3/13] Creating ACS resource '$AcsName'..." -ForegroundColor Yellow
az communication create `
    --name           $AcsName `
    --resource-group $ResourceGroup `
    --location       global `
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

Write-Host "[4/13] Creating Key Vault '$KvName'..." -ForegroundColor Yellow
az keyvault create `
    --name           $KvName `
    --resource-group $ResourceGroup `
    --location       $Location `
    --sku            standard `
    --output         none

Write-Host "[5/13] Creating App Configuration '$AppConfigName'..." -ForegroundColor Yellow
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

Write-Host "[6/13] Creating Azure AI Services account '$AiServicesName'..." -ForegroundColor Yellow
az cognitiveservices account create `
    --name           $AiServicesName `
    --resource-group $ResourceGroup `
    --kind           AIServices `
    --sku            S0 `
    --location       $Location `
    --custom-domain  $AiServicesName `
    --yes `
    --output         none

$CognitiveServicesEndpoint = az cognitiveservices account show `
    --name           $AiServicesName `
    --resource-group $ResourceGroup `
    --query          "properties.endpoint" `
    --output         tsv

Write-Host "[7/13] Creating storage account '$StorageAcctName'..." -ForegroundColor Yellow
az storage account create `
    --name           $StorageAcctName `
    --resource-group $ResourceGroup `
    --location       $Location `
    --sku            Standard_LRS `
    --output         none

Write-Host "[8/13] Creating Application Insights '$AppInsightsName'..." -ForegroundColor Yellow
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

Write-Host "[9/13] Creating Function App '$FunctionAppName'..." -ForegroundColor Yellow
az functionapp create `
    --name                      $FunctionAppName `
    --resource-group            $ResourceGroup `
    --storage-account           $StorageAcctName `
    --consumption-plan-location $Location `
    --runtime                   python `
    --runtime-version           3.11 `
    --functions-version         4 `
    --os-type                   linux `
    --output                    none

az functionapp identity assign `
    --name           $FunctionAppName `
    --resource-group $ResourceGroup `
    --output         none

$FunctionPrincipalId = az functionapp identity show `
    --name           $FunctionAppName `
    --resource-group $ResourceGroup `
    --query          "principalId" `
    --output         tsv

az functionapp config appsettings set `
    --name           $FunctionAppName `
    --resource-group $ResourceGroup `
    --settings `
        "AZURE_APPCONFIG_ENDPOINT=$AppConfigEndpoint" `
        "AZURE_KEYVAULT_URL=https://$KvName.vault.azure.net/" `
        "APPLICATIONINSIGHTS_CONNECTION_STRING=$AiConnString" `
        "FUNCTIONS_WORKER_RUNTIME=python" `
    --output none

Write-Host "[10/13] Creating App Registration '$AppRegName'..." -ForegroundColor Yellow
$AppRegJson  = az ad app create `
    --display-name      $AppRegName `
    --sign-in-audience  AzureADMyOrg `
    --output            json | ConvertFrom-Json

$AppClientId = $AppRegJson.appId
$AppObjectId = $AppRegJson.id
az ad sp create --id $AppClientId --output none

$GraphApi = "00000003-0000-0000-c000-000000000000"
$Perms = @(
    "5b567255-7703-4780-807c-7be8301ae99b",  # Group.Read.All
    "df021288-bdef-4463-88db-98f22de89214",  # User.Read.All
    "284383ee-7f6e-4e40-a2a8-e85dcb029101"   # Calls.Initiate.All
)
foreach ($p in $Perms) {
    az ad app permission add `
        --id              $AppObjectId `
        --api             $GraphApi `
        --api-permissions "$p=Role" `
        --output          none
}

$SecretJson   = az ad app credential reset `
    --id     $AppObjectId `
    --years  2 `
    --output json | ConvertFrom-Json
$ClientSecret = $SecretJson.password
$SecretExpiry = (Get-Date).AddYears(2).ToString("yyyy-MM-dd")

Write-Host "[11/13] Creating AD Security Group '$GroupName'..." -ForegroundColor Yellow
$GroupJson    = az ad group create `
    --display-name  $GroupName `
    --mail-nickname ($GroupName -replace '\s','-') `
    --output        json | ConvertFrom-Json
$StaffGroupId = $GroupJson.id

Write-Host "[12/13] Storing secrets in Key Vault and assigning roles..." -ForegroundColor Yellow
$CallerObjectId = az ad signed-in-user show --query id --output tsv
$KvScopeEarly = az keyvault show `
    --name           $KvName `
    --resource-group $ResourceGroup `
    --query          "id" `
    --output         tsv

az role assignment create `
    --role                    "Key Vault Secrets Officer" `
    --assignee-object-id      $CallerObjectId `
    --assignee-principal-type User `
    --scope                   $KvScopeEarly `
    --output                  none

Start-Sleep -Seconds 15

function Set-KeyVaultSecretFromValue {
    param([string]$VaultName, [string]$SecretName, [string]$SecretValue)
    $TempFile = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllText($TempFile, $SecretValue)
        az keyvault secret set --vault-name $VaultName --name $SecretName --file $TempFile --output none
    }
    finally {
        Remove-Item -Path $TempFile -Force -ErrorAction SilentlyContinue
    }
}

Set-KeyVaultSecretFromValue -VaultName $KvName -SecretName "acs-connection-string" -SecretValue $AcsConnString
Set-KeyVaultSecretFromValue -VaultName $KvName -SecretName "app-client-id"         -SecretValue $AppClientId
Set-KeyVaultSecretFromValue -VaultName $KvName -SecretName "app-client-secret"     -SecretValue $ClientSecret
$ClientSecret = $null

$KvScope = az keyvault show --name $KvName --resource-group $ResourceGroup --query "id" --output tsv
az role assignment create --role "Key Vault Secrets User" --assignee-object-id $FunctionPrincipalId --assignee-principal-type ServicePrincipal --scope $KvScope --output none

$AppConfigScope = az appconfig show --name $AppConfigName --resource-group $ResourceGroup --query "id" --output tsv
az role assignment create --role "App Configuration Data Reader" --assignee-object-id $FunctionPrincipalId --assignee-principal-type ServicePrincipal --scope $AppConfigScope --output none

Write-Host "[13/13] Seeding App Configuration..." -ForegroundColor Yellow
& "$PSScriptRoot\Set-AppConfiguration.ps1" `
    -AppConfigName $AppConfigName `
    -ConfigFile    "$PSScriptRoot\..\config\appconfig-seed.json"

az appconfig kv set --name $AppConfigName --key "receptionist:tenant_id" --value $TenantId --yes --output none
az appconfig kv set --name $AppConfigName --key "receptionist:staff_group_id" --value $StaffGroupId --yes --output none
az appconfig kv set --name $AppConfigName --key "receptionist:cognitive_services_endpoint" --value $CognitiveServicesEndpoint --yes --output none
az appconfig kv set --name $AppConfigName --key "receptionist:acs_callback_url" --value "https://$FunctionAppName.azurewebsites.net/api/acs_callback" --yes --output none

Write-Host "`n=== Deployment Complete ===" -ForegroundColor Green
Write-Host "  ACS:                 $AcsName"
Write-Host "  Key Vault:           $KvName"
Write-Host "  App Config:          $AppConfigName"
Write-Host "  Azure AI Services:   $AiServicesName"
Write-Host "  AI Endpoint:         $CognitiveServicesEndpoint"
Write-Host "  Function App:        $FunctionAppName"
Write-Host "  App Insights:        $AppInsightsName"
Write-Host "  App Reg Client ID:   $AppClientId"
Write-Host "  Staff Group ID:      $StaffGroupId"
Write-Host "  Secret Expiry:       $SecretExpiry"
