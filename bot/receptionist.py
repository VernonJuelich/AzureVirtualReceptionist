"""
Azure Virtual Receptionist — receptionist.py
=============================================
Main Azure Function App entry point.
All configuration loaded from Azure App Configuration at runtime.
Secrets loaded from Azure Key Vault via Managed Identity.
"""

import json
import logging
import azure.functions as func

from config_loader import ConfigLoader
from call_handler  import CallHandler

logger = logging.getLogger(__name__)
app    = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

# Singleton loader — reused across warm Function App instances
_config  = None
_handler = None


def _get_handler() -> CallHandler:
    global _config, _handler
    if _handler is None:
        _config  = ConfigLoader()
        _handler = CallHandler(_config)
    return _handler


# ── Webhook 1: Incoming call from ACS ────────────────────────

@app.route(route="incoming_call", methods=["POST"])
async def incoming_call(req: func.HttpRequest) -> func.HttpResponse:
    """
    ACS fires this webhook when a call arrives at the Teams resource account.
    Handles EventGrid validation handshake + IncomingCall event.
    """
    try:
        events = req.get_json()
        logger.info("incoming_call received: %s", json.dumps(events)[:300])

        for event in events:
            event_type = event.get("type", "")

            # EventGrid subscription validation handshake
            if event_type == "Microsoft.EventGrid.SubscriptionValidationEvent":
                code = event["data"]["validationCode"]
                logger.info("EventGrid validation handshake — returning code")
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
        return func.HttpResponse("Internal error", status_code=500)


# ── Webhook 2: Mid-call ACS callback events ──────────────────

@app.route(route="acs_callback", methods=["POST"])
async def acs_callback(req: func.HttpRequest) -> func.HttpResponse:
    """
    ACS fires mid-call events here:
      RecognizeCompleted  — speech transcribed
      RecognizeFailed     — no speech / timeout
      CallTransferAccepted / Failed
      PlayCompleted
    """
    try:
        events = req.get_json()
        logger.info("acs_callback received: %s", json.dumps(events)[:300])

        handler = _get_handler()
        for event in events:
            await handler.handle_callback(event)

        return func.HttpResponse("OK", status_code=200)

    except Exception as exc:
        logger.exception("acs_callback unhandled error: %s", exc)
        return func.HttpResponse("Internal error", status_code=500)


# ── Health check ─────────────────────────────────────────────

@app.route(route="health", methods=["GET"])
async def health(req: func.HttpRequest) -> func.HttpResponse:
    """Returns current config values (non-secret) for verification."""
    try:
        cfg = ConfigLoader()
        return func.HttpResponse(
            json.dumps({
                "status":       "ok",
                "company":      cfg.get("receptionist:company_name"),
                "timezone":     cfg.get("receptionist:timezone"),
                "voice":        cfg.get("receptionist:voice_name"),
                "threshold":    cfg.get("receptionist:match_threshold"),
            }),
            mimetype="application/json",
            status_code=200,
        )
    except Exception as exc:
        return func.HttpResponse(
            json.dumps({"status": "error", "detail": str(exc)}),
            mimetype="application/json",
            status_code=500,
        )
