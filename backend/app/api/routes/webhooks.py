"""Webhooks from outside services (P4.3). Mounted by app/main.py directly on the app, outside api_router, so the CSRF
guard (the X-NearShop-Client header) does not apply: Meta's calls never send it. Each call is authenticated instead:

  * GET /api/webhooks/whatsapp: Meta's verification challenge when the webhook is set up. It answers hub.challenge,
    as plain text, only when hub.mode is subscribe and hub.verify_token equals NEARSHOP_WHATSAPP_WEBHOOK_VERIFY_TOKEN
    (compared in constant time); otherwise 403.
  * POST /api/webhooks/whatsapp: status callbacks (sent, delivered, read, failed) for the notification outbox. The
    raw body must carry a valid X-Hub-Signature-256, the HMAC-SHA256 of the body keyed with the Meta app secret
    (NEARSHOP_WHATSAPP_APP_SECRET); otherwise 403 bad_signature, and nothing is read. Each status is recorded once
    (app/services/whatsapp.py record_statuses), so Meta's retries and duplicates change nothing. A signed call
    always gets 200, whatever it holds, so Meta does not retry it.

  * POST /api/webhooks/autopay (P9.6): the payment gateway's UPI Autopay events (mandate authorised, debit
    succeeded, debit failed, last retry failed, mandate cancelled) for app/services/gateway.py. The raw body must
    carry a valid X-Razorpay-Signature, the HMAC-SHA256 of the body keyed with NEARSHOP_AUTOPAY_WEBHOOK_SECRET;
    otherwise 403 bad_signature, and nothing is read. Each event id (X-Razorpay-Event-Id, or the body's hash when
    there is none) is processed once, so the gateway's retries and replayed events change nothing. A signed call
    gets 200 once its event is recorded; an unexpected error answers 500, nothing is recorded, and the gateway
    retries it.

The secrets and the token come only from settings. Like every /api path, these are reached through the site's domain
(Vercel's proxy adds the edge secret, P2.3), and count against the default per-IP rate limit. Replies from shops
(messages) are P4.4.
"""
import hmac
import json
import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.database import Database
from app.core.errors import AppError
from app.services import gateway, whatsapp

log = logging.getLogger("nearshop.webhooks")

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


class WebhookRefused(AppError):
    status_code = 403
    code = "forbidden"


@router.get("/whatsapp", response_class=PlainTextResponse)
def whatsapp_verify(mode: str = Query("", alias="hub.mode"), token: str = Query("", alias="hub.verify_token"),
                    challenge: str = Query("", alias="hub.challenge", max_length=200)):
    """Meta's subscription check: echo the challenge when the verify token matches."""
    expected = settings.whatsapp_webhook_verify_token.get_secret_value().strip()
    if not expected or mode != "subscribe" or not hmac.compare_digest(token.encode(), expected.encode()):
        raise WebhookRefused("The webhook verification failed.", code="verification_failed")
    return PlainTextResponse(challenge)


@router.post("/whatsapp")
async def whatsapp_events(request: Request):
    """Status callbacks, signed by Meta (module docstring)."""
    body = await request.body()
    secret = settings.whatsapp_app_secret.get_secret_value().strip()
    if not whatsapp.signature_valid(secret, body, request.headers.get("x-hub-signature-256")):
        log.warning("WhatsApp webhook call refused: %s", "bad signature" if secret else "no app secret configured")
        raise WebhookRefused("The request signature is not valid.", code="bad_signature")
    try:
        payload = json.loads(body) if body else {}
    except ValueError:
        log.warning("WhatsApp webhook call with a signed body that is not JSON")
        return {"ok": True, "recorded": 0}
    counts = await run_in_threadpool(whatsapp.record_statuses, Database(), payload)
    return {"ok": True, **counts}


@router.post("/autopay")
async def autopay_events(request: Request):
    """UPI Autopay events from the payment gateway, signed with the webhook secret (module docstring)."""
    body = await request.body()
    secret = settings.autopay_webhook_secret.get_secret_value().strip()
    if not gateway.signature_valid(secret, body, request.headers.get(gateway.SIGNATURE_HEADER)):
        log.warning("Autopay webhook call refused: %s", "bad signature" if secret else "no webhook secret configured")
        raise WebhookRefused("The request signature is not valid.", code="bad_signature")
    try:
        payload = json.loads(body) if body else {}
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        log.warning("Autopay webhook call with a signed body that is not a JSON object")
        return {"ok": True, "outcome": "ignored"}
    event_id = (request.headers.get(gateway.EVENT_ID_HEADER) or "").strip() or gateway.body_event_id(body)
    result = await run_in_threadpool(gateway.process_event, Database(), event_id, payload)
    return {"ok": True, **result}
