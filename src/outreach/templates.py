"""Canned outreach message templates for void-properties cold outreach.

Each template carries `{first_name}`/`{property_address}` placeholders that
`render()` fills in from a lead's own `leads` row — `owner_name` and `address`
— right before sending. `contact_lead(..., template="intro_email")` wires this
straight into the send path, so working the queue never requires typing out a
`--message` by hand.

`SENDER_NAME`/`SENDER_PHONE` are the sender identity baked into every
template: Bryan's Twilio number, not a personal line — it's the number
outreach already sends from (`TWILIO_FROM_NUMBER`), so replies funnel back
through the same tracked system rather than a personal cell.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

SENDER_NAME = "Bryan Moran"
SENDER_PHONE = "(901) 627-3454"


@dataclass(frozen=True)
class Template:
    body: str
    subject: Optional[str] = None


TEMPLATES: dict[str, Template] = {
    "intro_email": Template(
        subject="Quick note about {property_address}",
        body=(
            "Hi {first_name},\n\n"
            "I'll keep this short — my name's {sender_name}, and I buy houses "
            "directly from owners here in the Houston area. I came across some "
            "public records connected to {property_address} and wanted to "
            "reach out personally instead of sending you a form letter.\n\n"
            "I'm not a big company or a call center — just someone who buys "
            "houses directly, deals straight with the owner, and tries to make "
            "the whole thing as easy as possible if it's ever something you'd "
            "consider. No realtors, no listings, no waiting around on "
            "financing that might fall through.\n\n"
            "If selling has crossed your mind — for any reason, no judgment "
            "either way — I'd genuinely like to hear from you. And if it "
            "hasn't, no worries at all, just wanted to put a real name and "
            "number in front of you in case it's useful down the road.\n\n"
            "Feel free to reply here or reach me directly at {sender_phone}.\n\n"
            "Take care,\n{sender_name}\n{sender_phone}"
        ),
    ),
    "follow_up_email": Template(
        subject="Following up — {property_address}",
        body=(
            "Hey {first_name},\n\n"
            "Just circling back in case my last email got lost in the shuffle "
            "— totally understandable if it did, life gets busy.\n\n"
            "I'm still around and still interested in {property_address} if "
            "selling is ever something you'd want to talk through. No "
            "pressure if the timing's not right, or if things are already "
            "handled on your end — I just didn't want you to think I'd "
            "disappeared.\n\n"
            "If texting's easier than email, that works too: {sender_phone}. "
            "Hope all's well with you either way.\n\n"
            "{sender_name}"
        ),
    ),
    "sms": Template(
        body=(
            "Hey {first_name}, this is {sender_name} - I buy houses directly "
            "here in the Houston area and wanted to reach out personally "
            "about {property_address}. No pressure at all, just figured I'd "
            "ask in case selling's ever crossed your mind. Feel free to call "
            "or text me back anytime, even just to say \"not interested\" "
            "is appreciated."
        ),
    ),
    "voicemail": Template(
        body=(
            "Hey {first_name}, this is {sender_name} — sorry I missed you. "
            "I buy houses directly from owners here in Houston, and I came "
            "across some info connected to the property at "
            "{property_address}. I wasn't sure if selling was something "
            "you'd ever think about, but if it is, I'd love to just talk it "
            "through — no pressure, no obligation. My number's "
            "{sender_phone}. Feel free to call or text whenever works for "
            "you, even if the answer's just \"no thanks\" — I'd rather know "
            "either way. Thanks, and take care."
        ),
    ),
}


def _first_name(owner_name: Optional[str]) -> str:
    """Best-effort first name for a greeting — falls back to "there".

    HCAD owner names run "LAST FIRST [MIDDLE...]"; entity names (LLCs,
    trusts, banks, etc.) and anything too short/irregular to confidently
    read as a person's name fall back to a generic greeting rather than
    risk addressing someone as "Hi Estate," or "Hi Trust,".
    """
    if not owner_name:
        return "there"
    parts = owner_name.split()
    if len(parts) < 2 or not parts[1].isalpha():
        return "there"
    return parts[1].title()


def _property_address(lead: dict) -> str:
    return lead.get("address") or "your property"


def render(template_name: str, lead: dict) -> tuple[Optional[str], str]:
    """Fill in a template's placeholders from a lead row.

    Returns `(subject, body)` — `subject` is `None` for the SMS/voicemail
    templates, which have none.
    """
    if template_name not in TEMPLATES:
        raise ValueError(f"Unknown template '{template_name}', expected one of {sorted(TEMPLATES)}")
    template = TEMPLATES[template_name]
    fields = {
        "first_name": _first_name(lead.get("owner_name")),
        "property_address": _property_address(lead),
        "sender_name": SENDER_NAME,
        "sender_phone": SENDER_PHONE,
    }
    subject = template.subject.format(**fields) if template.subject else None
    return subject, template.body.format(**fields)
