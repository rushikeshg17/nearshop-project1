"""Outbound messages to phones (P4.1): one-time codes, through a provider interface with three implementations.

  * WhatsAppProvider: Meta's WhatsApp Cloud API with the approved authentication template (B8). The primary
    channel; Meta prices authentication messages per message. The template's copy-code button carries the code,
    so the body parameter and the button's URL parameter are both the code.
  * SmsProvider: SMS through a DLT-registered sender header and template (B7), sent with MSG91's flow API. The
    fallback, used when WhatsApp is not configured, when a WhatsApp send fails, when the user asks for an SMS
    (channel "sms" on the code request) and for a resend after an unused WhatsApp code (app/services/otp.py:
    the Cloud API accepts a message to a number without WhatsApp and reports the failure only later, by
    webhook). The registered text is otp_sms_text: its last line is the WebOTP line Android Chrome reads to
    fill the code in ("@<site domain> #<code>"). MSG91 can answer HTTP 200 with {"type": "error"}: only
    {"type": "success"} counts as sent.
  * ConsoleProvider: development and test only. It writes the code to the log and keeps the last messages in
    memory (ConsoleProvider.outbox), which the tests read. It is never used anywhere else (config.py refuses
    NEARSHOP_OTP_CONSOLE outside development and test).

Provider keys come only from settings (config.py: NEARSHOP_WHATSAPP_* and NEARSHOP_SMS_*), never from code.
otp_providers() lists the providers to try, in order. A provider never raises for a failed send: it returns a
Delivery whose error is a short reason (an HTTP status or an exception class), never the phone or the code.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from app.core.config import Settings, settings
from app.core.database import utcnow

log = logging.getLogger("nearshop.messaging")

HTTP_TIMEOUT = 10  # seconds per provider call; a code is worth waiting for, a hung provider is not
CODE_MINUTES = 5  # how long a code is valid, as the message says (app/services/otp.py, CODE_TTL)


@dataclass(frozen=True)
class Delivery:
    """The outcome of one send. cost_estimate is in rupees, 0 for a failed send."""

    ok: bool
    channel: str
    cost_estimate: float = 0.0
    error: str | None = None
    message_id: str | None = None


class OtpProvider(Protocol):
    channel: str

    def send_otp(self, phone: str, code: str) -> Delivery: ...


def e164(phone: str) -> str:
    """A 10-digit Indian mobile number as the providers take it: country code first, no plus sign."""
    return f"91{phone}"


def masked(phone: str) -> str:
    """A phone number as logs may show it: the last two digits only."""
    return f"********{phone[-2:]}"


def otp_sms_text(code: str, domain: str) -> str:
    """The SMS text registered under DLT (B7), with the code in place of its variable. The last line is the format
    Android Chrome's WebOTP feature reads: the site's domain after an at sign, then the code after a hash sign."""
    return (f"{code} is your NearShop code. It is valid for {CODE_MINUTES} minutes. Do not share it with anyone.\n"
            f"\n@{domain} #{code}")


# ---------------------------------------------------------------- HTTP

# (url, JSON body, headers) -> (status, parsed JSON reply or {}). Tests replace it; nothing else should.
Transport = Callable[[str, dict[str, Any], dict[str, str]], tuple[int, dict[str, Any]]]


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read() or b""
    try:
        reply = json.loads(raw) if raw else {}
    except ValueError:
        reply = {}
    return status, reply if isinstance(reply, dict) else {}


transport: Transport = _post_json


def _send(channel: str, url: str, payload: dict[str, Any], headers: dict[str, str], cost: float,
          message_id: Callable[[dict[str, Any]], str | None],
          accepted: Callable[[dict[str, Any]], bool] = lambda reply: True) -> Delivery:
    """One provider call. Sent only with a 2xx answer whose JSON body the provider's accepted() takes as sent."""
    try:
        status, reply = transport(url, payload, headers)
    except (OSError, TimeoutError) as exc:  # urllib's URLError is an OSError
        log.warning("%s send failed: %s", channel, type(exc).__name__)
        return Delivery(False, channel, error=type(exc).__name__[:80])
    if not 200 <= status < 300:
        log.warning("%s send failed: HTTP %s", channel, status)
        return Delivery(False, channel, error=f"http_{status}")
    if not accepted(reply):  # the reply's text is not logged: it may echo the number
        log.warning("%s send refused by the provider: HTTP %s with an error reply", channel, status)
        return Delivery(False, channel, error="rejected")
    return Delivery(True, channel, cost, message_id=message_id(reply))


