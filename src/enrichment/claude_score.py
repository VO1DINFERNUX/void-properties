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

MAO — when a lead does carry an `estimated_value` (an ARV proxy — see
`src/enrichment/mao.py` for the calculator and why repair scope/holding
period get treated as a labeled range here rather than asserted numbers),
`score_leads()` also runs `mao.quick_estimate()` against it, prints a one-line
MAO range alongside the score, and appends the full breakdown to
`deal_score_rationale` (see `_mao_estimate_block`). Same posture as the
equity-gap caveat above: a range that's honest about what it doesn't know
beats a single number that looks more certain than it is.

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
from src.enrichment import buyer_match, mao
from src.outreach import channels

load_dotenv(Path(__file__).resolve().parents[2] / "config" / ".env")

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# Where (and at what score) to send a heads-up about a promising lead — see
# `_notify_hot_lead`. Bryan's own inbox, not a lead's — this is a notification
# *to the operator*, not outreach to anyone in `leads`.
ALERT_EMAIL = "bryantushifukato1213@gmail.com"
ALERT_SCORE_THRESHOLD = 7

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


def _mao_estimate_block(arv: float) -> tuple[str, str]:
    """Render `mao.quick_estimate(arv)` as `(one_line_summary, rationale_block)`.

    `arv` here is `leads.estimated_value` — the only "estimated value" this
    project's schema carries (whatever enrichment or manual entry has put
    there; this project deliberately doesn't scrape Zillow — see mao.py's
    module docstring). It's usually NULL for county-records leads, which is
    why this only ever runs when there's actually a number to work with (see
    `score_leads`).

    Returns a short range for the per-lead console line and a fuller
    line-item block to tack onto the persisted rationale — both built from
    the same `quick_estimate()` range so the two can never disagree.
    """
    estimates = mao.quick_estimate(arv)
    lo, hi = estimates[-1][1].mao, estimates[0][1].mao  # heaviest rehab -> lowest MAO, lightest -> highest

    summary = f"MAO ~ ${lo:,.0f}-${hi:,.0f} (ARV ${arv:,.0f}, light-to-heavy rehab range)"

    block_lines = [
        f"MAO estimate (ARV = this lead's estimated_value, ${arv:,.0f} — repair scope unknown, "
        f"so shown as a range across rehab assumptions):"
    ]
    for label, result in estimates:
        block_lines.append(f"  - {label}: MAO ~ ${result.mao:,.0f}")
    block_lines.append(
        f"  Assumes a {mao.DEFAULT_HOLDING_PERIOD_MONTHS:.0f}-month hold and the standard "
        f"{mao.SELLING_COST_RATE:.0%}/{mao.INVESTOR_PROFIT_RATE:.0%}/{mao.HOLDING_COST_RATE:.0%}/"
        f"${mao.CLOSING_COSTS:,.0f} selling/profit/holding/closing assumptions — once repairs are "
        f"actually scoped, run `python scripts/mao_calculator.py` for a precise figure."
    )
    return summary, "\n".join(block_lines)


def _lead_address(lead: dict) -> str:
    parts = [lead.get("address"), lead.get("city"), lead.get("state"), lead.get("zip")]
    return ", ".join(p for p in parts if p) or "(address unknown)"


