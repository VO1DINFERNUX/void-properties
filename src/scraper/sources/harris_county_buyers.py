"""Harris County Clerk real-property deed records — likely-cash-buyer source.

Searches the same portal as `harris_county_deeds` (see that module's docstring
for the reverse-engineered details of how its "Instrument Type" search and
result parsing actually behave — both are shared via `_harris_county_clerk`),
but reads the **grantee** side of "DEED" filings instead of the grantor side:
who is *buying* property in this county right now, and does the way they're
buying it look like an investor rather than a homeowner?

Cash buyers don't announce themselves on a public form, so this source treats
two observable signals as worth a manual look — either is enough on its own:

  * INVESTOR-SHAPED NAME — the grantee is an entity whose name reads like a
    real-estate business (LLC/LP, "... Properties", "... Capital", "... Buys
    Houses", etc. — see `_INVESTOR_PATTERNS`) rather than a person's name.
  * RECURRING GRANTEE — the same name receives 2+ deeds within the lookback
    window. A person or company closing on multiple Harris County properties
    in a matter of weeks is very likely buying to hold/flip/wholesale, not to
    live in several houses at once — regardless of whether the name itself
    looks like an entity.

IMPORTANT — these are leads to *qualify*, not confirmed cash buyers: deed
records carry no financing information at all. "Cash" here is an inference
from buying *pattern*, not a fact from the record. Every buyer this source
finds is flagged UNVERIFIED in `notes`, with the same advice the seller-side
scraper gives about its own distress-signal inferences — confirm via the
document image (or HCAD) that the purchase wasn't financed before treating
someone as a cash-buyer lead worth a wholesale pitch.

Buyers are upserted into the `buyers` table (UNIQUE on `source, buyer_name`),
recomputed fresh from the current lookback window on every run rather than
incremented — re-running with an overlapping window corrects `purchase_count`
and `last_purchase_*` to the latest snapshot instead of inflating them.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

import requests

from src.db.database import get_connection

from ._harris_county_clerk import DeedRecord, DeedSearchClient, search_in_chunks

# Entity-name shapes that read as a real-estate investor/wholesaler/flipper
# rather than a person buying a home to live in. Deliberately broad — a false
# positive here just means a buyer lead gets a manual look and turns out to be
# a regular homebuyer with an unusual name; a false negative means a genuine
# investor never surfaces at all. Tuned against common Houston-market patterns,
# not an exhaustive legal-entity taxonomy.
_INVESTOR_PATTERNS = re.compile(
    r"\bL\.?L\.?C\.?\b|\bL\.?P\.?\b|INVESTMENT|PROPERT(Y|IES)|\bHOMES?\b|"
    r"CAPITAL|REALT(Y|OR)|HOLDINGS?|\bGROUP\b|VENTURES?|EQUITY|ACQUISITIONS?|"
    r"\bFUND\b|PARTNERS|ENTERPRISES?|\bREI\b|BUY(S|ING)?\s+HOUSES?|HOUSE\s+BUYERS?",
    re.I,
)

# How many purchases in the lookback window mark a grantee as "recurring"
# even without an investor-shaped name.
_MIN_RECURRING_PURCHASES = 2

_FILE_DATE_FORMAT = "%m/%d/%Y"


@dataclass
class Buyer:
    source: str
    buyer_name: str
    source_ref: Optional[str] = None
    purchase_count: int = 1
    last_purchase_address: Optional[str] = None
    last_purchase_date: Optional[str] = None
    notes: Optional[str] = None


def save_buyers(buyers: list[Buyer]) -> tuple[int, int]:
    """Upsert buyers, refreshing the snapshot fields on conflict.

    Returns (inserted, updated) — `buyers.source, buyer_name` is UNIQUE, so a
    name seen in a prior run gets its purchase_count/last_purchase_* replaced
    with this run's fresh count over the (possibly different) lookback window,
    not incremented on top of the old value.
    """
    inserted = updated = 0
    with get_connection() as conn:
        for buyer in buyers:
            existing = conn.execute(
                "SELECT id FROM buyers WHERE source = ? AND buyer_name = ?",
                (buyer.source, buyer.buyer_name),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO buyers
                    (source, source_ref, buyer_name, purchase_count,
                     last_purchase_address, last_purchase_date, notes, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(source, buyer_name) DO UPDATE SET
                    source_ref            = excluded.source_ref,
                    purchase_count        = excluded.purchase_count,
                    last_purchase_address = excluded.last_purchase_address,
                    last_purchase_date    = excluded.last_purchase_date,
                    notes                 = excluded.notes,
                    updated_at            = datetime('now')
                """,
                (
                    buyer.source,
                    buyer.source_ref,
                    buyer.buyer_name,
                    buyer.purchase_count,
                    buyer.last_purchase_address,
                    buyer.last_purchase_date,
                    buyer.notes,
                ),
            )
            if existing is None:
                inserted += 1
            else:
                updated += 1
    return inserted, updated


