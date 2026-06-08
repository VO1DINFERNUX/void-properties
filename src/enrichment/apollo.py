"""Apollo.io contact enrichment — resolves owner names into phone/email.

`hcad.py` gets a lead a real mailing/situs address, but neither the Harris
County deed records nor HCAD's bulk export carry any contact info — no
phone, no email. That's the gap between "we know who owns this house" and
"we can actually reach them" that `tracker.contact_lead()` needs filled:
Apollo.io is a paid contact-database API that can match a person's name
(optionally narrowed by location) to a record carrying verified emails and
phone numbers.

Credentials: APOLLO_API_KEY in config/.env
(https://app.apollo.io/#/settings/integrations/api). Calls Apollo's REST API
directly via `requests` — already a dependency, same approach as
`src/outreach/channels.py` — rather than pulling in Apollo's SDK.

Phone numbers are a special case worth knowing about: Apollo only returns a
number synchronously if the record already carries a verified one. A fresh
"reveal" of a withheld number is delivered *asynchronously* to a webhook —
a compliance step on Apollo's end (https://docs.apollo.io/docs/people-
enrichment), not an option this client can just turn on. This module has no
public server to receive that callback, so it only requests a reveal when
APOLLO_PHONE_WEBHOOK_URL names somewhere that can — otherwise it takes
whatever Apollo can hand back directly, which in practice means email
enrichment is the reliable half of this integration and phone is a bonus
when Apollo already has one on file.

`enrich_buyers()` runs the identical match -> write-back flow against the
`buyers` table (`buyer_phone`/`buyer_email`, mirroring `leads.owner_phone`/
`owner_email`) — the prerequisite for ever reaching out to a flagged cash
buyer at all (see `harris_county_buyers.py`: that source only ever captures
a grantee *name* off a deed record, nothing to contact them with). One real
limitation worth knowing up front: Apollo's `/people/match` is a *person*
search, and most of the buyers this project's own source flags as genuinely
active (LLCs, trusts, "... Capital", institutional grantees) are entities,
not individuals — `split_owner_name`'s `_ENTITY_NOISE` filter (built for
exactly this "is there a person here for Apollo to find" question) will
correctly skip them rather than waste credits on a guaranteed miss. In
practice that means this fills in contact info for the minority of buyer
records that read as an individual's name; reaching an LLC or trust means
finding its principal or registered agent — a manual look (Texas Secretary
of State, Harris County records), not something this client can automate.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

from src.db.database import get_connection

load_dotenv(Path(__file__).resolve().parents[2] / "config" / ".env")

APOLLO_MATCH_URL = "https://api.apollo.io/api/v1/people/match"

_REQUEST_TIMEOUT = 30
_DEFAULT_REQUEST_DELAY = 1.0  # be polite / spread out credit-consuming lookups


class ApolloError(RuntimeError):
    """Raised when APOLLO_API_KEY isn't configured or Apollo rejects the key outright."""


def _api_key() -> str:
    key = os.environ.get("APOLLO_API_KEY")
    if not key:
        raise ApolloError("Missing APOLLO_API_KEY — set it in config/.env (see config/.env.example)")
    return key


# Apollo's people-match wants a first/last name for an *individual* — HCAD
# `owner_name` is frequently a trust, estate, bank, or municipal entity
# instead (HCAD bulk data doesn't distinguish), and there's no person behind
# those for Apollo to find. Filtering them out up front saves credits on
# guaranteed misses; this is deliberately broader than hcad.normalize_name's
# suffix list, which only strips noise from names assumed to be a person's.
_ENTITY_NOISE = re.compile(
    r"\b(EST|ESTATE|TRUST|TRUSTEE|LLC|L L C|LLP|LP|PLLC|PLC|LTD|INC|CORP|"
    r"CORPORATION|CO|COMPANY|COMPANIES|BANK|N A|NA|CREDIT UNION|FEDERAL|"
    r"MUTUAL|INSURANCE|LAW FIRM|MORTGAGE|FINANCIAL|LENDING|CAPITAL|REALTY|"
    r"PROPERTIES|HOLDINGS|HOMES|INVESTMENTS?|GROUP|ENTERPRISES|SERVICES|"
    r"PARTNERS|ASSOCIATION|AUTHORITY|DISTRICT|CITY OF|COUNTY|STATE OF|HOUSING|"
    r"CHURCH|MINISTR(?:Y|IES)|FOUNDATION|MANAGEMENT)\b",
    re.I,
)


