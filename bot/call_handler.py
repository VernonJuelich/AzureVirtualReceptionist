"""
call_handler.py
===============
Orchestrates the full call flow.

Fixes from code review:
  - Transfer now waits for PlayCompleted event before initiating (no race condition)
  - After-hours and terminal fallback paths now hang up the call cleanly
  - Retry count parsed explicitly — no longer fragile string-shape dependent
  - AAD Object ID validated before attempting transfer
  - DirectoryUnavailableError handled distinctly from "no name match"
  - Unused import (build_ssml_not_found) removed
"""

import logging
import re
from datetime import datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

from azure.communication.callautomation import (
    CallAutomationClient,
    SsmlSource,
    TextSource,
)
from azure.communication.callautomation.models import (
    MicrosoftTeamsUserIdentifier,
    TransferCallToParticipantOptions,
    PlayOptions,
    HangUpOptions,
)

from config_loader import ConfigLoader
from graph_client  import get_staff_members, DirectoryUnavailableError
from matcher       import NameMatcher, build_ssml_transfer_message, build_ssml_message

logger = logging.getLogger(__name__)

# Regex for basic AAD Object ID validation (UUID format)
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE
)


def _is_valid_aad_id(value: str) -> bool:
    return bool(value and _UUID_RE.match(value.strip()))


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
            logger.warning("Invalid timezone '%s' — defaulting to UTC", tz_name)
            tz = ZoneInfo("UTC")

        now   = datetime.now(tz)
        day   = now.strftime("%A").lower()
        hours = self.config.get_business_hours().get(day)

        if not hours:
            return False
        try:
            sh, sm = map(int, hours[0].split(":"))
            eh, em = map(int, hours[1].split(":"))
            return dtime(sh, sm) <= now.time() <= dtime(eh, em)
        except Exception:
            logger.warning("Failed to parse business hours for %s — treating as closed", day)
            return False

    # ════════════════════════════════════════════════════════
    #  Incoming call
    # ════════════════════════════════════════════════════════

    async def handle_incoming(self, data: dict):
        ctx          = data.get("incomingCallContext", "")
        callback_url = self.config.get("receptionist:acs_callback_url")
        voice        = self.config.get("receptionist:voice_name")
        speech_lang  = self.config.get("receptionist:speech_language", "en-AU")

        # Log correlation ID only — not call content
        correlation_id = data.get("correlationId", "unknown")
        logger.info("Handling incoming call (correlationId=%s)", correlation_id)

        client    = self._acs()
        call_conn = client.answer_call(
            incoming_call_context=ctx,
            callback_url=callback_url,
        )

        if not self._is_open():
            # Play after-hours message then hang up cleanly
            afterhours_msg = self.config.get("receptionist:afterhours_message")
            call_conn.play_media_to_all(
                self._tts(afterhours_msg),
                play_options=PlayOptions(operation_context="afterhours_message"),
            )
            # Hang up is triggered in handle_callback on PlayCompleted
            return

        # Business hours — play greeting and start speech recognition (attempt 1)
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
        data       = event.get("data", {})
        call_id    = data.get("callConnectionId", "")
        op_context = data.get("operationContext", "")

        client = self._acs()
        conn   = client.get_call_connection(call_id)

        if event_type == "Microsoft.Communication.RecognizeCompleted":
            await self._on_speech_recognised(conn, data, op_context)

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
            # After-hours message finished — hang up cleanly
            logger.info("After-hours message complete — hanging up")
            try:
                conn.hang_up(is_for_everyone=True)
            except Exception as exc:
                logger.warning("Hang up failed: %s", exc)

        elif op_context == "terminal_fallback":
            # Final "unable to connect" message finished — hang up
            logger.info("Terminal fallback message complete — hanging up")
            try:
                conn.hang_up(is_for_everyone=True)
            except Exception as exc:
                logger.warning("Hang up failed: %s", exc)

        elif op_context == "pre_transfer":
            # "Connecting you to [name]" finished — now initiate transfer
            # Transfer AAD ID was stored in a pending transfer dict
            pending = _pending_transfers.pop(call_id, None)
            if pending:
                logger.info("PlayCompleted pre_transfer — initiating transfer to %s", pending["display_name"])
                self._do_transfer(conn, pending["aad_id"], pending["display_name"], is_fallback=False)
            else:
                logger.warning("No pending transfer found for call_id=%s", call_id)

        elif op_context == "pre_fallback":
            # "Couldn't find / unavailable" message finished — transfer to reception
            logger.info("PlayCompleted pre_fallback — transferring to reception")
            reception_id = self.config.get("receptionist:default_reception_aad_id")
            self._do_transfer(conn, reception_id, "Reception", is_fallback=True)

    # ── Speech recognised ─────────────────────────────────────

    async def _on_speech_recognised(self, conn, data: dict, op_context: str):
        speech_result = data.get("speechResult", {})
        spoken        = (speech_result.get("speech") or "").strip()

        # Log that recognition occurred — not the content (may be a person's name/PII)
        logger.info("Speech recognised (length=%d chars, op_context=%s)", len(spoken), op_context)

        if not spoken:
            await self._on_speech_failed(conn, data.get("callConnectionId", ""), op_context)
            return

        # Load staff directory
        try:
            tenant_id, client_id, client_secret = self.config.get_graph_credentials()
            group_id   = self.config.get("receptionist:staff_group_id")
            staff_list = await get_staff_members(tenant_id, client_id, client_secret, group_id)
        except DirectoryUnavailableError:
            logger.error("Staff directory unavailable — routing to reception")
            conn.play_media_to_all(
                self._tts("I'm sorry, our directory is currently unavailable. Let me transfer you to reception."),
                play_options=PlayOptions(operation_context="pre_fallback"),
            )
            return

        threshold = self.config.get_int("receptionist:match_threshold", 65)
        matcher   = NameMatcher(threshold=threshold)
        result    = matcher.match(spoken, staff_list)
        voice     = self.config.get("receptionist:voice_name")

        call_id = data.get("callConnectionId", "")

        if result.found:
            # Validate AAD ID before attempting transfer
            if not _is_valid_aad_id(result.staff.aad_id):
                logger.error("Invalid AAD Object ID for '%s': '%s'", result.staff.display_name, result.staff.aad_id)
                conn.play_media_to_all(
                    self._tts("I'm sorry, I'm unable to connect that call right now. Let me transfer you to reception."),
                    play_options=PlayOptions(operation_context="pre_fallback"),
                )
                return

            # Store transfer target — actual transfer triggered after PlayCompleted
            _pending_transfers[call_id] = {
                "aad_id":       result.staff.aad_id,
                "display_name": result.staff.display_name,
            }
            ssml = build_ssml_transfer_message(result.staff, voice)
            conn.play_media_to_all(
                self._ssml(ssml),
                play_options=PlayOptions(operation_context="pre_transfer"),
            )
            logger.info(
                "Queued transfer to '%s' via %s (score=%.1f)",
                result.staff.display_name, result.strategy, result.score
            )
        else:
            noanswer = self.config.get("receptionist:noanswer_message")
            conn.play_media_to_all(
                self._tts(noanswer),
                play_options=PlayOptions(operation_context="pre_fallback"),
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
            attempt_num = 2  # Unknown context — go straight to fallback

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
                end_silence_timeout_in_ms=1500,
                operation_context="attempt:2",
            )
        else:
            logger.info("Speech not recognised after 2 attempts — routing to reception")
            conn.play_media_to_all(
                self._tts("I'm unable to understand. Let me connect you to our reception team."),
                play_options=PlayOptions(operation_context="pre_fallback"),
            )

    # ── Transfer failed ───────────────────────────────────────

    async def _on_transfer_failed(self, conn, data: dict, op_context: str):
        reason = data.get("resultInformation", {}).get("message", "unknown")
        logger.error("Transfer failed (op_context=%s, reason=%s)", op_context, reason)

        if op_context == "fallback_transfer":
            # Reception transfer also failed — play terminal message and hang up
            conn.play_media_to_all(
                self._tts(
                    "I'm sorry, we are unable to connect your call at this time. "
                    "Please try again shortly."
                ),
                play_options=PlayOptions(operation_context="terminal_fallback"),
            )
        else:
            # Primary transfer failed — try reception
            conn.play_media_to_all(
                self._tts("That extension is currently unavailable. Transferring you to reception."),
                play_options=PlayOptions(operation_context="pre_fallback"),
            )

    # ── Transfer helper ───────────────────────────────────────

    def _do_transfer(self, conn, aad_object_id: str, display_name: str, is_fallback: bool = False):
        if not _is_valid_aad_id(aad_object_id):
            logger.error(
                "Transfer aborted — invalid AAD Object ID for '%s': '%s'",
                display_name, aad_object_id
            )
            conn.play_media_to_all(
                self._tts("I'm sorry, I'm unable to complete that transfer."),
                play_options=PlayOptions(operation_context="terminal_fallback"),
            )
            return

        target  = MicrosoftTeamsUserIdentifier(user_id=aad_object_id)
        options = TransferCallToParticipantOptions(
            target_participant=target,
            operation_context="fallback_transfer" if is_fallback else "primary_transfer",
        )
        try:
            conn.transfer_call_to_participant(options)
            logger.info("Transfer initiated → '%s' (%s)", display_name, aad_object_id)
        except Exception as exc:
            logger.error("Transfer initiation exception for '%s': %s", display_name, exc)
            raise


# Module-level dict to track pending transfers between PlayCompleted events
# key: call_connection_id, value: {"aad_id": ..., "display_name": ...}
_pending_transfers: dict = {}
