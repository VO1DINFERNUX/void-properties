"""Claude-based deal scoring — ranks each lead 1-10 for outreach priority.

`next_to_contact()` orders the daily worklist by status and recency, but says
nothing about which leads are actually worth chasing hardest — a probate
filing on a paid-off rental and a routine notice with a vague tax-keyword hit
both land in the queue looking the same. This module asks Claude to read each
lead's own scraped signals — `motivation_tags` and `notes` (the file number,
instrument type, and grantor/grantee names a county-records source leaves
behind) — and return a 1-10 score plus a short rationale, written back to
`leads.deal_score` / `leads.deal_score_rationale`.

IMPORTANT — what "equity gap" means here: the `leads` table has no lien,
mortgage-balance, or payoff data — only `estimated_value`, which itself is
usually NULL for county-records leads (deed filings don't carry a sale price
or appraisal). So Claude is never *computing* an equity gap from numbers; it's
making a **qualitative textual inference** — e.g. "probate + a decades-old
legal description suggests the heirs likely own it outright" vs. "an abstract
of judgment implies debt stacked on top of whatever's already owed". The
prompt says this explicitly and asks Claude to reason in those terms, not
fabricate a number it has no basis for. Treat `deal_score` as a triage signal
for *which leads to read first*, not a verified valuation — same spirit as
every "UNVERIFIED" flag elsewhere in this pipeline: a wrong guess that looks
confident is worse than an honest "needs a manual look".

Credentials: ANTHROPIC_API_KEY in config/.env (https://console.anthropic.com/
settings/keys). Calls the Messages API directly via `requests` — same
no-SDK approach as `apollo.py`/`channels.py` — POST
https://api.anthropic.com/v1/messages with `x-api-key` + `anthropic-version`
headers.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

from src.db.database import get_connection

load_dotenv(Path(__file__).resolve().parents[2] / "config" / ".env")

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# Haiku is plenty for a short structured-extraction task like this, and a lead
# list can run into the hundreds — cost-effective enough to score everything,
# not just a sample. Swap to "claude-sonnet-4-6" if you want deeper reasoning
# on a smaller batch; the prompt/parsing here doesn't depend on which model
# answers it.
MODEL = "claude-haiku-4-5-20251001"

_REQUEST_TIMEOUT = 60
_DEFAULT_REQUEST_DELAY = 1.0  # be polite / spread out paid API calls
_MAX_TOKENS = 300

_SCORE_RE = re.compile(r"\{.*\}", re.S)


class ScoreError(RuntimeError):
    """Raised when ANTHROPIC_API_KEY isn't configured or the API rejects the key outright."""


def _api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ScoreError("Missing ANTHROPIC_API_KEY — set it in config/.env (see config/.env.example)")
    return key


_PROMPT_TEMPLATE = """You are helping a real-estate wholesaler in Houston, TX triage a list of \
leads scraped from public county records (probate filings, lis pendens, judgment \
liens, and keyword-flagged deeds/notices). Score how promising THIS ONE lead is \
to chase first, on a scale of 1 (skip it) to 10 (call today).

Weigh three things:
1. DISTRESS SIGNALS — how strong is the evidence this owner is under real \
pressure to sell (not just present in a coarse county-code bucket)? Probate \
with multiple heirs, active litigation, and recorded judgments are stronger \
signals than a routine deed that only matched on a loose keyword.
2. LIKELY EQUITY — there is no lien/mortgage/value data available, so infer \
this qualitatively from what the notes describe: an old legal description, a \
probate/inheritance situation, or a long-held property suggests the owner may \
hold significant equity (or own it outright); a recent judgment or active \
foreclosure-type filing suggests debt may be stacked against whatever equity \
exists. Say plainly when there isn't enough information to tell.
3. MOTIVATION — does the situation described (death in the family, lawsuit, \
debt, tax trouble, an out-of-area or hard-to-reach owner, etc.) suggest someone \
who would genuinely want a fast, no-hassle cash sale over a traditional listing?

Respond with ONLY a JSON object — no markdown, no commentary before or after — \
in exactly this shape:
{{"score": <integer 1-10>, "rationale": "<one or two sentences citing the \
SPECIFIC signals from this lead that drove the score, including what you could \
and couldn't infer about equity>"}}

Lead:
- Motivation tags: {motivation_tags}
- Notes: {notes}
"""


