# Virtual Receptionist — Troubleshooting Guide

## Most important checks after applying the fix pack

1. `scripts/Set-AppConfiguration.ps1` completes successfully.
2. `receptionist:cognitive_services_endpoint` exists in App Configuration.
3. The Function App can read Key Vault and App Configuration.
4. The storage account has a `pendingtransfers` table after the first matched call.
5. Transfers complete even when the Function App has multiple instances.

## Quick diagnostics

```powershell
# Health check
$FnKey = az functionapp keys list --name "contoso-receptionist" --resource-group "rg-virtual-receptionist" --query "functionKeys.default" -o tsv
curl "https://contoso-receptionist.azurewebsites.net/api/health?code=$FnKey"

# Check pending transfer table exists
az storage table exists --name pendingtransfers --account-name YOUR-STORAGE-ACCOUNT

# View app configuration key
az appconfig kv show --name contoso-receptionist-config --key "receptionist:cognitive_services_endpoint"
```

## Common symptoms

### Bot answers, then nothing happens
- Usually means media actions were attempted on the wrong object after `answer_call()`.
- Confirm the deployed `bot/call_handler.py` is the fixed version.

### Bot speaks the transfer message but never transfers
- Check the `pendingtransfers` table.
- If the entity is missing before `PlayCompleted`, the wrong code is deployed.

### Speech recognition always fails
- Confirm `receptionist:cognitive_services_endpoint` is populated.
- Confirm the Azure AI Services resource exists and the endpoint is valid.
- Confirm the speech language and voice locale are compatible.

### Everyone routes to reception
- Check Graph application permissions and admin consent.
- Check the staff group actually contains users.
- Check `receptionist:staff_group_id` is not still a placeholder.