def split_owner_name(owner_name: str) -> Optional[tuple[str, str]]:
    """Best-effort split of a lead's `owner_name` into (first, last) for Apollo.

    Returns None when the name doesn't look like an individual — see
    `_ENTITY_NOISE` — or is too short/irregular to confidently split. HCAD
    owner names run "LAST FIRST [MIDDLE ...]"; this assumes that order and
    keeps just the first two tokens as (first, last) — a middle name/initial
    would only narrow Apollo's match for no benefit.
    """
    if not owner_name or _ENTITY_NOISE.search(owner_name):
        return None
    parts = owner_name.split()
    if len(parts) < 2:
        return None
    last, first = parts[0], parts[1]
    if not (first.isalpha() and last.isalpha()):
        return None
    return first.title(), last.title()


def match_person(
    first_name: str,
    last_name: str,
    phone_webhook_url: Optional[str] = None,
) -> Optional[dict]:
    """Look up one person on Apollo; return the matched record dict, or None
    if Apollo has nothing for this name. Raises `ApolloError` if
    `APOLLO_API_KEY` is unset or Apollo rejects the key outright (401/403) —
    a hard stop, not a per-lead miss.
    """
    payload = {
        "first_name": first_name,
        "last_name": last_name,
        "reveal_personal_emails": True,
    }
    if phone_webhook_url:
        payload["reveal_phone_number"] = True
        payload["webhook_url"] = phone_webhook_url

    resp = requests.post(
        APOLLO_MATCH_URL,
        headers={"Content-Type": "application/json", "x-api-key": _api_key()},
        json=payload,
        timeout=_REQUEST_TIMEOUT,
    )
    if resp.status_code in (401, 403):
        raise ApolloError(
            f"Apollo rejected the API key (HTTP {resp.status_code}) — check "
            f"APOLLO_API_KEY and that your plan includes People Enrichment: "
            f"{resp.text[:200]}"
        )
    resp.raise_for_status()
    return resp.json().get("person") or None


def _best_phone(person: dict) -> Optional[str]:
    for number in person.get("phone_numbers") or []:
        raw = number.get("sanitized_number") or number.get("raw_number")
        if raw:
            return raw
    return None


def _best_email(person: dict) -> Optional[str]:
    email = person.get("email")
    if email and "email_not_unlocked" not in email:
        return email
    for personal in person.get("personal_emails") or []:
        if personal:
            return personal
    return None


def enrich_leads(limit: Optional[int] = None, request_delay: float = _DEFAULT_REQUEST_DELAY) -> dict[str, int]:
    """Fill in `owner_phone`/`owner_email` for leads Apollo can identify.

    Targets leads that (a) have an `owner_name` to search on, (b) have
    *neither* phone nor email yet — the highest-value gap, and the one that
    keeps credit spend from re-querying leads we've already filled in — and
    (c) already carry a real address (`zip IS NOT NULL`, i.e. `hcad.py` has
    resolved them). That last filter matters: Apollo lookups cost credits,
    and a bare name with no corroborating address is the riskiest kind of
    record to spend them matching.

    Each match is looked up and written back individually — the candidate
    list is read in one short transaction, then each Apollo round-trip (plus
    `request_delay`) happens *outside* any open write transaction, so a slow
    API doesn't hold the leads table's write lock for the whole run (the
    mistake `hcad.cross_reference_leads` had no analog for, since its
    matching is all local-DB lookups with no network I/O in the loop).

    Returns counts: checked, matched, no_match, skipped_entity.
    """
    _api_key()  # fail fast on a missing/blank key before reading anything
    stats = {"checked": 0, "matched": 0, "no_match": 0, "skipped_entity": 0}
    phone_webhook_url = os.environ.get("APOLLO_PHONE_WEBHOOK_URL")

    with get_connection() as conn:
        query = (
            "SELECT id, owner_name FROM leads "
            "WHERE owner_phone IS NULL AND owner_email IS NULL "
            "AND owner_name IS NOT NULL AND zip IS NOT NULL "
        )
        params: list = []
        if limit is not None:
            query += "LIMIT ?"
            params.append(limit)
        leads = [dict(row) for row in conn.execute(query, params).fetchall()]

    for lead in leads:
        stats["checked"] += 1
        split = split_owner_name(lead["owner_name"])
        if split is None:
            stats["skipped_entity"] += 1
            continue
        first, last = split

        person = match_person(first, last, phone_webhook_url)
        phone = email = None
        if person is not None:
            phone = _best_phone(person)
            email = _best_email(person)

        if not (phone or email):
            stats["no_match"] += 1
        else:
            with get_connection() as conn:
                conn.execute(
                    "UPDATE leads SET owner_phone = ?, owner_email = ?, "
                    "updated_at = datetime('now') WHERE id = ?",
                    (phone, email, lead["id"]),
                )
            stats["matched"] += 1

        time.sleep(request_delay)

    return stats