def _build_prompt(lead: dict) -> str:
    return _PROMPT_TEMPLATE.format(
        motivation_tags=lead.get("motivation_tags") or "(none)",
        notes=lead.get("notes") or "(none)",
    )


def _parse_response(text: str) -> tuple[int, str]:
    match = _SCORE_RE.search(text)
    if not match:
        raise ScoreError(f"Couldn't find a JSON object in Claude's response: {text[:200]!r}")
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ScoreError(f"Claude's response wasn't valid JSON: {text[:200]!r}") from exc

    score = parsed.get("score")
    rationale = parsed.get("rationale")
    if not isinstance(score, int) or not (1 <= score <= 10):
        raise ScoreError(f"Claude returned an out-of-range/non-integer score: {parsed!r}")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ScoreError(f"Claude returned no rationale: {parsed!r}")
    return score, rationale.strip()


def score_lead(lead: dict) -> tuple[int, str]:
    """Ask Claude to score one lead. Returns `(score, rationale)`.

    Raises `ScoreError` if `ANTHROPIC_API_KEY` is unset, the API rejects the
    key outright (401/403 — a hard stop, not a per-lead miss), or Claude's
    reply can't be parsed into a valid `{"score": ..., "rationale": ...}`.
    """
    resp = requests.post(
        ANTHROPIC_API_URL,
        headers={
            "x-api-key": _api_key(),
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": _MAX_TOKENS,
            "messages": [{"role": "user", "content": _build_prompt(lead)}],
        },
        timeout=_REQUEST_TIMEOUT,
    )
    if resp.status_code in (401, 403):
        raise ScoreError(
            f"Anthropic rejected the API key (HTTP {resp.status_code}) — check "
            f"ANTHROPIC_API_KEY: {resp.text[:200]}"
        )
    resp.raise_for_status()

    content = resp.json().get("content") or []
    text = "".join(block.get("text", "") for block in content if block.get("type") == "text")
    if not text.strip():
        raise ScoreError(f"Claude returned no text content: {resp.json()!r}")
    return _parse_response(text)


def score_leads(limit: Optional[int] = None, request_delay: float = _DEFAULT_REQUEST_DELAY) -> dict[str, int]:
    """Score every not-yet-scored lead that has something to score.

    Targets leads with `deal_score IS NULL` (so re-runs only pick up freshly
    scraped leads) and a non-empty `motivation_tags` (a lead with none has no
    distress signal at all — there's nothing here for Claude to weigh, and
    scoring it would just be an LLM guessing from a blank page).

    Each lead is scored and written back individually — the candidate list is
    read in one short transaction, then each API round-trip (plus
    `request_delay`) happens *outside* any open write transaction, mirroring
    `apollo.enrich_leads()`'s lock-avoidance: a slow response from a paid API
    should never hold the leads table's write lock open for the whole run.

    A per-lead failure (unparseable response, transient API error) is counted
    and skipped rather than aborting the run — one bad reply shouldn't cost
    you the rest of the batch. Returns counts: checked, scored, failed.
    """
    _api_key()  # fail fast on a missing/blank key before reading anything
    stats = {"checked": 0, "scored": 0, "failed": 0}

    with get_connection() as conn:
        query = (
            "SELECT id, motivation_tags, notes FROM leads "
            "WHERE deal_score IS NULL AND motivation_tags IS NOT NULL AND motivation_tags != '' "
        )
        params: list = []
        if limit is not None:
            query += "LIMIT ?"
            params.append(limit)
        leads = [dict(row) for row in conn.execute(query, params).fetchall()]

    for lead in leads:
        stats["checked"] += 1
        try:
            score, rationale = score_lead(lead)
        except ScoreError as exc:
            print(f"[claude_score] lead #{lead['id']}: {exc}")
            stats["failed"] += 1
            time.sleep(request_delay)
            continue

        with get_connection() as conn:
            conn.execute(
                "UPDATE leads SET deal_score = ?, deal_score_rationale = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (score, rationale, lead["id"]),
            )
        stats["scored"] += 1
        time.sleep(request_delay)

    return stats


if __name__ == "__main__":
    print(score_leads())
