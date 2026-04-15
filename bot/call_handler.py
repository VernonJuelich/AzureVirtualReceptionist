# Fixed call_handler.py (indentation corrected)

from __future__ import annotations

import logging
import re
from datetime import datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

from azure.communication.callautomation import (
    CallAutomationClient,
    CommunicationUserIdentifier,
    MicrosoftTeamsUserIdentifier,
    PhoneNumberIdentifier,
    SsmlSource,
    TextSource,
)

from config_loader import ConfigLoader
from graph_client import DirectoryUnavailableError, get_staff_members
from matcher import NameMatcher, build_ssml_message, build_ssml_transfer_message
from pending_transfer_store import PendingTransferStore

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _is_valid_aad_id(value: str) -> bool:
    return bool(value and _UUID_RE.match(value.strip()))


class CallHandler:
    def __init__(self, config: ConfigLoader):
        self.config = config
        self._pending_store = PendingTransferStore()

    def _acs(self) -> CallAutomationClient:
        return CallAutomationClient.from_connection_string(
            self.config.get_acs_connection_string()
        )

    def _ssml(self, ssml_text: str) -> SsmlSource:
        return SsmlSource(ssml_text=ssml_text)

    def _tts(self, text: str) -> TextSource:
        return TextSource(
            text=text,
            voice_name=self.config.get("receptionist:voice_name"),
        )

    def _is_open(self) -> bool:
        tz_name = self.config.get("receptionist:timezone", "UTC")
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            logger.warning("Invalid timezone '%s' — defaulting to UTC", tz_name)
            tz = ZoneInfo("UTC")

        now = datetime.now(tz)
        day = now.strftime("%A").lower()
        hours = self.config.get_business_hours().get(day)

        if not hours:
            return False

        try:
            sh, sm = map(int, hours[0].split(":"))
            eh, em = map(int, hours[1].split(":"))
            return dtime(sh, sm) <= now.time() <= dtime(eh, em)
        except Exception:
            logger.warning(
                "Failed to parse business hours for %s — treating as closed", day
            )
            return False
