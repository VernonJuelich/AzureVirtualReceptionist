# Virtual Receptionist — Troubleshooting Guide

## Quick Diagnostics

```powershell
# View last 24 hours of logs
.\scripts\Get-CallLogs.ps1 -AppInsightsName "contoso-receptionist-ai" -ResourceGroup "rg-virtual-receptionist"

# Filter for failures only
.\scripts\Get-CallLogs.ps1 -AppInsightsName "contoso-receptionist-ai" -ResourceGroup "rg-virtual-receptionist" -Filter "FAILED"

# Run smoke tests
.\scripts\Test-EndToEnd.ps1 -FunctionAppName "contoso-receptionist" -ResourceGroup "rg-virtual-receptionist"

# Health check
curl "https://contoso-receptionist.azurewebsites.net/api/health?code=YOUR-FUNCTION-KEY"
```

---

## Issue Index

| Symptom | Jump to |
|---|---|
| Call rings but bot never answers | [Call not reaching the bot](#call-not-reaching-bot) |
| Greeting plays but nothing happens after | [Speech recognition not working](#speech-recognition-not-working) |
| Wrong person connected / bad name match | [Name matching issues](#name-matching-issues) |
| "Hanson" pronounced as "handsome" | [TTS mispronunciation](#tts-mispronunciation) |
| Transfer fails — caller stays with bot | [Transfer failures](#transfer-failures) |
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
   ```powershell
   az functionapp keys list --name contoso-receptionist --resource-group rg-virtual-receptionist
   ```
   Get the `default` function key. Verify it matches what's in the Event Subscription URL.

3. **Teams Resource Account ↔ ACS link**
   ```powershell
   Connect-MicrosoftTeams
   Get-CsOnlineApplicationInstance -Identity reception@contoso.com | Format-List
   ```
   Verify `ApplicationId` and `AcsResourceId` are both populated.
   If `AcsResourceId` is empty, re-run `New-TeamsResourceAccount.ps1`.

4. **ACS ↔ Teams interop**
   ```
   ACS Resource > Settings > Microsoft Teams interoperability
   ```
   Status must show **Connected**.

5. **Function App running**
   ```powershell
   az functionapp show --name contoso-receptionist --resource-group rg-virtual-receptionist --query "state"
   ```
   Should return `"Running"`. If stopped: `az functionapp start --name contoso-receptionist --resource-group rg-virtual-receptionist`

---

## Speech Recognition Not Working

**Symptom:** Greeting plays, silence, then "I didn't catch that" — even when speaking clearly.

1. **Check speech_language matches voice locale**
   In App Configuration, both keys should use the same locale:
   ```
   receptionist:voice_name     = en-AU-NatashaNeural
   receptionist:speech_language = en-AU       ← must match
   ```

2. **Check end_silence_timeout**
   The default is 1500ms (1.5 seconds). If callers pause before speaking, increase this.
   Update `end_silence_timeout_in_ms` in `call_handler.py` and redeploy.

3. **Check App Insights for RecognizeFailed events**
   ```powershell
   .\scripts\Get-CallLogs.ps1 -Filter "RecognizeFailed"
   ```
   The `resultInformation.message` field will show the reason (e.g. `NoMatch`, `InitialSilenceTimeout`).

4. **Test with a different voice/language combination**
   Update App Configuration:
   ```powershell
   az appconfig kv set --name contoso-receptionist-config --key "receptionist:speech_language" --value "en-US" --yes
   ```

---

## Name Matching Issues

**Symptom:** Caller says a name clearly but gets routed to reception instead.

1. **Check the match threshold**
   Default is 65. If legitimate names are being missed, lower it:
   ```
   Azure Portal > App Configuration > receptionist:match_threshold > set to 55
   ```
   Do not go below 50 — false positives increase.

2. **Check the staff group has the right members**
   ```powershell
   az ad group member list --group VirtualReceptionist-Staff --output table
   ```
   Confirm the person is in the group and their `displayName` is correct.

3. **View what the bot actually heard**
   ```powershell
   .\scripts\Get-CallLogs.ps1 -Filter "Speech recognised"
   ```
   The log shows `Speech recognised: 'what caller said' (confidence=X.XX)`.
   Compare to the actual displayName in Azure AD.

4. **Common mismatch patterns and fixes**

   | Caller says | AD displayName | Fix |
   |---|---|---|
   | "Sarah" | "Sarah Jones" | Works if Sarah is unique in group |
   | "Nguyen" | "David Nguyen" | Lower threshold or add nickname |
   | "Doctor Smith" | "James Smith" | Caller should say "James Smith" |
   | "Hanson" | "Hanson Lee" | Should match — if not, check threshold |

---

## TTS Mispronunciation

**Symptom:** Bot says a name incorrectly (e.g. "Hanson" → "handsome", "Nguyen" → "new-yen").

**Fix: Set extensionAttribute1 in Azure AD for the affected user.**

1. Azure Portal > Azure AD > Users > find the user
2. Edit Properties > Job info section or custom attributes
3. Set **extensionAttribute1** to the phonetic spelling

**Examples:**

| Name | extensionAttribute1 | Result |
|---|---|---|
| Hanson | `HAN-son` | Bot says "HAN-son" |
| Nguyen | `win` | Bot says "win" |
| Siobhan | `ʃɪˈvɔːn` | IPA — bot uses phoneme tag |
| Zbigniew | `zbig-nyev` | Bot says "zbig-nyev" |
| Aoife | `EE-fa` | Bot says "EE-fa" |

**Via PowerShell:**
```powershell
# Set pronunciation override for a user
$User = Get-MgUser -Filter "displayName eq 'Hanson Lee'"
Update-MgUser -UserId $User.Id -OnPremisesExtensionAttributes @{extensionAttribute1 = "HAN-son"}
```

The bot reads `extensionAttribute1` via Graph API when loading group members.
No redeployment needed — takes effect within 5 minutes (cache TTL).

---

## Transfer Failures

**Symptom:** Bot says "Connecting you to [name]" but call never transfers. Or bot says "That extension is unavailable."

1. **Check transfer failure logs**
   ```powershell
   .\scripts\Get-CallLogs.ps1 -Filter "Transfer FAILED"
   ```

2. **Verify the AAD Object ID is correct**
   The `default_reception_aad_id` in App Configuration must be the user's **Azure AD Object ID**, not their email or UPN.
   ```powershell
   az ad user show --id reception@contoso.com --query id --output tsv
   ```

3. **Verify ACS ↔ Teams interop is still connected**
   ```
   ACS Resource > Settings > Microsoft Teams interoperability > Status: Connected
   ```

4. **Check the target user has a Teams Phone license**
   The user being transferred to must have a Teams Phone license and be homed in Teams (not Skype for Business).

5. **Check Calls.Initiate.All admin consent**
   ```
   Azure AD > App Registrations > VirtualReceptionist > API Permissions
   ```
   All permissions must show green ✓ under Status.

---

## Business Hours Misconfigured

**Symptom:** Out-of-hours message plays during business hours, or calls connect outside hours.

1. **Verify timezone in App Configuration**
   Must be a valid IANA timezone string:
   ```
   Australia/Sydney    ✓
   Europe/London       ✓
   America/New_York    ✓
   AEST                ✗  (not valid IANA)
   ```

2. **Verify hours format**
   Must be `HH:MM-HH:MM` using 24-hour time:
   ```
   08:30-17:30   ✓
   8:30-5:30     ✗  (must be zero-padded)
   08:30-17:30   ✓
   ```

3. **Check current time in the configured timezone**
   ```powershell
   [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, "AUS Eastern Standard Time")
   ```

4. **Update via App Configuration**
   ```powershell
   az appconfig kv set --name contoso-receptionist-config --key "receptionist:business_hours_mon" --value "09:00-17:00" --yes
   ```

---

## Permission Errors

**Symptom:** Logs show `403 Forbidden` from Graph API or Key Vault.

**Key Vault 403:**
1. Function App > Identity > confirm System-assigned Managed Identity is **On**
2. Key Vault > Access Control (IAM) > confirm Function App's managed identity has **Key Vault Secrets User** role
3. Wait 5 minutes for IAM propagation

**Graph API 403:**
1. Azure AD > App Registrations > VirtualReceptionist > API Permissions
2. All permissions must be **Application** type (not Delegated)
3. Status column must show green ✓ for all
4. If any show ⚠, click **Grant admin consent** (requires Global Admin)

**App Configuration 403:**
1. App Configuration > Access Control (IAM) > confirm Function App has **App Configuration Data Reader** role

---

## App Configuration Not Refreshing

**Symptom:** Changed a value in App Configuration but bot still uses old value.

The bot caches App Config for 5 minutes. After making a change:
- Wait 5 minutes, then make a test call
- Or restart the Function App to force immediate refresh:
  ```powershell
  az functionapp restart --name contoso-receptionist --resource-group rg-virtual-receptionist
  ```

---

## CI/CD Failures

**Symptom:** GitHub Actions deploy workflow fails.

1. **Check GitHub secrets are set**
   Repository > Settings > Secrets and variables > Actions
   Required: `AZURE_CREDENTIALS`, `AZURE_RESOURCE_GROUP`, `FUNCTION_APP_NAME`, `TEAMS_WEBHOOK_URL`

2. **AZURE_CREDENTIALS format**
   Must be the full JSON from:
   ```bash
   az ad sp create-for-rbac --name "github-receptionist-deploy" \
     --role contributor \
     --scopes /subscriptions/YOUR-SUB-ID/resourceGroups/rg-virtual-receptionist \
     --sdk-auth
   ```

3. **View workflow logs**
   GitHub > Actions tab > select the failed run > expand the failed step

---

## Expired Client Secret

**Symptom:** Graph API calls all return 401 Unauthorized. Bot routes everything to reception.

**Immediate fix:**
```powershell
# Check expiry
az ad app credential list --id YOUR-APP-OBJECT-ID --output table

# Rotate immediately
.\scripts\Rotate-ClientSecret.ps1 `
    -AppObjectId     "YOUR-APP-OBJECT-ID" `
    -KeyVaultName    "contoso-receptionist-kv" `
    -FunctionAppName "contoso-receptionist" `
    -ResourceGroup   "rg-virtual-receptionist"
```

**Prevention:** The `Rotate-ClientSecret.ps1` script logs the new expiry date.
Set a calendar reminder for **22 months** after rotation.

---

## Collecting a Diagnostic Bundle

When escalating an issue, collect this information:

```powershell
# 1. Recent logs
.\scripts\Get-CallLogs.ps1 -Hours 2 | Out-File diagnostic-logs.txt

# 2. Health check
Invoke-RestMethod "https://contoso-receptionist.azurewebsites.net/api/health?code=KEY" | ConvertTo-Json

# 3. Resource account status
Connect-MicrosoftTeams
Get-CsOnlineApplicationInstance -Identity reception@contoso.com | Format-List | Out-File diagnostic-teams.txt

# 4. App Configuration current values
az appconfig kv list --name contoso-receptionist-config --key "receptionist:*" --output table | Out-File diagnostic-config.txt

# 5. App permissions
az ad app permission list --id YOUR-APP-OBJECT-ID --output table
```
