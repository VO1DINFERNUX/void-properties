"""Harris County Clerk real-property deed records — distress-signal source.

Searches the public Real Property records portal
(https://www.cclerk.hctx.net/applications/websearch/RP.aspx) for recently
filed documents that tend to indicate a motivated/distressed seller, and
turns matches into Leads for manual qualification.

How the portal's "Instrument Type" search actually behaves (reverse-engineered
by querying it directly — there is no documented API):

  * It is an EXACT match against a short internal code, not a free-text/
    contains search on the descriptive name. Probing the field against a
    sample of real results showed the live code set is:
        A/J, AFFT, AGMT, ASSGN, BOND, CERT, D/T, DECREE, DEED, FI STM,
        L AFFT, L/P, MODIF, NOTICE, ORDER, P/A, POAMC, PROB, PT REL, REL,
        SUPD/T, W/D
    e.g. "QUITCLAIM DEED" or "TRUSTEE DEED" matches nothing — those
    sub-types are filed simply under the broad "DEED" or "NOTICE" codes,
    and the descriptive sub-type is only visible on the document image
    itself (which costs $1+/page to view/print).
  * Each query appears to cap out at 200 rows, with no visible pager. We
    chunk the date range per code (see _DEFAULT_CHUNK_DAYS, tuned against
    observed daily volumes) and log a warning whenever a chunk comes back
    at the cap. Even a single-day window can occasionally hit 200 on the
    busiest codes (NOTICE in particular swings widely day to day) — there
    is no narrower window to fall back to, so some truncation on those
    codes is an inherent limit of this search UI, not a bug here. That's
    fine for sampling leads; if you need a *complete* feed, use the
    county's bulk-data channel mentioned below instead.

Because the short codes are coarse buckets, this source treats two groups
differently:
  * DIRECT_SIGNAL_CODES — the code itself is the signal (e.g. "PROB" =
    probate filing, "L/P" = lis pendens, "A/J" = judgment lien). Every
    matching record becomes a lead.
  * FILTERED_CODES — broad buckets ("DEED", "NOTICE", "AFFT") that mostly
    contain routine filings. We only keep a record if a grantor/grantee
    name contains a distress keyword (ESTATE OF, TRUSTEE, TAX, SHERIFF, …).

IMPORTANT — addresses: deed records carry grantor/grantee names and a
*legal* description (subdivision / section / lot / block), not a mailing or
situs address. Each Lead's `address` is populated with the legal description
as a placeholder, and `notes` flags the record for cross-reference against
the Harris County Appraisal District public property search
(https://hcad.org/property-search/) to resolve a real address, owner mailing
address, and (where available) phone — before any outreach. Every lead from
this source should be treated as "needs manual qualification", not contact-
ready: the instrument-type bucket is a hint, not a confirmed distress signal.

This is a read-only scrape of a public government records search explicitly
provided for looking up recorded documents. Be a good citizen of a shared
public resource — keep `request_delay` reasonable and prefer narrow date
windows over huge ranges. For serious volume, Harris County offers a
sanctioned bulk-data / FTP channel: contact datasales@cco.hctx.net (see
https://www.cclerk.hctx.net/RealProperty.aspx).
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Iterable, Optional

import requests

from ..scraper import Lead, LeadSource
from ._harris_county_clerk import DeedRecord, DeedSearchClient, search_in_chunks

# Codes where the instrument type itself is the distress signal — every
# matching record is kept.
DIRECT_SIGNAL_CODES: dict[str, str] = {
    "PROB": "probate-inheritance",  # probate filings — heirs are frequently motivated sellers
    "L/P": "pending-litigation",    # lis pendens — property tied up in a lawsuit (incl. foreclosure/partition/divorce)
    "A/J": "judgment-lien",         # abstract of judgment — unpaid money judgment recorded against the owner
}

# Broad buckets that mostly contain routine filings — only kept when a
# grantor/grantee name matches a distress keyword (see _infer_tag below).
FILTERED_CODES: tuple[str, ...] = ("DEED", "NOTICE", "AFFT")

_PROBATE_KEYWORDS = re.compile(r"ESTATE OF|HEIR|DECEASED|DECEDENT", re.I)
_FORECLOSURE_KEYWORDS = re.compile(r"TRUSTEE|SUBSTITUTE|DEFAULT|FORECLOS", re.I)
_TAX_KEYWORDS = re.compile(r"\bTAX\b|SHERIFF|CONSTABLE|TAX ASSESSOR|DELINQUEN", re.I)

# Default date-window size per code, tuned against observed daily volumes so
# a chunk stays comfortably under the portal's ~200-row cap (NOTICE/AFFT run
# ~75-90/day and need narrow windows; PROB/L/P run ~1-2/day and can use wide
# ones). Used unless the caller passes an explicit `chunk_days`.
_DEFAULT_CHUNK_DAYS: dict[str, int] = {
    "PROB": 14,
    "L/P": 14,
    "A/J": 7,
    "DEED": 3,
    "NOTICE": 1,
    "AFFT": 1,
}


def _infer_tag(names: list[str]) -> Optional[str]:
    """Classify a FILTERED_CODES record by keyword hits in its party names."""
    joined = " | ".join(names)
    if _PROBATE_KEYWORDS.search(joined):
        return "probate-inheritance"
    if _FORECLOSURE_KEYWORDS.search(joined):
        return "pre-foreclosure"
    if _TAX_KEYWORDS.search(joined):
        return "tax-delinquent"
    return None


class HarrisCountyDeedSource(LeadSource):
    """Pulls distress-signal deed filings from the Harris County Clerk portal."""

    name = "harris_county_deeds"

    def __init__(
        self,
        codes: Iterable[str] = (*DIRECT_SIGNAL_CODES, *FILTERED_CODES),
        days_back: int = 14,
        chunk_days: Optional[int] = None,
        request_delay: float = 2.0,
        contact_email: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ):
        valid_codes = {*DIRECT_SIGNAL_CODES, *FILTERED_CODES}
        self.codes = [c for c in codes if c in valid_codes]
        self.days_back = days_back
        # None => use the per-code defaults above; an explicit value overrides
        # every code uniformly (handy for quick/manual test runs).
        self.chunk_days = chunk_days
        self.request_delay = request_delay
        self.client = DeedSearchClient(session=session, contact_email=contact_email)

    # -- LeadSource interface ---------------------------------------------------

    def _build_lead(self, record: DeedRecord, motivation_tag: str) -> Lead:
        # The grantor is the outgoing/current owner — the contact target.
        owner_name = record.grantors[0] if record.grantors else None
        return Lead(
            source=self.name,
            source_ref=record.file_no,
            address=record.legal_description or f"Harris County deed file {record.file_no}",
            city="Houston",
            state="TX",
            owner_name=owner_name,
            motivation_tags=[motivation_tag, f"instrument:{record.instrument_type.lower()}"],
            notes=(
                f"Deed file {record.file_no} recorded {record.file_date} "
                f"(code: {record.instrument_type}). "
                f"Grantor(s): {', '.join(record.grantors) or 'none listed'}; "
                f"Grantee(s): {', '.join(record.grantees) or 'none listed'}. "
                f"UNVERIFIED - county code is a coarse bucket, not a confirmed distress "
                f"signal. Before outreach: (1) view the document image to confirm the "
                f"actual instrument sub-type, (2) cross-reference the legal description "
                f"and owner name against HCAD (hcad.org) to resolve a situs/mailing "
                f"address and contact info."
            ),
        )

    def fetch(self) -> list[Lead]:
        date_to = date.today()
        date_from = date_to - timedelta(days=self.days_back)
        leads: list[Lead] = []
        seen_file_nos: set[str] = set()

        for code in self.codes:
            direct_tag = DIRECT_SIGNAL_CODES.get(code)
            window = self.chunk_days if self.chunk_days is not None else _DEFAULT_CHUNK_DAYS.get(code, 3)
            records = search_in_chunks(
                self.client, code, date_from, date_to,
                chunk_days=window, request_delay=self.request_delay, source_name=self.name,
            )
            for record in records:
                if record.file_no in seen_file_nos:
                    continue

                if direct_tag is not None:
                    tag = direct_tag
                else:
                    tag = _infer_tag(record.grantors + record.grantees)
                    if tag is None:
                        continue  # routine filing in a broad bucket — not a lead

                seen_file_nos.add(record.file_no)
                leads.append(self._build_lead(record, tag))

        return leads


if __name__ == "__main__":
    from ..scraper import run

    run(sources=[HarrisCountyDeedSource(days_back=14)])
