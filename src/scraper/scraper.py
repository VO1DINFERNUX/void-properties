"""Lead scraping framework for void-properties.

Each data source (county records, FSBO sites, tax-delinquent lists, etc.)
should implement `LeadSource.fetch()` and return a list of `Lead` objects.
The scraper then upserts them into the local database, skipping duplicates
on (address, city, state, zip).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.db.database import get_connection


@dataclass
class Lead:
    source: str
    address: str
    source_ref: Optional[str] = None  # source-native unique id (e.g. deed file number) for real dedup
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None
    owner_name: Optional[str] = None
    owner_phone: Optional[str] = None
    owner_email: Optional[str] = None
    estimated_value: Optional[float] = None
    motivation_tags: list[str] = field(default_factory=list)
    notes: Optional[str] = None


class LeadSource:
    """Base class for a scrapeable source of leads."""

    name: str = "unknown"

    def fetch(self) -> list[Lead]:
        raise NotImplementedError


def save_leads(leads: list[Lead]) -> tuple[int, int]:
    """Insert new leads, ignoring duplicates. Returns (inserted, skipped)."""
    inserted = skipped = 0
    with get_connection() as conn:
        for lead in leads:
            try:
                conn.execute(
                    """
                    INSERT INTO leads
                        (source, source_ref, address, city, state, zip, owner_name,
                         owner_phone, owner_email, estimated_value,
                         motivation_tags, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lead.source,
                        lead.source_ref,
                        lead.address,
                        lead.city,
                        lead.state,
                        lead.zip,
                        lead.owner_name,
                        lead.owner_phone,
                        lead.owner_email,
                        lead.estimated_value,
                        ",".join(lead.motivation_tags) if lead.motivation_tags else None,
                        lead.notes,
                    ),
                )
                inserted += 1
            except Exception:
                skipped += 1
    return inserted, skipped


def run(sources: list[LeadSource]) -> None:
    for source in sources:
        leads = source.fetch()
        inserted, skipped = save_leads(leads)
        print(f"[{source.name}] fetched={len(leads)} inserted={inserted} skipped={skipped}")


if __name__ == "__main__":
    from src.scraper.sources.harris_county_deeds import HarrisCountyDeedSource

    # Register your LeadSource implementations here as you build them.
    run(sources=[HarrisCountyDeedSource()])
