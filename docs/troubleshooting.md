# Virtual Receptionist — Troubleshooting Guide

## Quick Diagnostics

```powershell
# View last 24 hours of logs
.\scripts\Get-CallLogs.ps1 -AppInsightsName "contoso-receptionist-ai" -ResourceGroup "rg-virtual-receptionist"

# Filter for failures only
.\scripts\Get-CallLogs.ps1 -AppInsightsName "contoso-receptionist-ai" -ResourceGroup "rg-virtual-receptionist" -Filter "FAILED"

# Run smoke tests
.\scripts\Test-EndToEnd.ps1 -FunctionAppName "contoso-receptionist" -ResourceGroup "rg-virtual-receptionist"

# Health check via Kudu key retrieval (works before runtime fully initialises)
$Creds    = az functionapp deployment list-publishing-credentials --name "contoso-receptionist" --resource-group "rg-virtual-receptionist" --query "[publishingUserName, publishingPassword]" --output tsv
$KuduUser = ($Creds -split "`n")[0].Trim()
$KuduPass = ($Creds -split "`n")[1].Trim()
$Key      = (Invoke-RestMethod -Uri "https://contoso-receptionist.scm.azurewebsites.net/api/functions/admin/masterkey" -Headers @{ Authorization = "Basic " + [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("${KuduUser}:${KuduPass}")) }).masterKey
Invoke-RestMethod "https://contoso-receptionist.azurewebsites.net/api/health?code=$Key"

