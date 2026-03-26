"""
call_handler.py
===============
Orchestrates the full call flow:
  incoming call → greeting → speech recognition → name match → transfer

All messages and config loaded from Azure App Configuration.
Pronunciation handled via SSML with per-user overrides.
"""

import logging
from datetime import datetime, time as dtime
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
)

from config_loader import ConfigLoader
from graph_client  import get_staff_members
from matcher       import NameMatcher, build_ssml_transfer_message, build_ssml_not_found, build_ssml_greeting

logger = logging.getLogger(__name__)


class CallHandler:

    def __init__(self, config: ConfigLoader):
        self.config = config

    # ── ACS client (fresh per call to pick up rotated secrets) ──

    def _acs(self) -> CallAutomationClient:
        return CallAutomationClient.from_connection_string(
            self.config.get_acs_connection_string()
        )

    # ── TTS helpers ─────────────────────────────────────────────

    def _ssml(self, ssml_text: str) -> SsmlSource:
        return SsmlSource(ssml_document=ssml_text)

    def _tts(self, text: str) -> TextSource:
        return TextSource(
            text=text,
            voice_name=self.config.get("receptionist:voice_name"),
        )

    # ── Business hours check ─────────────────────────────────────

    def _is_open(self) -> bool:
        tz    = ZoneInfo(self.config.get("receptionist:timezone", "UTC"))
        now   = datetime.now(tz)
        day   = now.strftime("%A").lower()
        hours = self.config.get_business_hours().get(day)
        if not hours:
            return False
        start = dtime(*map(int, hours[0].split(":")))
        end   = dtime(*map(int, hours[1].split(":")))
        return start <= now.time() <= end

    # ════════════════════════════════════════════════════════════
    #  Incoming call
    # ════════════════════════════════════════════════════════════

    async def handle_incoming(self, data: dict):
        ctx           = data["incomingCallContext"]
        callback_url  = self.config.get("receptionist:acs_callback_url")
        voice         = self.config.get("receptionist:voice_name")
        speech_lang   = self.config.get("receptionist:speech_language", "en-AU")
        client        = self._acs()

        call_conn = client.answer_call(
            incoming_call_context=ctx,
            callback_url=callback_url,
        )

        if not self._is_open():
            # ── Out of hours ────────────────────────────────────
            msg = self.config.get("receptionist:afterhours_message")
            call_conn.play_media_to_all(self._tts(msg))
            logger.info("Out of hours — played afterhours message")
            return

        # ── Business hours — play greeting, start speech recognition ──
        greeting_text = self.config.get("receptionist:greeting_message")
        greeting_ssml = build_ssml_greeting(
            self.config.get("receptionist:company_name"),
            greeting_text,
            voice,
        )

        call_conn.start_recognizing_media(
            input_type="speech",
            target_participant=None,
            play_prompt=self._ssml(greeting_ssml),
            interrupt_prompt=True,
            speech_language=speech_lang,
            end_silence_timeout_in_ms=1500,
            operation_context="attempt_1",
        )
        logger.info("Greeting played — listening for name (call_id=%s)", call_conn.call_connection_id)

    # ════════════════════════════════════════════════════════════
    #  Mid-call callback events
    # ════════════════════════════════════════════════════════════

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
            await self._on_speech_failed(conn, op_context)

        elif event_type == "Microsoft.Communication.CallTransferAccepted":
            logger.info("Transfer accepted — call_id=%s", call_id)

        elif event_type == "Microsoft.Communication.CallTransferFailed":
            await self._on_transfer_failed(conn, data, op_context)

        elif event_type == "Microsoft.Communication.PlayCompleted":
            logger.info("PlayCompleted — op_context=%s", op_context)

    # ── Speech recognised ────────────────────────────────────────

    async def _on_speech_recognised(self, conn, data: dict, op_context: str):
        speech_result = data.get("speechResult", {})
        spoken        = (speech_result.get("speech") or "").strip()
        confidence    = speech_result.get("confidence", 0.0)

        logger.info("Speech recognised: '%s' (confidence=%.2f)", spoken, confidence)

        if not spoken:
            await self._on_speech_failed(conn, op_context)
            return

        # Load staff and match
        tenant_id, client_id, client_secret = self._get_graph_creds()
        group_id   = self.config.get("receptionist:staff_group_id")
        threshold  = self.config.get_int("receptionist:match_threshold", 65)
        staff_list = await get_staff_members(tenant_id, client_id, client_secret, group_id)

        matcher = NameMatcher(threshold=threshold)
        result  = matcher.match(spoken, staff_list)

        voice = self.config.get("receptionist:voice_name")

        if result.found:
            # Speak name using SSML with pronunciation override if present
            ssml = build_ssml_transfer_message(result.staff, voice)
            conn.play_media_to_all(
                self._ssml(ssml),
                play_options=PlayOptions(operation_context="pre_transfer"),
            )
            self._transfer(conn, result.staff.aad_id, result.staff.display_name)
            logger.info(
                "Matched '%s' → '%s' via %s (score=%.1f)",
                spoken, result.staff.display_name, result.strategy, result.score
            )
        else:
            # No match — tell caller and route to reception
            noanswer_msg = self.config.get("receptionist:noanswer_message")
            conn.play_media_to_all(
                self._tts(noanswer_msg),
                play_options=PlayOptions(operation_context="pre_fallback"),
            )
            reception_id = self.config.get("receptionist:default_reception_aad_id")
            self._transfer(conn, reception_id, "Reception", is_fallback=True)
            logger.warning("No match for '%s' — routing to reception", spoken)

    # ── Speech failed / silence ──────────────────────────────────

    async def _on_speech_failed(self, conn, op_context: str):
        attempt = 2 if op_context.endswith("_1") else 3
        speech_lang = self.config.get("receptionist:speech_language", "en-AU")
        voice       = self.config.get("receptionist:voice_name")

        if attempt == 2:
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
                operation_context="attempt_2",
            )
        else:
            conn.play_media_to_all(
                self._tts("I'm unable to understand. Let me connect you to our reception team.")
            )
            reception_id = self.config.get("receptionist:default_reception_aad_id")
            self._transfer(conn, reception_id, "Reception", is_fallback=True)

    # ── Transfer failed ──────────────────────────────────────────

    async def _on_transfer_failed(self, conn, data: dict, op_context: str):
        reason = data.get("resultInformation", {}).get("message", "unknown")
        logger.error("Transfer failed — op_context=%s, reason=%s", op_context, reason)

        if op_context == "fallback_transfer":
            conn.play_media_to_all(self._tts(
                "I'm sorry, we are unable to connect your call right now. "
                "Please try again shortly."
            ))
        else:
            conn.play_media_to_all(self._tts(
                "That extension is currently unavailable. Transferring you to reception."
            ))
            reception_id = self.config.get("receptionist:default_reception_aad_id")
            self._transfer(conn, reception_id, "Reception", is_fallback=True)

    # ── Transfer helper ──────────────────────────────────────────

    def _transfer(self, conn, aad_object_id: str, display_name: str, is_fallback: bool = False):
        target  = MicrosoftTeamsUserIdentifier(user_id=aad_object_id)
        options = TransferCallToParticipantOptions(
            target_participant=target,
            operation_context="fallback_transfer" if is_fallback else "primary_transfer",
        )
        try:
            conn.transfer_call_to_participant(options)
            logger.info("Transfer initiated → %s (%s)", display_name, aad_object_id)
        except Exception as exc:
            logger.error("Transfer initiation failed for %s: %s", aad_object_id, exc)
            raise

    def _get_graph_creds(self) -> tuple[str, str, str]:
        tenant_id = self.config.get("receptionist:tenant_id")
        client_id, client_secret = self.config.get_graph_credentials()
        return tenant_id, client_id, client_secret
