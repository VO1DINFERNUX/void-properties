"""Outreach tracking for void-properties.

Logs every contact attempt against a lead (call, SMS, email, direct mail,
door knock) and updates the lead's pipeline status.
"""
from __future__ import annotations

from typing import Optional

from src.db.database import get_connection

VALID_CHANNELS = {"call", "sms", "email", "direct_mail", "door_knock"}
VALID_STATUSES = {
    "new", "contacted", "responded", "negotiating",
    "under_contract", "closed", "dead",
}


def log_outreach(
    lead_id: int,
    channel: str,
    message: Optional[str] = None,
    response: Optional[str] = None,
    outcome: Optional[str] = None,
    direction: str = "outbound",
    new_status: Optional[str] = None,
) -> int:
    """Record an outreach event and optionally advance the lead's status.

    Returns the new outreach_events row id.
    """
    if channel not in VALID_CHANNELS:
        raise ValueError(f"Unknown channel '{channel}', expected one of {VALID_CHANNELS}")
    if new_status is not None and new_status not in VALID_STATUSES:
        raise ValueError(f"Unknown status '{new_status}', expected one of {VALID_STATUSES}")

    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO outreach_events (lead_id, channel, direction, message, response, outcome)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (lead_id, channel, direction, message, response, outcome),
        )
        if new_status is not None:
            conn.execute(
                "UPDATE leads SET status = ?, updated_at = datetime('now') WHERE id = ?",
                (new_status, lead_id),
            )
        return cursor.lastrowid


def history(lead_id: int) -> list[dict]:
    """Return all outreach events for a lead, oldest first."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM outreach_events WHERE lead_id = ? ORDER BY occurred_at ASC",
            (lead_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def pipeline_summary() -> dict[str, int]:
    """Return a count of leads per status, useful for a quick dashboard."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM leads GROUP BY status"
        ).fetchall()
        return {row["status"]: row["count"] for row in rows}


# Lower number = contact sooner. "responded" leads are hot — a reply going
# cold is the costliest mistake in this pipeline, so they jump the queue even
# ahead of brand-new leads. "negotiating" deals are active and need tending;
# "contacted" leads are simply waiting on a follow-up window.
_STATUS_PRIORITY = {"responded": 0, "new": 1, "negotiating": 2, "contacted": 3}


def next_to_contact(limit: int = 20) -> list[dict]:
    """Return a prioritized worklist of leads worth contacting today.

    Excludes terminal statuses (closed, dead) and leads we can't actually
    reach yet — no resolved mailing address (zip) and no phone/email. Sources
    like harris_county_deeds hand back hundreds of leads with placeholder
    addresses; run the enrichment pass (src/enrichment/hcad.py) before
    expecting them to show up here.

    Within the remaining leads, ranks by status (see _STATUS_PRIORITY — a
    reply going cold is worse than a fresh lead going untouched a day longer)
    and, within a status, by how long it's been since the last outreach
    event (or since the lead was created, if never contacted) — most
    overdue first.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                l.*,
                MAX(oe.occurred_at) AS last_contact,
                COALESCE(julianday('now') - julianday(MAX(oe.occurred_at)),
                         julianday('now') - julianday(l.created_at)) AS days_since_activity
            FROM leads l
            LEFT JOIN outreach_events oe ON oe.lead_id = l.id
            WHERE l.status NOT IN ('closed', 'dead')
              AND (l.zip IS NOT NULL OR l.owner_phone IS NOT NULL OR l.owner_email IS NOT NULL)
            GROUP BY l.id
            """
        ).fetchall()

    leads = [dict(row) for row in rows]
    leads.sort(key=lambda l: (_STATUS_PRIORITY.get(l["status"], 99), -l["days_since_activity"]))
    return leads[:limit]


def format_worklist_row(lead: dict) -> str:
    """One human-readable line summarizing a next_to_contact() entry."""
    location = ", ".join(part for part in (lead.get("address"), lead.get("city"), lead.get("zip")) if part)
    contact = lead.get("owner_phone") or lead.get("owner_email") or "no phone/email on file"
    if lead.get("last_contact"):
        recency = f"last contact {lead['days_since_activity']:.0f}d ago"
    else:
        recency = f"never contacted ({lead['days_since_activity']:.0f}d since found)"
    return (
        f"#{lead['id']} [{lead['status']}] {lead.get('owner_name') or 'unknown owner'} "
        f"-- {location or 'no address'} -- {lead.get('motivation_tags') or 'no tags'} "
        f"-- {contact} -- {recency}"
    )
