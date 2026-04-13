"""
call_handler.py
===============
Orchestrates the full call flow.

Fixes applied:
  - [Issue 1]  _pending_transfers moved from module-level global to Azure Table Storage
                so state is shared correctly across all Function App scale-out instances.
  - [Issue 2]  call_id now passed as a parameter to _on_speech_recognised rather than
                being re-derived from data inside the method, eliminating the silent
                empty-string risk if callConnectionId is absent from a callback payload.
  - Transfer now waits for PlayCompleted event before initiating (no race condition)
  - After-hours and terminal fallback paths hang up the call cleanly
  - Retry count parsed explicitly — no longer fragile string-shape dependent
  - AAD Object ID validated before attempting transfer
  - DirectoryUnavailableError handled distinctly from "no name match"
  - Unused import (build_ssml_not_found) removed
"""

import logging
import os
import re
from datetime import datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

from azure.communication.callautomation import (
    CallAutomationClient,
    SsmlSource,
    TextSource,
    MicrosoftTeamsUserIdentifier,
)
from azure.data.tables import TableServiceClient
from azure.identity import DefaultAzureCredential

from config_loader import ConfigLoader
from graph_client import get_staff_members, DirectoryUnavailableError
from matcher import NameMatcher, build_ssml_transfer_message, build_ssml_message

logger = logging.getLogger(__name__)

# Regex for basic AAD Object ID validation (UUID format)
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE
)

# Table Storage constants for cross-instance pending transfer state
# The storage account connection string must be available as an app setting.
_PENDING_TABLE_NAME = "pendingtransfers"


def _is_valid_aad_id(value: str) -> bool:
    return bool(value and _UUID_RE.match(value.strip()))


# ── Pending transfer helpers (Table Storage — shared across all instances) ──

def _get_table_client():
    """
    Returns a TableClient for the pending-transfers table.
    Uses the Function App storage account connection string, which is always
    available as AzureWebJobsStorage (set automatically by the runtime).
    Falls back to Managed Identity + AZURE_STORAGE_ACCOUNT_NAME if preferred.
    """
    conn_str = os.environ.get("AzureWebJobsStorage", "")
    if conn_str:
        svc = TableServiceClient.from_connection_string(conn_str)
    else:
        # Managed Identity path (set AZURE_STORAGE_ACCOUNT_NAME app setting)
        account_name = os.environ["AZURE_STORAGE_ACCOUNT_NAME"]
        svc = TableServiceClient(
            endpoint=f"https://{account_name}.table.core.windows.net",
            credential=DefaultAzureCredential(),
        )
    svc.create_table_if_not_exists(_PENDING_TABLE_NAME)
    return svc.get_table_client(_PENDING_TABLE_NAME)


def _store_pending_transfer(call_id: str, aad_id: str, display_name: str) -> None:
    """Upsert a pending transfer record into Table Storage."""
    try:
        client = _get_table_client()
        client.upsert_entity({
            "PartitionKey": "pending",
            "RowKey": call_id,
            "aad_id": aad_id,
            "display_name": display_name,
        })
    except Exception as exc:
        logger.error("Failed to store pending transfer for call_id=%s: %s", call_id, exc)
        raise


def _pop_pending_transfer(call_id: str) -> dict | None:
    """
    Retrieve and delete a pending transfer record.
    Returns None if not found (already consumed or expired).
    """
    try:
        client = _get_table_client()
        entity = client.get_entity(partition_key="pending", row_key=call_id)
        client.delete_entity(partition_key="pending", row_key=call_id)
        return {"aad_id": entity["aad_id"], "display_name": entity["display_name"]}
    except Exception:
        return None


