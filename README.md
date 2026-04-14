# Azure Virtual Receptionist

Speech-driven call routing for Microsoft Teams using Azure Communication Services.

## Fixed files in this pack

- `bot/call_handler.py`
- `bot/config_loader.py`
- `bot/graph_client.py`
- `bot/pending_transfer_store.py` (new)
- `bot/requirements.txt`
- `scripts/Set-AppConfiguration.ps1`
- `scripts/Deploy-Infrastructure.ps1`
- `config/appconfig-seed.json`
- `docs/troubleshooting.md`

## Key changes

- Uses `get_call_connection(call_connection_id)` after `answer_call()` before any media actions.
- Stores pending transfer state in Azure Table Storage so callbacks work across scaled-out Function instances.
- Adds `receptionist:cognitive_services_endpoint` for ACS speech recognition.
- Replaces brittle Graph SDK paging with a direct Microsoft Graph HTTPS client.
- Fixes the broken App Configuration seeding script.
- Updates infrastructure deployment to create an Azure AI Services resource and seed its endpoint.

## Required GitHub secrets

| Secret name | Value |
|---|---|
| `AZURE_CREDENTIALS` | Service principal JSON from `az ad sp create-for-rbac --sdk-auth` |
| `AZURE_RESOURCE_GROUP` | Your Azure resource group |
| `FUNCTION_APP_NAME` | Your Function App name |
| `TEAMS_WEBHOOK_URL` | Optional Teams incoming webhook URL |

## Notes

- The Function App still relies on its standard `AzureWebJobsStorage` setting for host storage and for the durable `pendingtransfers` table.
- After deploying the fixed files, re-run `scripts/Test-EndToEnd.ps1` and make a live call test.
