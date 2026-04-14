"""
function_app.py
===============
Azure Functions v2 Python entry point.

CRITICAL DESIGN: incoming_call must return HTTP 200 to EventGrid within
30 seconds or EventGrid will retry. ACS call handling (answer_call, play_media)
can take 10-30 seconds. We therefore return 200 immediately and fire the
call handling as a background task using asyncio.

All configuration is loaded from Azure App Configuration at runtime.
Secrets are loaded from Azure Key Vault via Managed Identity.
"""

import asyncio
import json
import logging
import azure.functions as func
from call_handler import CallHandler
from config_loader import ConfigLoader

logger = logging.getLogger(__name__)
app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# Module-level singletons — reused across warm instances
_config: ConfigLoader = None
_handler: CallHandler = None


def _get_handler() -> CallHandler:
    global _config, _handler
    if _handler is None:
        _config = ConfigLoader()
        _handler = CallHandler(_config)
    return _handler


def _parse_events(body, source: str) -> list:
    """
    Normalizes webhook payload into a list of event dicts.
    Returns an empty list for invalid payloads.
    """
    if isinstance(body, dict):
        return [body]

    if isinstance(body, list):
        valid_events = [event for event in body if isinstance(event, dict)]
        if len(valid_events) != len(body):
            logger.warning("%s: dropped %d non-dict event(s)", source, len(body) - len(valid_events))
        return valid_events

    logger.warning("%s: invalid payload type '%s' (expected dict or list)", source, type(body).__name__)
    return []


# ── Route 1: Incoming call webhook ───────────────────────────

@app.route(route="incoming_call", methods=["POST"])
async def incoming_call(req: func.HttpRequest) -> func.HttpResponse:
    """
    ACS fires this when a call arrives at the Teams resource account.

    IMPORTANT: Returns 200 immediately to EventGrid, then processes the
    call in the background. This prevents EventGrid from timing out and
    retrying the event.
    """
    try:
        body = req.get_json()
    except Exception as exc:
        logger.error("Failed to parse request body: %s", exc)
        return func.HttpResponse("OK", status_code=200)

    try:
        events = _parse_events(body, "incoming_call")
        if not events:
            return func.HttpResponse("OK", status_code=200)

        event_types = [e.get("type", e.get("eventType", "unknown")) for e in events]
        logger.info(
            "incoming_call: received %d event(s): %s",
            len(events), event_types)

        for event in events:
            event_type = event.get("type", event.get("eventType", ""))

            # EventGrid validation — must respond synchronously
            if event_type == "Microsoft.EventGrid.SubscriptionValidationEvent":
                code = event["data"]["validationCode"]
                logger.info("EventGrid validation handshake completed")
                return func.HttpResponse(
                    json.dumps({"validationResponse": code}),
                    mimetype="application/json",
                    status_code=200,
                )

            if event_type == "Microsoft.Communication.IncomingCall":
                # Fire and forget — return 200 immediately to EventGrid
                # The call handling runs in the background
                asyncio.ensure_future(_handle_incoming_background(event["data"]))

        return func.HttpResponse("OK", status_code=200)

    except Exception as exc:
        logger.exception("incoming_call unhandled error: %s", exc)
        return func.HttpResponse("OK", status_code=200)


async def _handle_incoming_background(data: dict):
    """
    Handles the incoming call in the background after returning 200 to EventGrid.
    """
    try:
        handler = _get_handler()
        await handler.handle_incoming(data)
    except Exception as exc:
        logger.exception("Background call handling error: %s", exc)


# ── Route 2: Mid-call ACS callback events ────────────────────

@app.route(route="acs_callback", methods=["POST"])
async def acs_callback(req: func.HttpRequest) -> func.HttpResponse:
    """
    ACS fires mid-call events here:
      RecognizeCompleted  — speech transcribed
      RecognizeFailed     — no speech / timeout
      PlayCompleted       — audio finished playing
      CallTransferAccepted / CallTransferFailed
      CallDisconnected
    """
    try:
        body = req.get_json()

        events = _parse_events(body, "acs_callback")
        if not events:
            return func.HttpResponse("OK", status_code=200)

        event_types = [e.get("type", "unknown") for e in events]
        logger.info(
            "acs_callback: received %d event(s): %s",
            len(events), event_types)

        handler = _get_handler()
        for event in events:
            try:
                await handler.handle_callback(event)
            except Exception as exc:
                logger.exception("handle_callback error for %s: %s",
                                 event.get("type", "unknown"), exc)

        return func.HttpResponse("OK", status_code=200)

    except Exception as exc:
        logger.exception("acs_callback unhandled error: %s", exc)
        return func.HttpResponse("OK", status_code=200)


# ── Route 3: Health check ─────────────────────────────────────

@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
async def health(req: func.HttpRequest) -> func.HttpResponse:
    """
    Returns minimal status confirmation.
    """
    try:
        cfg = _get_handler().config
        return func.HttpResponse(
            json.dumps({
                "status": "ok",
                "company": cfg.get("receptionist:company_name"),
            }),
            mimetype="application/json",
            status_code=200,
        )
    except Exception as exc:
        logger.error("Health check failed: %s", exc)
        return func.HttpResponse(
            json.dumps({"status": "error"}),
            mimetype="application/json",
            status_code=500,
        )