def enrich_buyers(limit: Optional[int] = None, request_delay: float = _DEFAULT_REQUEST_DELAY) -> dict[str, int]:
    """Fill in `buyer_phone`/`buyer_email` for buyers Apollo can identify.

    The buyer-side mirror of `enrich_leads` — same targeting logic (a name to
    search on, neither contact field filled in yet), same `split_owner_name`
    individual-vs-entity filter (grantee names follow the identical "LAST
    FIRST [MIDDLE...]" convention as HCAD owner names — see
    `harris_county_buyers.py`'s docstring), same lock-avoidance shape (read
    the candidate list once, do every Apollo round-trip outside any open
    write transaction, write back individually).

    One thing this *doesn't* mirror: `enrich_leads` requires `zip IS NOT
    NULL` (a signal that `hcad.py` has resolved a real address worth
    spending credits to chase). `buyers` carries no such signal — its
    `last_purchase_address` is a deed *legal description* ("Desc: CLINTON |
    Lot: 7 | Block: 62"), not a mailing address, and there is no resolution
    step that turns it into one. The only pre-filter available here is
    `split_owner_name` itself — which is exactly why most of this table's
    most-active entries (LLCs, trusts, institutions) get skipped as
    `skipped_entity` rather than searched: see this module's docstring for
    why that's Apollo's own search shape, not a gap in this filter.

    Returns counts: checked, matched, no_match, skipped_entity.
    """
    _api_key()  # fail fast on a missing/blank key before reading anything
    stats = {"checked": 0, "matched": 0, "no_match": 0, "skipped_entity": 0}
    phone_webhook_url = os.environ.get("APOLLO_PHONE_WEBHOOK_URL")

    with get_connection() as conn:
        query = (
            "SELECT id, buyer_name FROM buyers "
            "WHERE buyer_phone IS NULL AND buyer_email IS NULL AND buyer_name IS NOT NULL "
        )
        params: list = []
        if limit is not None:
            query += "LIMIT ?"
            params.append(limit)
        buyers = [dict(row) for row in conn.execute(query, params).fetchall()]

    for buyer in buyers:
        stats["checked"] += 1
        split = split_owner_name(buyer["buyer_name"])
        if split is None:
            stats["skipped_entity"] += 1
            continue
        first, last = split

        person = match_person(first, last, phone_webhook_url)
        phone = email = None
        if person is not None:
            phone = _best_phone(person)
            email = _best_email(person)

        if not (phone or email):
            stats["no_match"] += 1
        else:
            with get_connection() as conn:
                conn.execute(
                    "UPDATE buyers SET buyer_phone = ?, buyer_email = ?, "
                    "updated_at = datetime('now') WHERE id = ?",
                    (phone, email, buyer["id"]),
                )
            stats["matched"] += 1

        time.sleep(request_delay)

    return stats


if __name__ == "__main__":
    print("leads:", enrich_leads())
    print("buyers:", enrich_buyers())
