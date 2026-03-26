"""
config_loader.py
================
Loads all runtime configuration from Azure App Configuration.
Secrets (ACS connection string, client secret) are loaded from
Azure Key Vault via Managed Identity — never hardcoded.

Azure App Configuration endpoint is the ONLY value in an
environment variable (AZURE_APPCONFIG_ENDPOINT), set in the
Function App's Application Settings — not a secret.
"""

import os
import logging
from functools import lru_cache
from azure.appconfiguration import AzureAppConfigurationClient
from azure.identity import ManagedIdentityCredential, DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

logger = logging.getLogger(__name__)

# Keys expected in Azure App Configuration
REQUIRED_KEYS = [
    "receptionist:company_name",
    "receptionist:voice_name",
    "receptionist:timezone",
    "receptionist:greeting_message",
    "receptionist:noanswer_message",
    "receptionist:afterhours_message",
    "receptionist:match_threshold",
    "receptionist:staff_group_id",
    "receptionist:default_reception_aad_id",
    "receptionist:acs_callback_url",
    "receptionist:speech_language",
]

HOURS_KEYS = [
    "receptionist:business_hours_mon",
    "receptionist:business_hours_tue",
    "receptionist:business_hours_wed",
    "receptionist:business_hours_thu",
    "receptionist:business_hours_fri",
    "receptionist:business_hours_sat",
    "receptionist:business_hours_sun",
]

# Key Vault secret names (values stored in KV, names stored in App Config)
KV_SECRET_ACS_CONN   = "acs-connection-string"
KV_SECRET_CLIENT_ID  = "app-client-id"
KV_SECRET_CLIENT_SEC = "app-client-secret"


class ConfigLoader:
    """
    Loads and caches config from Azure App Configuration.
    Cache duration: 5 minutes (after which fresh values are loaded).
    This means App Config changes are live within 5 minutes — no redeploy.
    """

    _cache: dict = {}
    _cache_time: float = 0.0
    CACHE_TTL = 300  # seconds

    def __init__(self):
        self._endpoint = os.environ.get("AZURE_APPCONFIG_ENDPOINT")
        self._kv_url   = os.environ.get("AZURE_KEYVAULT_URL")

        if not self._endpoint:
            raise ValueError(
                "AZURE_APPCONFIG_ENDPOINT environment variable not set. "
                "Set this in Function App > Configuration > Application Settings."
            )
        if not self._kv_url:
            raise ValueError(
                "AZURE_KEYVAULT_URL environment variable not set. "
                "Set this in Function App > Configuration > Application Settings."
            )

        # Use DefaultAzureCredential — works with Managed Identity in Azure
        # and falls back to az login / env vars for local development
        self._credential = DefaultAzureCredential()
        self._refresh_if_stale()

    def _refresh_if_stale(self):
        import time
        now = time.time()
        if self._cache and (now - self._cache_time) < self.CACHE_TTL:
            return

        logger.info("Refreshing config from Azure App Configuration...")
        client = AzureAppConfigurationClient(
            base_url=self._endpoint,
            credential=self._credential,
        )

        fresh = {}
        for setting in client.list_configuration_settings(key_filter="receptionist:*"):
            fresh[setting.key] = setting.value or ""

        self._cache      = fresh
        self._cache_time = now
        logger.info("Config loaded — %d keys", len(fresh))

    def get(self, key: str, default: str = "") -> str:
        self._refresh_if_stale()
        val = self._cache.get(key, default)
        if not val and key in REQUIRED_KEYS:
            logger.warning("Config key '%s' is empty or missing", key)
        return val

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get(key, str(default)))
        except ValueError:
            return default

    def get_business_hours(self) -> dict:
        """
        Returns dict mapping day name to (start, end) tuple or None if closed.
        Format in App Config: "08:30-17:30" or "" for closed.
        """
        day_map = {
            "monday":    "receptionist:business_hours_mon",
            "tuesday":   "receptionist:business_hours_tue",
            "wednesday": "receptionist:business_hours_wed",
            "thursday":  "receptionist:business_hours_thu",
            "friday":    "receptionist:business_hours_fri",
            "saturday":  "receptionist:business_hours_sat",
            "sunday":    "receptionist:business_hours_sun",
        }
        result = {}
        for day, key in day_map.items():
            val = self.get(key, "").strip()
            if val and "-" in val:
                parts = val.split("-")
                result[day] = (parts[0].strip(), parts[1].strip())
            else:
                result[day] = None
        return result

    # ── Secret accessors (Key Vault via Managed Identity) ─────

    def _kv_client(self) -> SecretClient:
        return SecretClient(vault_url=self._kv_url, credential=self._credential)

    def get_acs_connection_string(self) -> str:
        return self._kv_client().get_secret(KV_SECRET_ACS_CONN).value

    def get_graph_credentials(self) -> tuple[str, str]:
        """Returns (client_id, client_secret)"""
        kv = self._kv_client()
        return (
            kv.get_secret(KV_SECRET_CLIENT_ID).value,
            kv.get_secret(KV_SECRET_CLIENT_SEC).value,
        )
