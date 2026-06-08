"""Cash-buyer shortlist for a hot seller lead — what "matching" can honestly mean here.

`claude_score._notify_hot_lead` wants to hand Bryan more than just a
promising seller — it wants to suggest *who might take the assignment*. But
look at what `buyers` actually carries (see `harris_county_buyers.py`): a
name, a recent-purchase count, and that purchase's *legal description*
("Desc: CLINTON | Lot: 7 | Block: 62" — lot/block/subdivision, not a mailing
address). `leads` records a street address ("3703 INDIAN MOUND TRL, CROSBY
77532"). There is no shared field, no overlapping vocabulary, nothing to
join on — and neither table carries anything resembling a buyer's budget,
property-type preference, or service area. Pretending to compute a
"compatibility score" from data that can't support one would be exactly the
kind of confident-looking guess this codebase has consistently refused to
make (see `claude_score`'s equity-gap caveat, `mao.quick_estimate`'s ranges,
every UNVERIFIED flag `harris_county_*` leaves behind).

So this surfaces something true instead of something invented: *your
liveliest cash-buyer candidates right now*, ranked by the same
recent-activity signal `harris_county_buyers.py` already uses to flag them
(purchase_count, recency), with reachable buyers (Apollo-enriched — see
`apollo.enrich_buyers`) bumped to the front, since an unreachable "match" is
no use to anyone. It's an honest "here's who's actively buying in this
market lately, you make the call on whether this property fits their
pattern" shortlist — not a claim that any of them specifically wants THIS
property. Bryan's read of a buyer's notes/history is the part no query can do.
"""
from __future__ import annotations

from typing import Optional

from src.db.database import get_connection

DEFAULT_LIMIT = 5


def candidate_buyers(limit: int = DEFAULT_LIMIT) -> list[dict]:
    """Return up to `limit` buyers worth a look for a hot lead, most-promising first.

    Order: reachable (has a phone or email on file) before unreachable, then
    most arms-length purchases recently, then most recent purchase — i.e.
    "can Bryan actually call them" beats "are they technically the most
    active", which beats "are they still active lately". See the module
    docstring for why this can't be narrowed any further by what the seller's
    property actually looks like.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, buyer_name, buyer_phone, buyer_email, purchase_count,
                   last_purchase_address, last_purchase_date, notes
            FROM buyers
            ORDER BY
                (buyer_phone IS NOT NULL OR buyer_email IS NOT NULL) DESC,
                purchase_count DESC,
                last_purchase_date DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def _contact(buyer: dict) -> str:
    return buyer.get("buyer_phone") or buyer.get("buyer_email") or "no contact info on file yet"


def format_candidates(buyers: list[dict]) -> str:
    """Render `candidate_buyers()` as the block `_notify_hot_lead` tacks onto
    its alert email — one line per buyer, honest about what's known and what
    isn't (see module docstring for why "matched" means "active lately", not
    "wants this property")."""
    if not buyers:
        return (
            "  (no buyer candidates on file yet — run harris_county_buyers "
            "to build the cash-buyer list)"
        )
    lines = []
    for buyer in buyers:
        recency = buyer.get("last_purchase_date") or "date unknown"
        lines.append(
            f"  - {buyer['buyer_name']} -- {buyer['purchase_count']} recent purchase(s), "
            f"most recent {recency} -- {_contact(buyer)}"
        )
    return "\n".join(lines)


def candidate_summary(limit: int = DEFAULT_LIMIT) -> tuple[list[dict], str]:
    """Convenience wrapper: `(candidates, formatted_block)` in one call —
    what `_notify_hot_lead` actually wants."""
    buyers = candidate_buyers(limit=limit)
    return buyers, format_candidates(buyers)


if __name__ == "__main__":
    _, block = candidate_summary()
    print("Your liveliest cash-buyer candidates right now:")
    print(block)
