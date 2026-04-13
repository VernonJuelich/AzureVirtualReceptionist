# Azure Virtual Receptionist

Speech-driven call routing for Microsoft Teams using Azure Communication Services.

Callers speak a staff member's name → Azure AI Speech transcribes it → phonetic + fuzzy match against Azure AD Security Group → call transferred via Teams Direct Routing.

---

## Repo Structure

```
├── .github/
│   └── workflows/
│       └── deploy.yml          # GitHub Actions — deploy bot to Azure Function App
├── bot/
│   ├── function_app.py         # Azure Functions v2 entry point (HTTP triggers)
│   ├── call_handler.py         # ACS call orchestration and event handling
│   ├── matcher.py              # Phonetic + fuzzy name matching engine
│   ├── config_loader.py        # Azure App Configuration reader
│   ├── graph_client.py         # Microsoft Graph API — AD group member lookup
│   ├── host.json               # Azure Functions host configuration
│   └── requirements.txt        # Python dependencies
├── scripts/
│   ├── Deploy-Infrastructure.ps1   # Full infrastructure deployment
│   ├── Deploy-BotCode.ps1          # Deploy Python code to Function App
│   ├── New-TeamsResourceAccount.ps1# Teams resource account + ACS linking (PowerShell)
│   ├── Set-AppConfiguration.ps1    # Seed/update Azure App Configuration values
│   ├── Set-AlertRules.ps1          # Create Azure Monitor + Teams webhook alerts
│   ├── Get-CallLogs.ps1            # Query App Insights call logs
│   ├── Test-EndToEnd.ps1           # Automated post-deploy smoke tests
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
- Azure CLI: `az login`
- Teams PowerShell: `Install-Module MicrosoftTeams`
- Python 3.11 + Azure Functions Core Tools
- A GitHub repo with these secrets set (Settings > Secrets > Actions):

| Secret name | Value |
|---|---|
| `AZURE_CREDENTIALS` | Service principal JSON from `az ad sp create-for-rbac` |
| `AZURE_SUBSCRIPTION_ID` | Your Azure subscription ID |
| `AZURE_RESOURCE_GROUP` | e.g. `rg-virtual-receptionist` |
| `FUNCTION_APP_NAME` | e.g. `contoso-receptionist` |

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

### 4 — Seed App Configuration
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
    -TeamsWebhookUrl    "https://contoso.webhook.office.com/..."
```

### 6 — Deploy Bot Code
Push to `main` branch — GitHub Actions handles deployment automatically.
Or run manually:
```powershell
.\scripts\Deploy-BotCode.ps1 -FunctionAppName "contoso-receptionist" -ResourceGroup "rg-virtual-receptionist"
```

---

## Configuration Management

All customer-facing settings live in **Azure App Configuration** — no code changes or redeployment needed.

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

---

## Name Pronunciation Overrides

For staff with names that Azure TTS mispronounces (e.g. "Hanson" → "handsome"), set a phonetic override in their Azure AD profile:

1. Azure AD > Users > select user > Edit Properties > Customize
2. Set **extensionAttribute1** to the phonetic spelling: `HAN-son`
3. The bot will use this for TTS playback while still matching on the real display name

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

Alerts fire to both email and a Teams channel webhook on:
- Repeated call transfer failures (>3 in 5 minutes)
- Speech recognition error rate >20%
- Function App exceptions
- Key Vault access failures
- Client secret expiry (30 days warning)
