# Azure Virtual Receptionist

Speech-driven call routing for Microsoft Teams using Azure Communication Services.

Callers speak a staff member's name → Azure AI Speech transcribes it → phonetic + fuzzy match against Azure AD Security Group → call transferred via Teams Direct Routing.

---

## Repo Structure

```
├── .github/
│   └── workflows/
│       └── deploy.yml              # GitHub Actions — deploy bot to Azure Function App
├── bot/
│   ├── function_app.py             # Azure Functions v2 entry point + route definitions
│   ├── call_handler.py             # ACS call orchestration (answer, recognise, transfer)
│   ├── matcher.py                  # Phonetic + fuzzy name matching engine
│   ├── config_loader.py            # Azure App Configuration reader (5 min cache)
│   ├── graph_client.py             # Microsoft Graph API — AD group member lookup
│   ├── host.json                   # Functions host configuration
│   └── requirements.txt            # Python dependencies
├── scripts/
│   ├── Deploy-Infrastructure.ps1   # Full infrastructure deployment (all 12 resources)
│   ├── Deploy-BotCode.ps1          # Deploy Python code to Function App (manual)
│   ├── New-TeamsResourceAccount.ps1# Teams resource account + ACS linking + license
│   ├── Set-AppConfiguration.ps1    # Seed/update Azure App Configuration values
│   ├── Set-AlertRules.ps1          # Create Azure Monitor + Teams webhook alerts
│   ├── Get-CallLogs.ps1            # Query App Insights call logs
│   ├── Test-EndToEnd.ps1           # Automated post-deploy smoke tests (8 checks)
│   └── Rotate-ClientSecret.ps1     # Secret rotation lifecycle management
├── config/
│   └── appconfig-seed.json         # Initial App Configuration values (template)
├── alerts/
│   └── alert-rules.json            # Alert rule definitions
└── docs/
    └── troubleshooting.md          # Troubleshooting guide
```

---

## Quick Start

### 1 — Prerequisites
- Azure CLI (`az login`)
- Teams PowerShell: `Install-Module MicrosoftTeams`
- Microsoft.Graph.Users: `Install-Module Microsoft.Graph.Users`
- Python 3.11 + Azure Functions Core Tools v4
- A GitHub repo with these secrets set (Settings > Secrets > Actions):

| Secret name | Value |
|---|---|
| `AZURE_CREDENTIALS` | Service principal JSON from `az ad sp create-for-rbac --sdk-auth` |
| `AZURE_SUBSCRIPTION_ID` | Your Azure subscription ID |
| `AZURE_RESOURCE_GROUP` | e.g. `rg-virtual-receptionist` |
| `FUNCTION_APP_NAME` | e.g. `contoso-receptionist` |
| `TEAMS_WEBHOOK_URL` | Power Automate flow URL (not a deprecated O365 connector URL) |

### 2 — Deploy Infrastructure
```powershell
.\scripts\Deploy-Infrastructure.ps1 `
    -TenantId        "your-tenant-id" `
    -SubscriptionId  "your-subscription-id" `
    -ResourceGroup   "rg-virtual-receptionist" `
    -Location        "australiaeast" `
    -OrgPrefix       "contoso"
```

### 3 — Link Teams Resource Account
```powershell
.\scripts\New-TeamsResourceAccount.ps1 `
    -UPN          "reception@contoso.com" `
    -DisplayName  "Virtual Receptionist" `
    -AppId        "your-app-registration-client-id" `
    -AcsResourceId "/subscriptions/.../communicationServices/contoso-acs" `
    -PhoneNumber  "+61XXXXXXXXX"
```

This script creates the resource account, sets the ACS Resource ID, syncs provisioning,
**assigns the Teams Phone Resource Account license**, and assigns the phone number.
All steps are automated — no manual action in Teams Admin Center is required.

### 4 — Update and Seed App Configuration

Edit `config/appconfig-seed.json` with your real values first:
- Set `receptionist:company_name`, `receptionist:greeting_message`, etc.
- Set `receptionist:staff_group_id` to the AD Group Object ID from the deployment summary
- Set `receptionist:default_reception_aad_id` to the reception user's AAD Object ID
- **Set `receptionist:acs_callback_url` to the full callback URL including the function key:**
  ```
  https://contoso-receptionist.azurewebsites.net/api/acs_callback?code=YOUR-FUNCTION-KEY
  ```
  Retrieve the key from: Azure Portal > Function App > App keys > default key.
  Without `?code=...`, ACS mid-call events return HTTP 401 and the bot will answer
  calls but never process speech recognition or complete transfers.

Then seed:
```powershell
.\scripts\Set-AppConfiguration.ps1 `
    -AppConfigName "contoso-receptionist-config" `
    -ConfigFile    ".\config\appconfig-seed.json"
```

### 5 — Set Up Alerts
```powershell
.\scripts\Set-AlertRules.ps1 `
    -ResourceGroup      "rg-virtual-receptionist" `
    -AppInsightsName    "contoso-receptionist-ai" `
    -AlertEmailAddress  "it-alerts@contoso.com" `
    -TeamsWebhookUrl    "https://prod-xx.australiaeast.logic.azure.com/..."
```

