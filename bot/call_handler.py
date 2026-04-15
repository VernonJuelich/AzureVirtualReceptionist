"""
call_handler.py
===============
Orchestrates the full ACS call flow.

What this version fixes:
  - Resolves a real CallConnectionClient after answer_call(); answer_call()
    returns CallConnectionProperties, not a usable connection client.
  - Passes cognitive_services_endpoint during answer so speech recognition is
    wired correctly for ACS recognize flows.
  - Persists pending transfer state in Azure Table Storage so PlayCompleted can
    complete the transfer even when callback events land on another Function
    instance.
  - Handles ACS / PSTN / Teams caller identifiers more defensively.
  - Avoids hard failure when caller identity cannot be reconstructed by falling
    back to a best-effort participant lookup before recognition retries.
"""

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

    async def handle_incoming(self, data: dict):
        incoming_call_context = data.get("incomingCallContext", "")
        if not incoming_call_context:
            raise ValueError("Incoming call payload missing incomingCallContext")

        callback_url = self.config.get("receptionist:acs_callback_url")
        if not callback_url:
            raise ValueError("Missing config key receptionist:acs_callback_url")

        cognitive_services_endpoint = self.config.get(
            "receptionist:cognitive_services_endpoint"
        )

        correlation_id = data.get("correlationId", "unknown")
        logger.info("Handling incoming call (correlationId=%s)", correlation_id)

        client = self._acs()
        answer_result = client.answer_call(
            incoming_call_context=incoming_call_context,
            callback_url=callback_url,
            cognitive_services_endpoint=cognitive_services_endpoint or None,
            operation_context=f"answer:{correlation_id}",
        )
        call_connection_id = answer_result.call_connection_id

        logger.info("Call answered (call_connection_id=%s)", call_connection_id)
        # Greeting and recognition are started in _on_call_connected,
        # triggered by the CallConnected callback event from ACS.

    def _extract_caller_id(self, from_obj: dict):
        try:
            kind = (from_obj.get("kind") or "").strip()

            if kind == "communicationUser":
                comm_user = from_obj.get("communicationUser") or {}
                comm_id = (
                    comm_user.get("id")
                    or from_obj.get("id")
                    or from_obj.get("rawId")
                )
                if comm_id:
                    return CommunicationUserIdentifier(comm_id)

            if kind == "phoneNumber":
                phone_obj = from_obj.get("phoneNumber") or {}
                phone_number = phone_obj.get("value") or from_obj.get("id") or ""
                if not phone_number:
                    raw_id = (from_obj.get("rawId") or "").strip()
                    if raw_id.startswith("4:"):
                        phone_number = raw_id[2:]
                if phone_number:
                    return PhoneNumberIdentifier(phone_number)

            if kind == "microsoftTeamsUser":
                teams_obj = from_obj.get("microsoftTeamsUser") or {}
                user_id = teams_obj.get("userId") or from_obj.get("userId") or from_obj.get("id")
                if user_id:
                    return MicrosoftTeamsUserIdentifier(user_id=user_id)

            logger.warning("Unsupported caller identifier kind '%s'", kind)
            return None
        except Exception as exc:
            logger.warning("Could not extract caller ID from incoming event: %s", exc)
            return None

    def _best_effort_target_participant(self, conn):
        """
        Best-effort participant discovery for cases where the incoming event does
        not deserialize into a usable identifier shape.

        We avoid picking a Teams app / unknown identifier here and prefer a real
        user/phone/ACS participant if one is visible on the call.
        """
        try:
            participants = list(conn.list_participants())
        except Exception as exc:
            logger.warning("Participant lookup failed: %s", exc)
            return None

        for participant in participants:
            identifier = getattr(participant, "identifier", None)
            if identifier is None:
                continue

            kind = getattr(identifier, "kind", "") or type(identifier).__name__
            logger.info("Found participant candidate for recognition: %s", kind)

            if isinstance(
                identifier,
                (
                    CommunicationUserIdentifier,
                    PhoneNumberIdentifier,
                    MicrosoftTeamsUserIdentifier,
                ),
            ):
                return identifier

        logger.warning("No usable participant found for speech recognition")
        return None

    async def handle_callback(self, event: dict):
        event_type = event.get("type", "")
        data = event.get("data", {})
        call_id = data.get("callConnectionId", "")
        op_context = data.get("operationContext", "")

        if not call_id:
            logger.warning("Callback missing callConnectionId for event %s", event_type)
            return

        conn = self._acs().get_call_connection(call_id)

        if event_type == "Microsoft.Communication.RecognizeCompleted":
            await self._on_speech_recognised(conn, data, op_context)
        elif event_type == "Microsoft.Communication.RecognizeFailed":
            await self._on_speech_failed(conn, call_id, op_context)
        elif event_type == "Microsoft.Communication.PlayCompleted":
            await self._on_play_completed(conn, call_id, op_context)
        elif event_type == "Microsoft.Communication.CallTransferAccepted":
            self._pending_store.delete(call_id)
            logger.info("Transfer accepted (call_id=%s)", call_id)
        elif event_type == "Microsoft.Communication.CallTransferFailed":
            await self._on_transfer_failed(conn, call_id, data, op_context)
        elif event_type == "Microsoft.Communication.CallDisconnected":
            self._pending_store.delete(call_id)
            logger.info("Call disconnected (call_id=%s)", call_id)
        elif event_type == "Microsoft.Communication.CallConnected":
            await self._on_call_connected(conn, call_id, data)

        else:
            logger.info("Ignoring unsupported callback event type: %s", event_type)

    async def _on_call_connected(self, conn, call_id: str, data: dict):
        """
        Triggered when ACS confirms the call is fully established.
        This is the correct point to start media operations.
        """
        logger.info("CallConnected (call_id=%s)", call_id)

        voice = self.config.get("receptionist:voice_name")
        speech_lang = self.config.get("receptionist:speech_language", "en-AU")

        if not self._is_open():
            afterhours_msg = self.config.get("receptionist:afterhours_message")
            conn.play_media_to_all(
                self._tts(afterhours_msg),
                operation_context="afterhours_message",
            )
            return

        caller_id = self._best_effort_target_participant(conn)

        if caller_id is None:
            logger.error(
                "Unable to determine caller participant for recognition (call_id=%s)",
                call_id,
            )
            conn.play_media_to_all(
                self._tts(
                    "I'm sorry, I wasn't able to start speech recognition. "
                    "Let me transfer you to reception."
                ),
                operation_context="pre_fallback",
            )
            return

        greeting = self.config.get("receptionist:greeting_message")
        greeting_ssml = build_ssml_message(greeting, voice)

        conn.start_recognizing_media(
            input_type="speech",
            target_participant=caller_id,
            play_prompt=self._ssml(greeting_ssml),
            interrupt_prompt=True,
            interrupt_call_media_operation=True,
            speech_language=speech_lang,
            initial_silence_timeout=10,
            end_silence_timeout=2,
            operation_context="attempt:1",
        )
        logger.info("Started speech recognition (call_id=%s)", call_id)

    async def _on_play_completed(self, conn, call_id: str, op_context: str):
        logger.info("PlayCompleted: op_context=%s", op_context)

        if op_context in ("afterhours_message", "terminal_fallback"):
            try:
                conn.hang_up(is_for_everyone=True)
            except Exception as exc:
                logger.warning("Hang up failed: %s", exc)
            return

        if op_context == "pre_transfer":
            pending = self._pending_store.get(call_id)
            if pending:
                self._do_transfer(
                    conn,
                    pending["aad_id"],
                    pending["display_name"],
                    is_fallback=False,
                )
                return

            logger.warning(
                "No pending transfer found for call_id=%s — falling back to reception",
                call_id,
            )
            reception_id = self.config.get("receptionist:default_reception_aad_id")
            self._do_transfer(conn, reception_id, "Reception", is_fallback=True)
            return

        if op_context == "pre_fallback":
            reception_id = self.config.get("receptionist:default_reception_aad_id")
            self._do_transfer(conn, reception_id, "Reception", is_fallback=True)

    async def _on_speech_recognised(self, conn, data: dict, op_context: str):
        speech_result = data.get("speechResult", {}) or {}
        spoken = (speech_result.get("speech") or "").strip()
        call_id = data.get("callConnectionId", "")

        logger.info(
            "Speech recognised (chars=%d, op_context=%s, call_id=%s)",
            len(spoken),
            op_context,
            call_id,
        )

        if not spoken:
            await self._on_speech_failed(conn, call_id, op_context)
            return

        try:
            tenant_id, client_id, client_secret = self.config.get_graph_credentials()
            group_id = self.config.get("receptionist:staff_group_id")
            staff_list = await get_staff_members(
                tenant_id=tenant_id,
                client_id=client_id,
                client_secret=client_secret,
                group_id=group_id,
            )
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

        if not result.found:
            noanswer = self.config.get("receptionist:noanswer_message")
            conn.play_media_to_all(
                self._tts(noanswer),
                operation_context="pre_fallback",
            )
            logger.info("No directory match found for '%s'", spoken)
            return

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
        conn.play_media_to_all(
            self._ssml(build_ssml_transfer_message(result.staff, voice)),
            operation_context="pre_transfer",
        )
        logger.info(
            "Queued transfer to '%s' via %s (score=%.1f)",
            result.staff.display_name,
            result.strategy,
            result.score,
        )

    async def _on_speech_failed(self, conn, call_id: str, op_context: str):
        try:
            attempt_num = int(op_context.split(":")[-1])
        except (ValueError, IndexError):
            attempt_num = 1

        speech_lang = self.config.get("receptionist:speech_language", "en-AU")

        if attempt_num < 2:
            target_participant = self._best_effort_target_participant(conn)
            if target_participant is None:
                logger.error(
                    "Retry recognition could not resolve a participant (call_id=%s)",
                    call_id,
                )
                conn.play_media_to_all(
                    self._tts(
                        "I'm sorry, I'm unable to restart speech recognition. "
                        "Let me transfer you to reception."
                    ),
                    operation_context="pre_fallback",
                )
                return

            conn.start_recognizing_media(
                input_type="speech",
                target_participant=target_participant,
                play_prompt=self._tts(
                    "I didn't quite catch that. "
                    "Please say the full name of the person you would like to speak to."
                ),
                interrupt_prompt=True,
                interrupt_call_media_operation=True,
                speech_language=speech_lang,
                initial_silence_timeout=10,
                end_silence_timeout=2,
                operation_context="attempt:2",
            )
            logger.info("Started recognition retry for call_id=%s", call_id)
            return

        logger.info("Speech not recognised after 2 attempts — routing to reception")
        conn.play_media_to_all(
            self._tts(
                "I'm unable to understand. Let me connect you to our reception team."
            ),
            operation_context="pre_fallback",
        )

    async def _on_transfer_failed(self, conn, call_id: str, data: dict, op_context: str):
        reason = (data.get("resultInformation") or {}).get("message", "unknown")
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
            return

        conn.play_media_to_all(
            self._tts(
                "That extension is currently unavailable. Transferring you to reception."
            ),
            operation_context="pre_fallback",
        )

    def _do_transfer(
        self,
        conn,
        aad_object_id: str,
        display_name: str,
        is_fallback: bool = False,
    ):
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

        conn.transfer_call_to_participant(
            target_participant=target,
            operation_context=op_ctx,
        )
        logger.info("Transfer initiated → '%s' (%s)", display_name, aad_object_id)
