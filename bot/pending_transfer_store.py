import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict

from azure.data.tables import TableServiceClient

logger = logging.getLogger(__name__)


class PendingTransferStore:
    TABLE_NAME = "pendingtransfers"
    PARTITION_KEY = "pending_transfer"

    def __init__(self, connection_string: Optional[str] = None):
        self._connection_string = (
            connection_string
            or os.environ.get("AzureWebJobsStorage", "").strip()
        )
        if not self._connection_string:
            raise EnvironmentError(
                "AzureWebJobsStorage is not set. It is required for durable pending transfer state."
            )
        self._service = TableServiceClient.from_connection_string(self._connection_string)
        self._table = self._service.get_table_client(self.TABLE_NAME)
        self._table.create_table_if_not_exists()

    def save(self, call_connection_id: str, aad_id: str, display_name: str, ttl_minutes: int = 30):
        expires_utc = (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat()
        entity = {
            "PartitionKey": self.PARTITION_KEY,
            "RowKey": call_connection_id,
            "aad_id": aad_id,
            "display_name": display_name,
            "expires_utc": expires_utc,
        }
        self._table.upsert_entity(mode="MERGE", entity=entity)
        logger.info("Saved pending transfer state for call_connection_id=%s", call_connection_id)

    def get(self, call_connection_id: str) -> Optional[Dict[str, str]]:
        try:
            entity = self._table.get_entity(
                partition_key=self.PARTITION_KEY,
                row_key=call_connection_id,
            )
        except Exception:
            return None

        expires_utc = entity.get("expires_utc")
        if expires_utc:
            try:
                expires = datetime.fromisoformat(expires_utc)
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires < datetime.now(timezone.utc):
                    logger.warning(
                        "Pending transfer state expired for call_connection_id=%s",
                        call_connection_id,
                    )
                    self.delete(call_connection_id)
                    return None
            except Exception as exc:
                logger.warning("Could not parse expires_utc for call_connection_id=%s: %s", call_connection_id, exc)

        return {
            "aad_id": entity.get("aad_id", ""),
            "display_name": entity.get("display_name", ""),
        }

    def delete(self, call_connection_id: str):
        try:
            self._table.delete_entity(
                partition_key=self.PARTITION_KEY,
                row_key=call_connection_id,
            )
            logger.info("Deleted pending transfer state for call_connection_id=%s", call_connection_id)
        except Exception:
            pass