The `-TeamsWebhookUrl` must be a Power Automate workflow URL.
Create it in Teams: channel > **...** > **Workflows** > **Post to a channel when a
webhook request is received**. Office 365 Connector URLs
(`*.webhook.office.com`) are deprecated and will stop working.

### 6 — Deploy Bot Code

Push to `main` branch — GitHub Actions handles deployment automatically.
Or run manually:
```powershell
.\scripts\Deploy-BotCode.ps1 `
    -FunctionAppName "contoso-receptionist" `
    -ResourceGroup   "rg-virtual-receptionist"
```

### 7 — Run Smoke Tests
```powershell
.\scripts\Test-EndToEnd.ps1 `
    -FunctionAppName "contoso-receptionist" `
    -ResourceGroup   "rg-virtual-receptionist"
```

Runs 8 automated checks including a validation that `acs_callback_url` contains
a function key and no placeholder text.

---

## Configuration Management

All customer-facing settings live in **Azure App Configuration** — no code changes or
redeployment needed. Changes are live within 5 minutes (config cache TTL).

Edit values at: Azure Portal > App Configuration > contoso-receptionist-config > Configuration Explorer

| Key | Description | Example |
|---|---|---|
| `receptionist:company_name` | Spoken in greeting | `Contoso Ltd` |
| `receptionist:voice_name` | Azure Neural TTS voice | `en-AU-NatashaNeural` |
| `receptionist:timezone` | IANA timezone | `Australia/Sydney` |
| `receptionist:match_threshold` | Fuzzy match % (0–100) | `65` |
| `receptionist:greeting_message` | Full greeting text | `Welcome to Contoso...` |
| `receptionist:noanswer_message` | Name not found message | `I couldn't find...` |
| `receptionist:afterhours_message` | Out of hours message | `Our office is closed...` |
| `receptionist:business_hours_mon` | Monday hours | `08:30-17:30` |
| `receptionist:business_hours_tue` | Tuesday hours | `08:30-17:30` |
| ... | ... | ... |
| `receptionist:business_hours_sat` | Saturday (blank = closed) | `` |
| `receptionist:default_reception_aad_id` | Fallback reception AAD Object ID | `xxxxxxxx-xxxx-...` |
| `receptionist:staff_group_id` | AD Security Group Object ID | `xxxxxxxx-xxxx-...` |
| `receptionist:acs_callback_url` | ACS mid-call event webhook **including `?code=`** | `https://...azurewebsites.net/api/acs_callback?code=...` |

---

## Name Pronunciation Overrides

For staff with names that Azure TTS mispronounces (e.g. "Hanson" → "handsome"), set a
phonetic override in their Azure AD profile:

1. Azure AD > Users > select user > Edit Properties
2. Set **extensionAttribute1** to the phonetic spelling: `HAN-son`
3. The bot uses this for TTS playback while still matching on the real display name

Via PowerShell:
```powershell
$User = Get-MgUser -Filter "displayName eq 'Hanson Lee'"
Update-MgUser -UserId $User.Id -OnPremisesExtensionAttributes @{extensionAttribute1 = "HAN-son"}
```

Takes effect within 5 minutes (Graph cache TTL).

---

## Known Limitations

**Scale-out and pending transfers**

`call_handler.py` uses a module-level dict (`_pending_transfers`) to track calls
between the "Connecting you to..." audio prompt and the actual transfer. This works
correctly when the Function App runs as a single instance. If the Consumption plan
scales out, a PlayCompleted callback may arrive on a different instance than the one
that queued the transfer, silently dropping it.

At receptionist call volumes (one concurrent call) this is not a practical issue.
For higher-volume deployments, replace `_pending_transfers` with an Azure Table
Storage row keyed on `callConnectionId`.

---

## Lifecycle Management

| Task | Script |
|---|---|
| Rotate client secret (every 24 months) | `.\scripts\Rotate-ClientSecret.ps1` |
| View recent call logs | `.\scripts\Get-CallLogs.ps1 -Hours 24` |
| Run smoke tests after deployment | `.\scripts\Test-EndToEnd.ps1` |
| Update App Configuration values | Azure Portal or `.\scripts\Set-AppConfiguration.ps1` |

---

## Alerting

Alerts fire to both email and a Teams channel (via Power Automate webhook) on:
- Repeated call transfer failures (>3 in 5 minutes)
- Speech recognition error rate >20%
- Function App exceptions
- Key Vault access failures
- High fallback-to-reception rate (>10 in 30 minutes)
- Client secret expiry (30 days warning)

---

## CI/CD

The GitHub Actions workflow (`deploy.yml`):
- Triggers on push to `main` affecting `bot/**`, or manually via workflow_dispatch
- Lints with flake8 before deploying
- Deploys using `Azure/functions-action` with `scm-do-build-during-deployment: true`
- Does **not** set `enable-oryx-build: true` — combining this with pre-resolved
  `.python_packages/` causes deployment failures
- Retrieves the function key via the **Kudu SCM API** (not `az functionapp keys list`,
  which can fail if the Functions runtime hasn't fully initialised post-deploy)
- Posts success/failure notifications to Teams via Power Automate webhook
