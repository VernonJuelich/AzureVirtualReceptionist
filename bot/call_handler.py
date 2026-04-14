"""
call_handler.py
===============
Orchestrates the full call flow.

Key fixes:
  - answer_call() now returns CallConnectionProperties, so we resolve the
    CallConnectionClient via get_call_connection(call_connection_id) before
    attempting media/recognize/transfer actions.
  - Pending transfer state is stored durably in Azure Table Storage so the
    PlayCompleted callback can complete the transfer even if it lands on a
    different Function instance.
  - Added support for phone number caller identifiers for speech recognition.
  - Added cognitive_services_endpoint support for speech recognition.
"""

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
            start = dtime(sh, sm)
            end = dtime(eh, em)

            # Normal same-day window (e.g., 09:00-17:00)
            if start <= end:
                return start <= now.time() <= end

            # Overnight window crossing midnight (e.g., 22:00-06:00)
            return now.time() >= start or now.time() <= end
        except Exception:
            logger.warning("Failed to parse business hours for %s — treating as closed", day)
            return False

    async def handle_incoming(self, data: dict):
        ctx = data.get("incomingCallContext", "")
        callback_url = self.config.get("receptionist:acs_callback_url")
        voice = self.config.get("receptionist:voice_name")
        speech_lang = self.config.get("receptionist:speech_language", "en-AU")
        cognitive_services_endpoint = self.config.get("receptionist:cognitive_services_endpoint")

        correlation_id = data.get("correlationId", "unknown")
        logger.info("Handling incoming call (correlationId=%s)", correlation_id)

        caller_id = self._extract_caller_id(data)

        client = self._acs()
        answer_result = client.answer_call(
            incoming_call_context=ctx,
            callback_url=callback_url,
            cognitive_services_endpoint=cognitive_services_endpoint,
        )
        call_connection_id = answer_result.call_connection_id
        conn = client.get_call_connection(call_connection_id)

        if not self._is_open():
            afterhours_msg = self.config.get("receptionist:afterhours_message")
            conn.play_media_to_all(
                self._tts(afterhours_msg),
                operation_context="afterhours_message",
            )
            return

        greeting = self.config.get("receptionist:greeting_message")
        greeting_ssml = build_ssml_message(greeting, voice)

        conn.start_recognizing_media(
            input_type="speech",
            target_participant=caller_id,
            play_prompt=self._ssml(greeting_ssml),
            interrupt_prompt=True,
            speech_language=speech_lang,
            end_silence_timeout=2,
            initial_silence_timeout=10,
            operation_context="attempt:1",
        )

    def _extract_caller_id(self, data: dict):
        try:
            from_obj = data.get("from", {}) or {}
            kind = (from_obj.get("kind") or "").strip()

            if kind == "communicationUser":
                raw_id = from_obj.get("rawId") or from_obj.get("id")
                if raw_id:
                    return CommunicationUserIdentifier(raw_id)

            if kind == "phoneNumber":
                phone_number = ""
                phone_data = from_obj.get("phoneNumber") or {}
                if isinstance(phone_data, dict):
                    phone_number = phone_data.get("value", "")
                phone_number = phone_number or from_obj.get("rawId", "")
                if phone_number:
                    return PhoneNumberIdentifier(phone_number)

            if kind == "microsoftTeamsUser":
                teams_user_id = from_obj.get("rawId") or from_obj.get("userId") or from_obj.get("id")
                if teams_user_id:
                    return MicrosoftTeamsUserIdentifier(user_id=teams_user_id)

            logger.warning("Unsupported caller kind '%s' — target_participant will be None", kind)
            return None
        except Exception as exc:
            logger.warning("Could not extract caller ID: %s", exc)
            return None

    async def handle_callback(self, event: dict):
        event_type = event.get("type", "")
        data = event.get("data", {})
        call_id = data.get("callConnectionId", "")
        op_context = data.get("operationContext", "")

        if not call_id:
            logger.warning("Ignoring callback '%s' with missing callConnectionId", event_type)
            return

        if event_type in (
            "Microsoft.Communication.CallTransferAccepted",
            "Microsoft.Communication.CallDisconnected",
        ):
            self._pending_store.delete(call_id)
            logger.info("%s (call_id=%s)", event_type.split(".")[-1], call_id)
            return

        client = self._acs()
        conn = client.get_call_connection(call_id)

        if event_type == "Microsoft.Communication.RecognizeCompleted":
            await self._on_speech_recognised(conn, data, op_context)
        elif event_type == "Microsoft.Communication.RecognizeFailed":
            await self._on_speech_failed(conn, call_id, op_context)
        elif event_type == "Microsoft.Communication.PlayCompleted":
            await self._on_play_completed(conn, call_id, op_context)
        elif event_type == "Microsoft.Communication.CallTransferFailed":
            await self._on_transfer_failed(conn, call_id, data, op_context)

    async def _on_play_completed(self, conn, call_id: str, op_context: str):
        logger.info("PlayCompleted: op_context=%s", op_context)

        if op_context in ("afterhours_message", "terminal_fallback"):
            logger.info("%s complete — hanging up", op_context)
            try:
                conn.hang_up(is_for_everyone=True)
            except Exception as exc:
                logger.warning("Hang up failed: %s", exc)

        elif op_context == "pre_transfer":
            pending = self._pending_store.get(call_id)
            if pending:
                logger.info("Initiating transfer to %s", pending["display_name"])
                self._do_transfer(
                    conn,
                    pending["aad_id"],
                    pending["display_name"],
                    is_fallback=False,
                )
            else:
                logger.warning(
                    "No pending transfer found for call_id=%s — routing to reception",
                    call_id,
                )
                reception_id = self.config.get("receptionist:default_reception_aad_id")
                self._do_transfer(conn, reception_id, "Reception", is_fallback=True)

        elif op_context == "pre_fallback":
            logger.info("Transferring to reception")
            reception_id = self.config.get("receptionist:default_reception_aad_id")
            self._do_transfer(conn, reception_id, "Reception", is_fallback=True)

    async def _on_speech_recognised(self, conn, data: dict, op_context: str):
        speech_result = data.get("speechResult", {})
        spoken = (speech_result.get("speech") or "").strip()

        logger.info("Speech recognised (length=%d chars, op_context=%s)", len(spoken), op_context)

        if not spoken:
            await self._on_speech_failed(conn, data.get("callConnectionId", ""), op_context)
            return

        try:
            tenant_id, client_id, client_secret = self.config.get_graph_credentials()
            group_id = self.config.get("receptionist:staff_group_id")
            staff_list = await get_staff_members(tenant_id, client_id, client_secret, group_id)
        except DirectoryUnavailableError:
            logger.error("Staff directory unavailable — routing to reception")
            conn.play_media_to_all(
                self._tts(
                    "I'm sorry, our directory is currently unavailable. "
                    "Let me transfer you to reception."
                ),
                operation_context="pre_fallback",
            )
            return

        threshold = self.config.get_int("receptionist:match_threshold", 65)
        matcher = NameMatcher(threshold=threshold)
        result = matcher.match(spoken, staff_list)
        voice = self.config.get("receptionist:voice_name")
        call_id = data.get("callConnectionId", "")

        if result.found:
            if not _is_valid_aad_id(result.staff.aad_id):
                logger.error("Invalid AAD Object ID for '%s'", result.staff.display_name)
                conn.play_media_to_all(
                    self._tts(
                        "I'm sorry, I'm unable to connect that call. "
                        "Let me transfer you to reception."
                    ),
                    operation_context="pre_fallback",
                )
                return

            self._pending_store.save(
                call_connection_id=call_id,
                aad_id=result.staff.aad_id,
                display_name=result.staff.display_name,
            )
            ssml = build_ssml_transfer_message(result.staff, voice)
            conn.play_media_to_all(
                self._ssml(ssml),
                operation_context="pre_transfer",
            )
            logger.info(
                "Queued transfer to '%s' via %s (score=%.1f)",
                result.staff.display_name,
                result.strategy,
                result.score,
            )
        else:
            noanswer = self.config.get("receptionist:noanswer_message")
            conn.play_media_to_all(
                self._tts(noanswer),
                operation_context="pre_fallback",
            )
            logger.info("No match found — queued fallback to reception")

    async def _on_speech_failed(self, conn, call_id: str, op_context: str):
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
                    "Please say the full name of the person you would like to speak to."
                ),
                interrupt_prompt=True,
                speech_language=speech_lang,
                end_silence_timeout=2,
                initial_silence_timeout=10,
                operation_context="attempt:2",
            )
        else:
            logger.info("Speech not recognised after 2 attempts — routing to reception")
            conn.play_media_to_all(
                self._tts("I'm unable to understand. Let me connect you to our reception team."),
                operation_context="pre_fallback",
            )

    async def _on_transfer_failed(self, conn, call_id: str, data: dict, op_context: str):
        reason = data.get("resultInformation", {}).get("message", "unknown")
        logger.error("Transfer failed (op_context=%s, reason=%s)", op_context, reason)
        self._pending_store.delete(call_id)

        if op_context == "fallback_transfer":
            conn.play_media_to_all(
                self._tts(
                    "I'm sorry, we are unable to connect your call at this time. "
                    "Please try again shortly."
                ),
                operation_context="terminal_fallback",
            )
        else:
            conn.play_media_to_all(
                self._tts("That extension is currently unavailable. Transferring you to reception."),
                operation_context="pre_fallback",
            )

    def _do_transfer(self, conn, aad_object_id: str, display_name: str, is_fallback: bool = False):
        if not _is_valid_aad_id(aad_object_id):
            logger.error(
                "Transfer aborted — invalid AAD Object ID for '%s': '%s'",
                display_name,
                aad_object_id,
            )
            conn.play_media_to_all(
                self._tts("I'm sorry, I'm unable to complete that transfer."),
                operation_context="terminal_fallback",
            )
            return

        target = MicrosoftTeamsUserIdentifier(user_id=aad_object_id)
        op_ctx = "fallback_transfer" if is_fallback else "primary_transfer"
        try:
            conn.transfer_call_to_participant(
                target_participant=target,
                operation_context=op_ctx,
            )
            logger.info("Transfer initiated → '%s' (%s)", display_name, aad_object_id)
        except Exception as exc:
            logger.error("Transfer initiation exception for '%s': %s", display_name, exc)
            raise
