"""Twilio (SMS + voice) and SendGrid (email) send integrations.

This is the layer behind `tracker.contact_lead()` that actually reaches out
to a lead — as opposed to `tracker.log_outreach()`, which just records an
attempt *you* made yourself (a phone call, a knock on the door).

Credentials load from `config/.env` (copy `config/.env.example`):
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER
    SENDGRID_API_KEY, SENDGRID_FROM_EMAIL

Both providers are plain REST APIs over HTTPS with simple auth (Basic for
Twilio, a bearer token for SendGrid), so this talks to them directly with
`requests` — already a dependency — rather than pulling in either SDK. A
missing credential raises `ChannelError` *before* any network call, so a
blank/typo'd .env fails loud immediately rather than as a cryptic 401 mid-send.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / "config" / ".env")

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"
SENDGRID_API_BASE = "https://api.sendgrid.com/v3"

_REQUEST_TIMEOUT = 30


class ChannelError(RuntimeError):
    """Raised when a provider isn't configured — checked before any send."""


@dataclass
class SendResult:
    ok: bool
    provider_id: Optional[str] = None  # Twilio message/call SID, or SendGrid's X-Message-Id
    detail: Optional[str] = None       # provider status on success, error message on failure


def _require(*names: str) -> dict[str, str]:
    values = {name: os.environ.get(name) for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ChannelError(
            f"Missing {', '.join(missing)} — set them in config/.env (see config/.env.example)"
        )
    return values


def send_sms(to: str, body: str) -> SendResult:
    """Send an SMS through Twilio's Programmable Messaging API."""
    creds = _require("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER")
    resp = requests.post(
        f"{TWILIO_API_BASE}/Accounts/{creds['TWILIO_ACCOUNT_SID']}/Messages.json",
        auth=(creds["TWILIO_ACCOUNT_SID"], creds["TWILIO_AUTH_TOKEN"]),
        data={"To": to, "From": creds["TWILIO_FROM_NUMBER"], "Body": body},
        timeout=_REQUEST_TIMEOUT,
    )
    return _twilio_result(resp)


def place_call(to: str, message: str) -> SendResult:
    """Place a voice call through Twilio that reads `message` aloud via TwiML <Say>.

    Passes the TwiML inline through the `Twiml` parameter, so Twilio executes
    it directly when the call connects — no public callback URL / webhook
    server required.
    """
    creds = _require("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER")
    twiml = f"<Response><Say>{_escape_xml(message)}</Say></Response>"
    resp = requests.post(
        f"{TWILIO_API_BASE}/Accounts/{creds['TWILIO_ACCOUNT_SID']}/Calls.json",
        auth=(creds["TWILIO_ACCOUNT_SID"], creds["TWILIO_AUTH_TOKEN"]),
        data={"To": to, "From": creds["TWILIO_FROM_NUMBER"], "Twiml": twiml},
        timeout=_REQUEST_TIMEOUT,
    )
    return _twilio_result(resp)


def _twilio_result(resp: requests.Response) -> SendResult:
    try:
        data = resp.json() if resp.content else {}
    except ValueError:
        data = {}
    if resp.status_code >= 400:
        return SendResult(ok=False, detail=data.get("message") or f"HTTP {resp.status_code}: {resp.text[:200]}")
    return SendResult(ok=True, provider_id=data.get("sid"), detail=data.get("status"))


def send_email(to: str, subject: str, body: str) -> SendResult:
    """Send a plaintext email through SendGrid's Mail Send API (v3)."""
    creds = _require("SENDGRID_API_KEY", "SENDGRID_FROM_EMAIL")
    resp = requests.post(
        f"{SENDGRID_API_BASE}/mail/send",
        headers={
            "Authorization": f"Bearer {creds['SENDGRID_API_KEY']}",
            "Content-Type": "application/json",
        },
        json={
            "personalizations": [{"to": [{"email": to}]}],
            "from": {"email": creds["SENDGRID_FROM_EMAIL"]},
            "subject": subject,
            "content": [{"type": "text/plain", "value": body}],
        },
        timeout=_REQUEST_TIMEOUT,
    )
    if resp.status_code >= 400:
        detail = f"HTTP {resp.status_code}: {resp.text[:200]}"
        try:
            errors = resp.json().get("errors") or []
            if errors:
                detail = "; ".join(e.get("message", "") for e in errors if e.get("message"))
        except ValueError:
            pass
        return SendResult(ok=False, detail=detail)
    # SendGrid returns 202 with an empty body; the message id rides in a header.
    return SendResult(ok=True, provider_id=resp.headers.get("X-Message-Id"), detail=f"HTTP {resp.status_code} accepted")


_XML_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;"), ("'", "&apos;"))


def _escape_xml(text: str) -> str:
    for char, escaped in _XML_ESCAPES:
        text = text.replace(char, escaped)
    return text