def _parsed_file_date(record: DeedRecord) -> datetime:
    """Sort key for "most recent" — file_date is rendered "MM/DD/YYYY", which
    sorts wrong lexicographically (e.g. "12/01/2025" < "06/05/2026" as text)."""
    try:
        return datetime.strptime(record.file_date, _FILE_DATE_FORMAT)
    except ValueError:
        return datetime.min


class HarrisCountyBuyerSource:
    """Pulls likely cash-buyer grantees from recent Harris County DEED filings."""

    name = "harris_county_buyers"

    def __init__(
        self,
        days_back: int = 30,
        chunk_days: int = 3,
        request_delay: float = 2.0,
        contact_email: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ):
        self.days_back = days_back
        self.chunk_days = chunk_days
        self.request_delay = request_delay
        self.client = DeedSearchClient(session=session, contact_email=contact_email)

    def fetch(self) -> list[Buyer]:
        date_to = date.today()
        date_from = date_to - timedelta(days=self.days_back)

        purchases: dict[str, list[DeedRecord]] = defaultdict(list)
        records = search_in_chunks(
            self.client, "DEED", date_from, date_to,
            chunk_days=self.chunk_days, request_delay=self.request_delay, source_name=self.name,
        )
        for record in records:
            for grantee in record.grantees:
                purchases[grantee].append(record)

        buyers: list[Buyer] = []
        for grantee, deeds in purchases.items():
            investor_name = bool(_INVESTOR_PATTERNS.search(grantee))
            recurring = len(deeds) >= _MIN_RECURRING_PURCHASES
            if not (investor_name or recurring):
                continue

            signals = []
            if investor_name:
                signals.append("investor-shaped entity name")
            if recurring:
                signals.append(f"{len(deeds)} purchases in the last {self.days_back} days")

            latest = max(deeds, key=_parsed_file_date)
            buyers.append(Buyer(
                source=self.name,
                source_ref=latest.file_no,
                buyer_name=grantee,
                purchase_count=len(deeds),
                last_purchase_address=latest.legal_description or f"Harris County deed file {latest.file_no}",
                last_purchase_date=latest.file_date,
                notes=(
                    f"Flagged as a likely cash buyer - {'; '.join(signals)}. "
                    f"UNVERIFIED - deed records carry no financing info; confirm via "
                    f"the document image (or HCAD) that the purchase wasn't financed "
                    f"before pitching a wholesale assignment. Most recent: file "
                    f"{latest.file_no} recorded {latest.file_date} "
                    f"(grantor(s): {', '.join(latest.grantors) or 'none listed'})."
                ),
            ))

        return buyers


def run_buyers(sources: Iterable[HarrisCountyBuyerSource]) -> None:
    for source in sources:
        buyers = source.fetch()
        inserted, updated = save_buyers(buyers)
        print(f"[{source.name}] found={len(buyers)} inserted={inserted} updated={updated}")


if __name__ == "__main__":
    run_buyers(sources=[HarrisCountyBuyerSource(days_back=30)])