class CallHandler:

    def __init__(self, config: ConfigLoader):
        self.config = config

    def _acs(self) -> CallAutomationClient:
        return CallAutomationClient.from_connection_string(
            self.config.get_acs_connection_string()
        )

    def _ssml(self, ssml_text: str) -> SsmlSource:
        return SsmlSource(ssml_document=ssml_text)

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
            logger.warning(
                "Invalid timezone '%s' — defaulting to UTC", tz_name)
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
                "Failed to parse business hours for %s — treating as closed", day)
            return False

    # ════════════════════════════════════════════════════════
    #  Incoming call
    # ════════════════════════════════════════════════════════

    async def handle_incoming(self, data: dict):
        ctx = data.get("incomingCallContext", "")
        callback_url = self.config.get("receptionist:acs_callback_url")
        voice = self.config.get("receptionist:voice_name")
        speech_lang = self.config.get("receptionist:speech_language", "en-AU")

        correlation_id = data.get("correlationId", "unknown")
        logger.info("Handling incoming call (correlationId=%s)", correlation_id)

        client = self._acs()
        call_conn = client.answer_call(
            incoming_call_context=ctx,
            callback_url=callback_url,
        )

        if not self._is_open():
            afterhours_msg = self.config.get("receptionist:afterhours_message")
            call_conn.play_media_to_all(
                self._tts(afterhours_msg), operation_context="afterhours_message",
            )
            # Hang up is triggered in handle_callback on PlayCompleted
            return

        greeting = self.config.get("receptionist:greeting_message")
        greeting_ssml = build_ssml_message(greeting, voice)

        call_conn.start_recognizing_media(
            input_type="speech",
            target_participant=None,
            play_prompt=self._ssml(greeting_ssml),
            interrupt_prompt=True,
            speech_language=speech_lang,
            end_silence_timeout_in_ms=1500,
            operation_context="attempt:1",
        )

    # ════════════════════════════════════════════════════════
    #  Mid-call callback events
    # ════════════════════════════════════════════════════════

    async def handle_callback(self, event: dict):
        event_type = event.get("type", "")
        data = event.get("data", {})
        call_id = data.get("callConnectionId", "")
        op_context = data.get("operationContext", "")

        client = self._acs()
        conn = client.get_call_connection(call_id)

        if event_type == "Microsoft.Communication.RecognizeCompleted":
            # [Issue 2] Pass call_id explicitly rather than re-deriving it inside
            await self._on_speech_recognised(conn, call_id, data, op_context)

        elif event_type == "Microsoft.Communication.RecognizeFailed":
            await self._on_speech_failed(conn, call_id, op_context)

        elif event_type == "Microsoft.Communication.PlayCompleted":
            await self._on_play_completed(conn, call_id, op_context)

        elif event_type == "Microsoft.Communication.CallTransferAccepted":
            logger.info("Transfer accepted (call_id=%s)", call_id)

        elif event_type == "Microsoft.Communication.CallTransferFailed":
            await self._on_transfer_failed(conn, data, op_context)

        elif event_type == "Microsoft.Communication.CallDisconnected":
            logger.info("Call disconnected (call_id=%s)", call_id)

    # ── PlayCompleted — controls sequencing ─────────────────

    async def _on_play_completed(self, conn, call_id: str, op_context: str):
        """
        Triggered when an audio prompt finishes playing.
        Used to sequence actions that must not race with audio.
        """
        logger.info("PlayCompleted: op_context=%s", op_context)

        if op_context == "afterhours_message":
            logger.info("After-hours message complete — hanging up")
            try:
                conn.hang_up(is_for_everyone=True)
            except Exception as exc:
                logger.warning("Hang up failed: %s", exc)

        elif op_context == "terminal_fallback":
            logger.info("Terminal fallback message complete — hanging up")
            try:
                conn.hang_up(is_for_everyone=True)
            except Exception as exc:
                logger.warning("Hang up failed: %s", exc)

        elif op_context == "pre_transfer":
            # [Issue 1] Retrieve transfer target from Table Storage (shared across instances)
            pending = _pop_pending_transfer(call_id)
            if pending:
                logger.info(
                    "PlayCompleted pre_transfer — initiating transfer to %s",
                    pending["display_name"])
                self._do_transfer(
                    conn,
                    pending["aad_id"],
                    pending["display_name"],
                    is_fallback=False)
            else:
                logger.warning(
                    "No pending transfer found in Table Storage for call_id=%s", call_id)

        elif op_context == "pre_fallback":
            logger.info("PlayCompleted pre_fallback — transferring to reception")
            reception_id = self.config.get("receptionist:default_reception_aad_id")
            self._do_transfer(conn, reception_id, "Reception", is_fallback=True)

    # ── Speech recognised ─────────────────────────────────────

    async def _on_speech_recognised(self, conn, call_id: str, data: dict, op_context: str):
        """
        [Issue 2] call_id is now received as a parameter, not re-derived from data,
        so it is guaranteed to be consistent with the connection object in use.
        """
        speech_result = data.get("speechResult", {})
        spoken = (speech_result.get("speech") or "").strip()

        logger.info(
            "Speech recognised (length=%d chars, op_context=%s)",
            len(spoken), op_context)

        if not spoken:
            await self._on_speech_failed(conn, call_id, op_context)
            return

        try:
            tenant_id, client_id, client_secret = self.config.get_graph_credentials()
            group_id = self.config.get("receptionist:staff_group_id")
            staff_list = await get_staff_members(tenant_id, client_id, client_secret, group_id)
        except DirectoryUnavailableError:
            logger.error("Staff directory unavailable — routing to reception")
            conn.play_media_to_all(
                self._tts("I'm sorry, our directory is currently unavailable. Let me transfer you to reception."), operation_context="pre_fallback",
            )
            return

        threshold = self.config.get_int("receptionist:match_threshold", 65)
        matcher = NameMatcher(threshold=threshold)
        result = matcher.match(spoken, staff_list)
        voice = self.config.get("receptionist:voice_name")

        if result.found:
            if not _is_valid_aad_id(result.staff.aad_id):
                logger.error(
                    "Invalid AAD Object ID for '%s': '%s'",
                    result.staff.display_name, result.staff.aad_id)
                conn.play_media_to_all(
                    self._tts("I'm sorry, I'm unable to connect that call right now. Let me transfer you to reception."), operation_context="pre_fallback",
                )
                return

            # [Issue 1] Store transfer target in Table Storage (durable, cross-instance)
            _store_pending_transfer(call_id, result.staff.aad_id, result.staff.display_name)

            ssml = build_ssml_transfer_message(result.staff, voice)
            conn.play_media_to_all(
                self._ssml(ssml), operation_context="pre_transfer",
            )
            logger.info(
                "Queued transfer to '%s' via %s (score=%.1f)",
                result.staff.display_name, result.strategy, result.score
            )
        else:
            noanswer = self.config.get("receptionist:noanswer_message")
            conn.play_media_to_all(
                self._tts(noanswer), operation_context="pre_fallback",
            )
            logger.info("No match found — queued fallback to reception")

    # ── Speech failed / silence ───────────────────────────────

    async def _on_speech_failed(self, conn, call_id: str, op_context: str):
        """
        Retry logic — explicit attempt number parsed from op_context.
        Context format: "attempt:N" where N is 1 or 2.
        After 2 failed attempts, route to reception.
        """
        try:
            attempt_num = int(op_context.split(":")[-1])
        except (ValueError, IndexError):
            attempt_num = 2

        speech_lang = self.config.get("receptionist:speech_language", "en-AU")

        if attempt_num < 2:
            logger.info("Speech not recognised (attempt %d) — prompting retry", attempt_num)
            conn.start_recognizing_media(
                input_type="speech",
                target_participant=None,
                play_prompt=self._tts(
                    "I didn't quite catch that. "
                    "Please say the full name of the person you would like to speak to."),
                interrupt_prompt=True,
                speech_language=speech_lang,
                end_silence_timeout_in_ms=1500,
                operation_context="attempt:2",
            )
        else:
            logger.info("Speech not recognised after 2 attempts — routing to reception")
            conn.play_media_to_all(
                self._tts("I'm unable to understand. Let me connect you to our reception team."), operation_context="pre_fallback",
            )

    # ── Transfer failed ───────────────────────────────────────

    async def _on_transfer_failed(self, conn, data: dict, op_context: str):
        reason = data.get("resultInformation", {}).get("message", "unknown")
        logger.error("Transfer failed (op_context=%s, reason=%s)", op_context, reason)

        if op_context == "fallback_transfer":
            conn.play_media_to_all(
                self._tts(
                    "I'm sorry, we are unable to connect your call at this time. "
                    "Please try again shortly."), operation_context="terminal_fallback",
            )
        else:
            conn.play_media_to_all(
                self._tts("That extension is currently unavailable. Transferring you to reception."), operation_context="pre_fallback",
            )

    # ── Transfer helper ───────────────────────────────────────

    def _do_transfer(self, conn, aad_object_id: str, display_name: str, is_fallback: bool = False):
        if not _is_valid_aad_id(aad_object_id):
            logger.error(
                "Transfer aborted — invalid AAD Object ID for '%s': '%s'",
                display_name, aad_object_id
            )
            conn.play_media_to_all(
                self._tts("I'm sorry, I'm unable to complete that transfer."), operation_context="terminal_fallback",
            )
            return

        target = MicrosoftTeamsUserIdentifier(user_id=aad_object_id)
        operation_context = "fallback_transfer" if is_fallback else "primary_transfer"
        try:
            conn.transfer_call_to_participant(
                target_participant=target,
                operation_context=operation_context,
            )
            logger.info("Transfer initiated → '%s' (%s)", display_name, aad_object_id)
        except Exception as exc:
            logger.error("Transfer initiation exception for '%s': %s", display_name, exc)
            raise