def _notify_hot_lead(lead: dict, score: int, rationale: str, mao_summary: Optional[str]) -> None:
    """Email a heads-up to `ALERT_EMAIL` for any lead scoring >= `ALERT_SCORE_THRESHOLD`.

    This is a notification *to the operator* about a promising lead, not
    outreach to the lead — it goes straight through `channels.send_email`
    rather than `tracker.contact_lead`/`log_outreach`, which exist to track
    communication *with leads* (routing this through them would log a false
    "you contacted this lead" event against someone nobody's reached out to
    yet).

    Also attaches a cash-buyer shortlist (`buyer_match.candidate_summary`) —
    Bryan's liveliest recent buyer candidates, surfaced here rather than
    contacted automatically: see that module's docstring for why "matching"
    can only honestly mean "active lately", not "wants this property" (the
    schema carries no location/budget/criteria data to match against), and
    `scripts/daily_pipeline.py`'s module docstring for why automated
    buyer-facing outreach and contract delivery stay manual, human-triggered
    steps (`scripts/close_deal.py`) rather than firing from this alert.

    A failed send is printed the same honest way every other send failure in
    this pipeline is (`contact_lead`, `send_followups`, etc.) — a notification
    that silently fails to fire is exactly how a good deal gets missed, and
    `score_leads()` shouldn't abort its run over it either way (one broken
    alert shouldn't cost you the rest of the batch's scores).
    """
    owner = lead.get("owner_name") or "(unknown owner)"
    subject = f"Hot lead alert: {owner} scored {score}/10"

    _, buyer_block = buyer_match.candidate_summary()

    body = (
        f"A lead just scored {score}/10 during deal scoring — at or above "
        f"the alert threshold ({ALERT_SCORE_THRESHOLD}/10).\n\n"
        f"Owner:      {owner}\n"
        f"Address:    {_lead_address(lead)}\n"
        f"Deal score: {score}/10\n"
        f"MAO range:  {mao_summary or 'not available — no estimated_value on file for this lead yet'}\n\n"
        f"Why it scored well:\n{rationale}\n\n"
        f"Buyers worth a call about this one (your liveliest cash-buyer\n"
        f"candidates by recent activity — see src/enrichment/buyer_match.py\n"
        f"for why this is a shortlist to apply your own judgment to, not a\n"
        f"claim that any of them specifically wants this property):\n"
        f"{buyer_block}\n\n"
        f"-- sent automatically by claude_score.score_leads() (lead #{lead['id']})"
    )
    try:
        result = channels.send_email(ALERT_EMAIL, subject, body)
    except channels.ChannelError as exc:
        print(f"[claude_score] lead #{lead['id']}: couldn't send hot-lead alert — {exc}")
        return
    if result.ok:
        print(f"[claude_score] lead #{lead['id']}: hot-lead alert emailed to {ALERT_EMAIL}")
    else:
        print(f"[claude_score] lead #{lead['id']}: hot-lead alert FAILED — {result.detail}")


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

    MAO — when a lead carries an `estimated_value` (this project's only ARV
    proxy; see `_mao_estimate_block`'s docstring for why it's usually NULL
    and where a number in it would have come from), this also runs
    `mao.quick_estimate()` against it, prints a one-line MAO range alongside
    the score, and appends the full light/moderate/heavy breakdown to the
    rationale that gets written to `deal_score_rationale` — so the range is
    both visible the moment scoring runs and still there on a later look at
    the lead.

    Hot-lead alerts — any lead scoring `>= ALERT_SCORE_THRESHOLD` (7) gets an
    immediate email to `ALERT_EMAIL` with its name, address, score, MAO range,
    and Claude's own rationale for why it's promising (see `_notify_hot_lead`).
    This is a notification to the operator, not outreach to the lead, so it
    bypasses `tracker`/`log_outreach` entirely — see that function's docstring
    for why routing it through there would be actively misleading.
    """
    _api_key()  # fail fast on a missing/blank key before reading anything
    stats = {"checked": 0, "scored": 0, "failed": 0}

    with get_connection() as conn:
        query = (
            "SELECT id, owner_name, address, city, state, zip, "
            "motivation_tags, notes, estimated_value FROM leads "
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

        # Keep Claude's own rationale separate from the persisted version —
        # the alert email's "why it scored well" should be Claude's qualitative
        # read alone, with the MAO range called out as its own field rather
        # than buried inside a wall of appended text.
        arv = lead.get("estimated_value")
        mao_summary: Optional[str] = None
        stored_rationale = rationale
        if arv:
            mao_summary, mao_block = _mao_estimate_block(arv)
            stored_rationale = f"{rationale}\n\n{mao_block}"

        print(
            f"[claude_score] lead #{lead['id']}: scored {score}/10"
            + (f" — {mao_summary}" if mao_summary else "")
        )

        if score >= ALERT_SCORE_THRESHOLD:
            _notify_hot_lead(lead, score, rationale, mao_summary)

        with get_connection() as conn:
            conn.execute(
                "UPDATE leads SET deal_score = ?, deal_score_rationale = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (score, stored_rationale, lead["id"]),
            )
        stats["scored"] += 1
        time.sleep(request_delay)

    return stats


if __name__ == "__main__":
    print(score_leads())
