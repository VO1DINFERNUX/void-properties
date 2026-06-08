"""Shared plumbing for searching the Harris County Clerk real-property portal.

Both `harris_county_deeds` (seller-distress leads, built from grantors) and
`harris_county_buyers` (cash-buyer leads, built from grantees) search the same
ASP.NET WebForms page (https://www.cclerk.hctx.net/applications/websearch/RP.aspx)
and parse the same result rows into `DeedRecord`s. That machinery was
reverse-engineered against a live, undocumented portal — see the module
docstring in `harris_county_deeds` for the full notes on its quirks (exact-
match instrument codes, the ~200-row cap, the postback/__VIEWSTATE dance).
Keeping it in one place means a markup change only needs fixing once, instead
of being chased down independently in two scrapers that happen to look similar
today and drift apart tomorrow.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Optional

import requests
from bs4 import BeautifulSoup

SEARCH_URL = "https://www.cclerk.hctx.net/applications/websearch/RP.aspx"

_RESULT_CAP = 200  # observed maximum rows returned per query — treat as possibly truncated

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


def daterange_chunks(start: date, end: date, chunk_days: int) -> Iterable[tuple[date, date]]:
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


def parse_records(html: str) -> list[DeedRecord]:
    """Parse a results page into `DeedRecord`s.

    Each result renders as a <tr class="odd|even"> containing lblFileNo /
    lblFileDate spans, an instrument-type link, a nested grantor/grantee name
    table (rows id *_lvOR_ctrl{n}_row, labelled "Grantor"/"Grantee"), and a
    nested legal-description table (rows id *_lvLegal_ctrl{n}_*).
    """
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


class DeedSearchClient:
    """Wraps the postback/__VIEWSTATE dance the search form requires.

    Every request must echo back the current __VIEWSTATE / __EVENTVALIDATION
    hidden fields from the most recent GET, alongside the visible search
    fields — `search()` re-fetches that state before each query rather than
    caching it, since the portal rotates it per page load and a stale token
    gets the POST silently rejected back to an empty form.
    """

    def __init__(self, session: Optional[requests.Session] = None, contact_email: Optional[str] = None):
        self.session = session or requests.Session()
        ua_contact = f" (+{contact_email})" if contact_email else ""
        self.session.headers.setdefault(
            "User-Agent", f"void-properties-lead-scraper/0.1{ua_contact}"
        )

    def _form_state(self) -> dict[str, str]:
        resp = self.session.get(SEARCH_URL, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        state = {}
        for field in _HIDDEN_FIELDS:
            tag = soup.find("input", id=field)
            state[field] = tag["value"] if tag and tag.has_attr("value") else ""
        return state

    def search(self, instrument_code: str, date_from: date, date_to: date) -> list[DeedRecord]:
        """Run one instrument-code/date-range query and return parsed records."""
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
        return parse_records(resp.text)


def search_in_chunks(
    client: DeedSearchClient,
    code: str,
    date_from: date,
    date_to: date,
    chunk_days: int,
    request_delay: float,
    source_name: str,
) -> Iterable[DeedRecord]:
    """Run `client.search` across date-range chunks, yielding records as found.

    Centralizes the truncation warning every chunked search needs — the
    portal caps each query at `_RESULT_CAP` rows with no pager, so a chunk
    that comes back at the cap may be silently missing records (see
    `harris_county_deeds`'s module docstring for the per-code tuning this
    implies).
    """
    for chunk_start, chunk_end in daterange_chunks(date_from, date_to, max(1, chunk_days)):
        records = client.search(code, chunk_start, chunk_end)
        if len(records) >= _RESULT_CAP:
            print(
                f"[{source_name}] WARNING: '{code}' {chunk_start} to {chunk_end} returned "
                f"{len(records)} rows (possible truncation at the portal's row cap) - "
                f"reduce chunk_days for this code to avoid missing records."
            )
        yield from records
        time.sleep(request_delay)