# ---------------------------------------------------------------- providers


class WhatsAppProvider:
    """Meta's WhatsApp Cloud API: POST /<version>/<phone number id>/messages with the authentication template."""

    channel = "whatsapp"

    def __init__(self, config: Settings = settings):
        self.config = config

    def payload(self, phone: str, code: str) -> dict[str, Any]:
        c = self.config
        return {
            "messaging_product": "whatsapp", "recipient_type": "individual", "to": e164(phone), "type": "template",
            "template": {
                "name": c.whatsapp_otp_template.strip(), "language": {"code": c.whatsapp_template_language.strip()},
                "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": code}]},
                    # The copy-code button: Meta's authentication templates take the code as the button's parameter.
                    {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": code}]},
                ],
            },
        }

    def send_otp(self, phone: str, code: str) -> Delivery:
        c = self.config
        url = (f"{c.whatsapp_api_base.rstrip('/')}/{c.whatsapp_api_version.strip()}/"
               f"{c.whatsapp_phone_number_id.strip()}/messages")
        headers = {"Authorization": f"Bearer {c.whatsapp_access_token.get_secret_value().strip()}"}
        return _send(self.channel, url, self.payload(phone, code), headers, c.whatsapp_cost_inr, _whatsapp_id,
                     lambda reply: _whatsapp_id(reply) is not None)


def _whatsapp_id(reply: dict[str, Any]) -> str | None:
    """The message id of a Cloud API answer ({"messages": [{"id": "wamid..."}]}); an answer without one sent nothing."""
    messages = reply.get("messages")
    if not isinstance(messages, list):
        return None
    return next((m["id"] for m in messages if isinstance(m, dict) and m.get("id")), None)


class SmsProvider:
    """MSG91's flow API with the DLT-registered OTP template (its text is otp_sms_text)."""

    channel = "sms"

    def __init__(self, config: Settings = settings):
        self.config = config

    def payload(self, phone: str, code: str) -> dict[str, Any]:
        c = self.config
        return {"template_id": c.sms_template_id.strip(), "short_url": "0",
                "recipients": [{"mobiles": e164(phone), c.sms_code_variable.strip() or "otp": code}]}

    def send_otp(self, phone: str, code: str) -> Delivery:
        c = self.config
        headers = {"authkey": c.sms_auth_key.get_secret_value().strip(), "accept": "application/json"}
        # MSG91 answers {"type": "success", "message": "<request id>"}, or {"type": "error", "message": "<why>"},
        # sometimes with HTTP 200.
        return _send(self.channel, c.sms_api_url, self.payload(phone, code), headers, c.sms_cost_inr,
                     lambda reply: reply.get("request_id") or reply.get("message"),
                     lambda reply: str(reply.get("type", "")).strip().lower() == "success")


class ConsoleProvider:
    """Development and test: the code goes to the log and to outbox, nothing leaves the machine."""

    channel = "console"
    outbox: ClassVar[deque[dict[str, Any]]] = deque(maxlen=200)

    def __init__(self, config: Settings = settings):
        self.config = config

    def send_otp(self, phone: str, code: str) -> Delivery:
        text = otp_sms_text(code, self.config.otp_webotp_domain.strip() or "localhost")
        self.outbox.append({"phone": phone, "code": code, "text": text, "at": utcnow()})
        log.info("console OTP for %s: %s", masked(phone), code)
        return Delivery(True, self.channel)

    @classmethod
    def last_code(cls, phone: str) -> str | None:
        """The newest code sent to phone (tests)."""
        return next((m["code"] for m in reversed(cls.outbox) if m["phone"] == phone), None)


def otp_providers(config: Settings = settings) -> list[OtpProvider]:
    """The providers to try for a code, in order: WhatsApp, then SMS, whichever are configured, while
    config.otp_live holds (never in the test environment); outside development and test nothing else. In
    development and test, with neither, the console provider (when NEARSHOP_OTP_CONSOLE allows it, the default
    there). Empty: phone sign-in is unavailable."""
    if not config.otp_enabled:
        return []
    live: list[OtpProvider] = []
    # The test suite never reaches a real provider, whatever backend/.env holds; nor does any environment whose
    # rate limiter is off (otp_live).
    if config.environment != "test" and config.otp_live:
        if config.whatsapp_configured:
            live.append(WhatsAppProvider(config))
        if config.sms_configured:
            live.append(SmsProvider(config))
    if live:
        return live
    if config.otp_console and config.environment in ("development", "test"):
        return [ConsoleProvider(config)]
    return []
