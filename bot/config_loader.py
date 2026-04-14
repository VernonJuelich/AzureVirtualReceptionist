"""
config_loader.py
================
Loads all runtime configuration from Azure App Configuration.
Secrets are pulled from Azure Key Vault via Managed Identity.
"""

import logging
import os
import re
import time
from datetime import time as dtime

from azure.appconfiguration import AzureAppConfigurationClient
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

logger = logging.getLogger(__name__)

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
    "receptionist:tenant_id",
    "receptionist:cognitive_services_endpoint",
]

HOURS_PATTERN = re.compile(r"^\d{2}:\d{2}-\d{2}:\d{2}$")

KV_ACS_CONN_STRING = "acs-connection-string"
KV_CLIENT_ID = "app-client-id"
KV_CLIENT_SECRET = "app-client-secret"


class ConfigLoader:
    CACHE_TTL = 300

    def __init__(self):
        self._cache: dict = {}
        self._cache_time: float = 0.0

        endpoint = os.environ.get("AZURE_APPCONFIG_ENDPOINT", "").strip()
        kv_url = os.environ.get("AZURE_KEYVAULT_URL", "").strip()

        if not endpoint:
            raise EnvironmentError(
                "AZURE_APPCONFIG_ENDPOINT is not set. Add it in Function App configuration."
            )
        if not kv_url:
            raise EnvironmentError(
                "AZURE_KEYVAULT_URL is not set. Add it in Function App configuration."
            )

        self._endpoint = endpoint
        self._kv_url = kv_url
        self._credential = DefaultAzureCredential()
        self._refresh_if_stale()

    def _refresh_if_stale(self):
        now = time.time()
        if self._cache and (now - self._cache_time) < self.CACHE_TTL:
            return

        logger.info("Refreshing config from Azure App Configuration...")
        try:
            client = AzureAppConfigurationClient(
                base_url=self._endpoint,
                credential=self._credential,
            )
            fresh = {}
            for setting in client.list_configuration_settings(key_filter="receptionist:*"):
                fresh[setting.key] = setting.value or ""

            for key in REQUIRED_KEYS:
                if key not in fresh or not fresh[key]:
                    logger.warning("Required config key missing or empty: %s", key)

            self._cache = fresh
            self._cache_time = now
            logger.info("Config refreshed — %d keys loaded", len(fresh))
        except Exception as exc:
            logger.error("Failed to refresh App Configuration: %s", exc)
            if not self._cache:
                raise
            logger.warning("Using stale config cache due to refresh failure")

    def get(self, key: str, default: str = "") -> str:
        self._refresh_if_stale()
        return self._cache.get(key, default)

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get(key, str(default)))
        except ValueError:
            logger.warning("Config key '%s' is not a valid integer — using default %d", key, default)
            return default

    def get_business_hours(self) -> dict:
        day_map = {
            "monday": "receptionist:business_hours_mon",
            "tuesday": "receptionist:business_hours_tue",
            "wednesday": "receptionist:business_hours_wed",
            "thursday": "receptionist:business_hours_thu",
            "friday": "receptionist:business_hours_fri",
            "saturday": "receptionist:business_hours_sat",
            "sunday": "receptionist:business_hours_sun",
        }
        result = {}
        for day, key in day_map.items():
            val = self.get(key, "").strip()
            if not val:
                result[day] = None
                continue
            if not HOURS_PATTERN.match(val):
                logger.warning(
                    "Invalid business hours format for %s: '%s' — expected HH:MM-HH:MM. Treating as closed.",
                    day,
                    val,
                )
                result[day] = None
                continue
            try:
                start_str, end_str = val.split("-", maxsplit=1)
                sh, sm = map(int, start_str.split(":"))
                eh, em = map(int, end_str.split(":"))
                dtime(sh, sm)
                dtime(eh, em)
                result[day] = (start_str, end_str)
            except ValueError:
                logger.warning(
                    "Could not parse business hours for %s: '%s'. Treating as closed.",
                    day,
                    val,
                )
                result[day] = None
        return result

    def _kv(self) -> SecretClient:
        return SecretClient(vault_url=self._kv_url, credential=self._credential)

    def get_acs_connection_string(self) -> str:
        return self._kv().get_secret(KV_ACS_CONN_STRING).value

    def get_graph_credentials(self) -> tuple:
        kv = self._kv()
        return (
            self.get("receptionist:tenant_id"),
            kv.get_secret(KV_CLIENT_ID).value,
            kv.get_secret(KV_CLIENT_SECRET).value,
        )
