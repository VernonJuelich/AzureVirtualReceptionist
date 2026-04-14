"""
function_app.py
===============
Azure Functions v2 Python entry point.

All configuration is loaded from Azure App Configuration at runtime.
Secrets are loaded from Azure Key Vault via Managed Identity.
"""

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


# ── Route 1: Incoming call webhook ───────────────────────────

@app.route(route="incoming_call", methods=["POST"])
async def incoming_call(req: func.HttpRequest) -> func.HttpResponse:
    """
    ACS fires this when a call arrives at the Teams resource account.
    Handles EventGrid validation handshake and IncomingCall events.
    """
    try:
        body = req.get_json()
    except Exception as exc:
        logger.error("Failed to parse request body: %s", exc)
        return func.HttpResponse("OK", status_code=200)

    try:
        if isinstance(body, dict):
            events = [body]
        else:
            events = body

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
                handler = _get_handler()
                await handler.handle_incoming(event["data"])

        return func.HttpResponse("OK", status_code=200)

    except Exception as exc:
        logger.exception("incoming_call unhandled error: %s", exc)
        return func.HttpResponse("OK", status_code=200)


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

        if isinstance(body, dict):
            events = [body]
        else:
            events = body

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

@app.route(route="health", methods=["GET"])
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
            json.dumps({"status": "error", "detail": str(exc)}),
            mimetype="application/json",
            status_code=500,
        )