# Check pending transfer table exists after first matched call
az storage table exists --name pendingtransfers --account-name YOUR-STORAGE-ACCOUNT --output tsv
```

---

## Issue Index

| Symptom | Jump to |
|---|---|
| Call rings but bot never answers | [Call not reaching the bot](#call-not-reaching-bot) |
| Health check returns HTTP 404 | [HTTP 404 on health endpoint](#http-404-on-health-endpoint) |
| Bot answers but nothing happens after greeting | [ACS callback URL missing function key](#acs-callback-url-missing-function-key) |
| Greeting plays but nothing happens after | [Speech recognition not working](#speech-recognition-not-working) |
| Wrong person connected / bad name match | [Name matching issues](#name-matching-issues) |
| "Hanson" pronounced as "handsome" | [TTS mispronunciation](#tts-mispronunciation) |
| Transfer fails — caller stays with bot | [Transfer failures](#transfer-failures) |
| Transfer works on one instance but drops on another | [Scale-out transfer drops](#scale-out-transfer-drops) |
| Out-of-hours message plays during business hours | [Business hours misconfigured](#business-hours-misconfigured) |
| 403 error in logs — Key Vault or Graph API | [Permission errors](#permission-errors) |
| Config change not taking effect | [App Configuration not refreshing](#app-configuration-not-refreshing) |
| GitHub Actions deploy failing | [CI/CD failures](#cicd-failures) |
| Secret expired — calls broken | [Expired client secret](#expired-client-secret) |

---

## Call Not Reaching Bot

**Symptom:** Phone rings but no greeting plays. Bot logs show no activity.

**Check these in order:**

1. **ACS Event Subscription**
   ```
   Azure Portal > ACS Resource > Events > Event Subscriptions
   ```
   Confirm the `incoming-call-sub` subscription exists and the endpoint URL is correct.
   The URL must include `?code=YOUR-FUNCTION-KEY`.

2. **Function key in webhook URL**

   Use the Kudu SCM API — `az functionapp keys list` can fail before the runtime initialises:
   ```powershell
   $Creds    = az functionapp deployment list-publishing-credentials `
       --name contoso-receptionist --resource-group rg-virtual-receptionist `
       --query "[publishingUserName, publishingPassword]" --output tsv
   $KuduUser = ($Creds -split "`n")[0].Trim()
   $KuduPass = ($Creds -split "`n")[1].Trim()
   $Key = (Invoke-RestMethod `
       -Uri "https://contoso-receptionist.scm.azurewebsites.net/api/functions/admin/masterkey" `
       -Headers @{ Authorization = "Basic " + [Convert]::ToBase64String(
           [Text.Encoding]::ASCII.GetBytes("${KuduUser}:${KuduPass}")) }).masterKey
   Write-Host "Function key: $Key"
   ```

3. **Teams Resource Account ↔ ACS link**
   ```powershell
   Connect-MicrosoftTeams
   Get-CsOnlineApplicationInstance -Identity reception@contoso.com | Format-List
   ```
   Verify `ApplicationId` and `AcsResourceId` are both populated.
   If `AcsResourceId` is empty, re-run `New-TeamsResourceAccount.ps1`.

4. **ACS ↔ Teams interop**
   ```
   ACS Resource > Settings > Microsoft Teams interoperability > Status: Connected
   ```

5. **Function App running**
   ```powershell
   az functionapp show --name contoso-receptionist --resource-group rg-virtual-receptionist --query "state"
   ```

---

## HTTP 404 on Health Endpoint

**Symptom:** `curl` returns HTTP 404 for `/api/health`. The runtime is up (otherwise you'd get 503) but routes are not registered.

**Most likely causes in order:**

1. **Import failure at startup** — if any Python dependency fails to import, the Functions worker silently fails to register all routes and returns 404 for everything. Check the live log stream:
   ```powershell
   az webapp log tail --name contoso-receptionist --resource-group rg-virtual-receptionist
   ```
   Look for a Python traceback immediately after `Worker process started`. A missing package or wrong platform wheel is the most common cause.

2. **`function_app.py` not at wwwroot root** — the Functions runtime requires `function_app.py` at the root of the deployed package. Verify via Kudu:
   ```powershell
   $Creds    = az functionapp deployment list-publishing-credentials --name contoso-receptionist --resource-group rg-virtual-receptionist --query "[publishingUserName, publishingPassword]" --output tsv
   $KuduUser = ($Creds -split "`n")[0].Trim()
   $KuduPass = ($Creds -split "`n")[1].Trim()
   Invoke-RestMethod `
       -Uri "https://contoso-receptionist.scm.azurewebsites.net/api/vfs/site/wwwroot/" `
       -Headers @{ Authorization = "Basic " + [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("${KuduUser}:${KuduPass}")) } |
       Select-Object -ExpandProperty name
   ```
   You must see `function_app.py` directly in the listing. If you see a `bot/` subdirectory, the deployment package path is wrong.

3. **`FUNCTIONS_WORKER_RUNTIME` not set to `python`**
   ```powershell
   az functionapp config appsettings list --name contoso-receptionist --resource-group rg-virtual-receptionist --query "[?name=='FUNCTIONS_WORKER_RUNTIME']"
   ```

4. **Cold start timing** — the Python worker on Consumption plan can take 90+ seconds after a new deployment. Wait and retry before investigating further.

---

## ACS Callback URL Missing Function Key

**Symptom:** Bot answers calls and plays the greeting, but then nothing happens. No `RecognizeCompleted`, `PlayCompleted`, or `CallTransferAccepted` events appear in App Insights. ACS diagnostics show HTTP 401 on the callback URL.

**Cause:** `receptionist:acs_callback_url` in App Configuration is missing `?code=YOUR-FUNCTION-KEY`. ACS mid-call events POST to this URL. Without the function key, the Functions host returns HTTP 401 and silently drops the event.

**Fix:**
```powershell
# Get the function key via Kudu
$Creds    = az functionapp deployment list-publishing-credentials --name contoso-receptionist --resource-group rg-virtual-receptionist --query "[publishingUserName, publishingPassword]" --output tsv
$KuduUser = ($Creds -split "`n")[0].Trim()
$KuduPass = ($Creds -split "`n")[1].Trim()
$Key = (Invoke-RestMethod -Uri "https://contoso-receptionist.scm.azurewebsites.net/api/functions/admin/masterkey" `
    -Headers @{ Authorization = "Basic " + [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("${KuduUser}:${KuduPass}")) }).masterKey

# Update App Configuration
az appconfig kv set --name "contoso-receptionist-config" `
    --key   "receptionist:acs_callback_url" `
    --value "https://contoso-receptionist.azurewebsites.net/api/acs_callback?code=$Key" `
    --yes
```

Also update `config/appconfig-seed.json` for future deployments.

---

## Speech Recognition Not Working

**Symptom:** Greeting plays, silence, then "I didn't catch that" — even when speaking clearly.

1. **Confirm `?code=` is in acs_callback_url** — `RecognizeCompleted` is a mid-call callback event. If the callback URL returns 401 it will never arrive. See above section.

2. **Check speech_language matches voice locale**
   ```
   receptionist:voice_name      = en-AU-NatashaNeural
   receptionist:speech_language = en-AU        ← must match
   ```

3. **Check App Insights for RecognizeFailed events**
   ```powershell
   .\scripts\Get-CallLogs.ps1 -Filter "RecognizeFailed"
   ```

4. **Increase end_silence_timeout** — default is 1500ms. Callers who pause before speaking may time out. Update `end_silence_timeout_in_ms` in `call_handler.py`.

---

## Name Matching Issues

**Symptom:** Caller says a name clearly but gets routed to reception instead.

1. **Lower the match threshold**
   ```powershell
   az appconfig kv set --name contoso-receptionist-config --key "receptionist:match_threshold" --value "55" --yes
   ```
   Do not go below 50.

2. **Check the staff group has the right members**
   ```powershell
   az ad group member list --group VirtualReceptionist-Staff --output table
   ```

3. **View what the bot actually heard**
   ```powershell
   .\scripts\Get-CallLogs.ps1 -Filter "Speech recognised"
   ```

4. **Common mismatch patterns**

   | Caller says | AD displayName | Fix |
   |---|---|---|
   | "Sarah" | "Sarah Jones" | Works if Sarah is unique |
   | "Nguyen" | "David Nguyen" | Lower threshold or add phonetic override |
   | "Doctor Smith" | "James Smith" | Caller should say first + last name |

---

## TTS Mispronunciation

**Symptom:** Bot says a name incorrectly (e.g. "Hanson" → "handsome").

Set `extensionAttribute1` in Azure AD for the affected user:

```powershell
$User = Get-MgUser -Filter "displayName eq 'Hanson Lee'"
Update-MgUser -UserId $User.Id -OnPremisesExtensionAttributes @{extensionAttribute1 = "HAN-son"}
```

| Name | extensionAttribute1 | Result |
|---|---|---|
| Hanson | `HAN-son` | Bot says "HAN-son" |
| Nguyen | `win` | Bot says "win" |
| Siobhan | `ʃɪˈvɔːn` | IPA — bot uses phoneme tag |
| Aoife | `EE-fa` | Bot says "EE-fa" |

Takes effect within 5 minutes (Graph cache TTL). No redeployment needed.

---

## Transfer Failures

**Symptom:** Bot says "Connecting you to [name]" but call never transfers.

1. **Check transfer failure logs**
   ```powershell
   .\scripts\Get-CallLogs.ps1 -Filter "Transfer FAILED"
   ```

2. **Verify the AAD Object ID** — `default_reception_aad_id` must be the Azure AD Object ID, not a UPN:
   ```powershell
   az ad user show --id reception@contoso.com --query id --output tsv
   ```

3. **Verify ACS ↔ Teams interop is still connected**
   ```
   ACS Resource > Settings > Microsoft Teams interoperability > Status: Connected
   ```

4. **Check the target user has a Teams Phone license**

5. **Check Calls.Initiate.All admin consent**
   ```
   Azure AD > App Registrations > VirtualReceptionist > API Permissions > all show ✓
   ```

---

## Scale-Out Transfer Drops

**Symptom:** Transfers work on low-traffic days but occasionally drop during busy periods. The `pendingtransfers` Table Storage row is missing when `PlayCompleted` fires.

**Cause:** The `PendingTransferStore` writes to Azure Table Storage keyed on `callConnectionId`. If the `save()` call fails (storage connectivity issue, wrong connection string), the pending state is lost and the transfer silently drops.

**Check:**
```powershell
# Confirm AzureWebJobsStorage is set
az functionapp config appsettings list --name contoso-receptionist --resource-group rg-virtual-receptionist --query "[?name=='AzureWebJobsStorage']"

# Check the pendingtransfers table exists and has rows
az storage table exists --name pendingtransfers --account-name YOUR-STORAGE-ACCOUNT --output tsv
```

---

## Business Hours Misconfigured

**Symptom:** Out-of-hours message plays during business hours, or calls connect outside hours.

1. **Verify timezone** — must be a valid IANA string:
   ```
   Australia/Sydney    ✓
   Europe/London       ✓
   AEST                ✗  (not valid IANA)
   ```

2. **Verify hours format** — `HH:MM-HH:MM` (zero-padded 24-hour):
   ```
   08:30-17:30   ✓
   8:30-5:30     ✗
   ```

3. **Check current time in timezone**
   ```powershell
   [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, "AUS Eastern Standard Time")
   ```

---

## Permission Errors

**Symptom:** Logs show `403 Forbidden` from Graph API or Key Vault.

**Key Vault 403:**
1. Function App > Identity > System-assigned Managed Identity must be **On**
2. Key Vault > Access Control (IAM) > Function App MI needs **Key Vault Secrets User**
3. Wait 5 minutes for IAM propagation

**Graph API 403:**
1. Azure AD > App Registrations > VirtualReceptionist > API Permissions
2. All permissions must be **Application** type, status must show ✓
3. Click **Grant admin consent** if any show ⚠ (requires Global Admin)

**App Configuration 403:**
1. App Configuration > Access Control (IAM) > Function App MI needs **App Configuration Data Reader**

---

## App Configuration Not Refreshing

Config is cached for 5 minutes. After making a change either wait 5 minutes or restart:
```powershell
az functionapp restart --name contoso-receptionist --resource-group rg-virtual-receptionist
```

---

## CI/CD Failures

**Symptom:** GitHub Actions deploy workflow fails.

1. **Check GitHub secrets** — required: `AZURE_CREDENTIALS`, `AZURE_RESOURCE_GROUP`, `FUNCTION_APP_NAME`, `TEAMS_WEBHOOK_URL`

2. **HTTP 404 on health check** — see [HTTP 404 on health endpoint](#http-404-on-health-endpoint) above. The deploy workflow now dumps the wwwroot file listing on 404 to help diagnose this.

3. **Deployment settings conflict** — the workflow must have `SCM_DO_BUILD_DURING_DEPLOYMENT=true` and `scm-do-build-during-deployment: true`. `enable-oryx-build` must NOT be set — it conflicts with the SCM build pipeline.

4. **AZURE_CREDENTIALS format**
   ```bash
   az ad sp create-for-rbac --name "github-receptionist-deploy" \
     --role contributor \
     --scopes /subscriptions/YOUR-SUB-ID/resourceGroups/rg-virtual-receptionist \
     --sdk-auth
   ```

---

## Expired Client Secret

**Symptom:** Graph API calls all return 401. Bot routes everything to reception.

```powershell
# Check expiry
az ad app credential list --id YOUR-APP-OBJECT-ID --output table

# Rotate
.\scripts\Rotate-ClientSecret.ps1 `
    -AppObjectId     "YOUR-APP-OBJECT-ID" `
    -KeyVaultName    "contoso-receptionist-kv" `
    -FunctionAppName "contoso-receptionist" `
    -ResourceGroup   "rg-virtual-receptionist"
```

The bot reads the secret from Key Vault at call time — no restart is needed after rotation.
Set a calendar reminder for **22 months** after rotation.

---

## Collecting a Diagnostic Bundle

```powershell
# 1. Recent logs
.\scripts\Get-CallLogs.ps1 -Hours 2 | Out-File diagnostic-logs.txt

# 2. Health check
$Creds    = az functionapp deployment list-publishing-credentials --name "contoso-receptionist" --resource-group "rg-virtual-receptionist" --query "[publishingUserName, publishingPassword]" --output tsv
$KuduUser = ($Creds -split "`n")[0].Trim()
$KuduPass = ($Creds -split "`n")[1].Trim()
$Key = (Invoke-RestMethod -Uri "https://contoso-receptionist.scm.azurewebsites.net/api/functions/admin/masterkey" -Headers @{ Authorization = "Basic " + [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("${KuduUser}:${KuduPass}")) }).masterKey
Invoke-RestMethod "https://contoso-receptionist.azurewebsites.net/api/health?code=$Key" | ConvertTo-Json

# 3. Resource account status
Connect-MicrosoftTeams
Get-CsOnlineApplicationInstance -Identity reception@contoso.com | Format-List | Out-File diagnostic-teams.txt

# 4. App Configuration current values
az appconfig kv list --name contoso-receptionist-config --key "receptionist:*" --output table | Out-File diagnostic-config.txt

# 5. App permissions
az ad app permission list --id YOUR-APP-OBJECT-ID --output table

# 6. wwwroot file listing (for 404 diagnosis)
Invoke-RestMethod -Uri "https://contoso-receptionist.scm.azurewebsites.net/api/vfs/site/wwwroot/" `
    -Headers @{ Authorization = "Basic " + [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("${KuduUser}:${KuduPass}")) } |
    Select-Object -ExpandProperty name
```
