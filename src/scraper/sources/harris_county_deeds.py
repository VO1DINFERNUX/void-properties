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
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Optional

import requests
from bs4 import BeautifulSoup

from ..scraper import Lead, LeadSource

SEARCH_URL = "https://www.cclerk.hctx.net/applications/websearch/RP.aspx"

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

_RESULT_CAP = 200  # observed maximum rows returned per query — treat as possibly truncated

# Default date-window size per code, tuned against observed daily volumes so
# a chunk stays comfortably under _RESULT_CAP (NOTICE/AFFT run ~75-90/day and
# need narrow windows; PROB/L/P run ~1-2/day and can use wide ones). Used
# unless the caller passes an explicit `chunk_days`.
_DEFAULT_CHUNK_DAYS: dict[str, int] = {
    "PROB": 14,
    "L/P": 14,
    "A/J": 7,
    "DEED": 3,
    "NOTICE": 1,
    "AFFT": 1,
}

_HIDDEN_FIELDS = ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__VIEWSTATEENCRYPTED", "__EVENTVALIDATION")
_FIELD_PREFIX = "ctl00$ContentPlaceHolder1$"
_TEXT_FIELDS = (
    "txtFileNo", "txtFilmCd", "txtFrom", "txtTo", "txtOR", "txtEE", "txtNameTee",
    "txtDesc", "txtInstrument", "txtVolNo", "txtPageNo", "txtSection", "txtLot",
    "txtBlock", "txtUnit", "txtAbstract", "txtOutLot", "txtTract", "txtReserve",
)


@dataclass
class DeedRecord:
    file_no: str
    file_date: str
    instrument_type: str
    grantors: list[str]
    grantees: list[str]
    legal_description: str


def _daterange_chunks(start: date, end: date, chunk_days: int) -> Iterable[tuple[date, date]]:
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


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
        self.session = session or requests.Session()
        ua_contact = f" (+{contact_email})" if contact_email else ""
        self.session.headers.setdefault(
            "User-Agent", f"void-properties-lead-scraper/0.1{ua_contact}"
        )

    # -- ASP.NET WebForms plumbing --------------------------------------------
    # The search form is a postback page: every request must echo back the
    # current __VIEWSTATE / __EVENTVALIDATION hidden fields from the most
    # recent GET, alongside the visible search fields.

    def _form_state(self) -> dict[str, str]:
        resp = self.session.get(SEARCH_URL, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        state = {}
        for field in _HIDDEN_FIELDS:
            tag = soup.find("input", id=field)
            state[field] = tag["value"] if tag and tag.has_attr("value") else ""
        return state

    def _search(self, instrument_code: str, date_from: date, date_to: date) -> str:
        payload = {
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            **self._form_state(),
            **{f"{_FIELD_PREFIX}{name}": "" for name in _TEXT_FIELDS},
            f"{_FIELD_PREFIX}txtFrom": date_from.strftime("%m/%d/%Y"),
            f"{_FIELD_PREFIX}txtTo": date_to.strftime("%m/%d/%Y"),
            f"{_FIELD_PREFIX}txtInstrument": instrument_code,
            f"{_FIELD_PREFIX}btnSearch": "Search",
        }
        resp = self.session.post(SEARCH_URL, data=payload, timeout=60)
        resp.raise_for_status()
        return resp.text

    # -- Result parsing --------------------------------------------------------
    # Each result renders as a <tr class="odd|even"> containing lblFileNo /
    # lblFileDate spans, an instrument-type link, a nested grantor/grantee
    # name table (rows id *_lvOR_ctrl{n}_row, labelled "Grantor"/"Grantee"),
    # and a nested legal-description table (rows id *_lvLegal_ctrl{n}_*).

    @staticmethod
    def _parse_records(html: str) -> list[DeedRecord]:
        soup = BeautifulSoup(html, "html.parser")
        records: list[DeedRecord] = []

        for row in soup.find_all("tr", class_=re.compile(r"^(odd|even)$")):
            file_no = row.select_one('span[id$="_lblFileNo"]')
            file_date = row.select_one('span[id$="_lblFileDate"]')
            if not file_no or not file_date:
                continue

            instrument_link = row.find("a", id=re.compile(r"_lnkdetailtest$"))
            instrument_type = instrument_link.get_text(strip=True) if instrument_link else ""

            grantors: list[str] = []
            grantees: list[str] = []
            for name_row in row.select('tr[id*="_lvOR_ctrl"]'):
                label = name_row.find("b")
                name_span = name_row.select_one('span[id$="_lblNames"]')
                if not label or not name_span:
                    continue
                name = name_span.get_text(strip=True)
                if not name:
                    continue
                (grantors if "grantor" in label.get_text(strip=True).lower() else grantees).append(name)

            legal_parts = [
                text for text in (
                    desc_row.get_text(" ", strip=True)
                    for desc_row in row.select('tr[id*="_lvLegal_"]')
                )
                if text
            ]

            records.append(DeedRecord(
                file_no=file_no.get_text(strip=True),
                file_date=file_date.get_text(strip=True),
                instrument_type=instrument_type,
                grantors=grantors,
                grantees=grantees,
                legal_description=" | ".join(legal_parts),
            ))
        return records

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
            for chunk_start, chunk_end in _daterange_chunks(date_from, date_to, max(1, window)):
                html = self._search(code, chunk_start, chunk_end)
                records = self._parse_records(html)
                if len(records) >= _RESULT_CAP:
                    print(
                        f"[{self.name}] WARNING: '{code}' {chunk_start} to {chunk_end} returned "
                        f"{len(records)} rows (possible truncation at the portal's row cap) - "
                        f"reduce chunk_days for this code to avoid missing records."
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

                time.sleep(self.request_delay)

        return leads


if __name__ == "__main__":
    from ..scraper import run

    run(sources=[HarrisCountyDeedSource(days_back=14)])
