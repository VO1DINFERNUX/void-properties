# void-properties

Real estate wholesaling automation system: scrapes leads, stores them in a
local SQLite database, and tracks outreach through the deal pipeline.

## Structure

```
void-properties/
├── config/             # .env (not committed) and example config
├── data/               # local SQLite database file lives here
├── logs/               # run logs
├── scripts/
│   └── init_db.py      # creates the database from src/db/schema.sql
└── src/
    ├── db/             # schema + connection helper
    ├── scraper/        # LeadSource implementations + save pipeline
    └── outreach/       # contact logging and pipeline status tracking
```

## Setup

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config\.env.example config\.env   # then fill in real credentials
python scripts\init_db.py
```

## Lead sources

- `src/scraper/sources/harris_county_deeds.py` — `HarrisCountyDeedSource`
  scrapes the public Harris County Clerk real-property records search for
  recent filings that signal a distressed/motivated seller (probate, lis
  pendens, judgment liens, and keyword-filtered deeds/notices/affidavits
  for foreclosure/tax/heirship language). Deed records only carry a legal
  description, not a mailing address — every lead is flagged in `notes` as
  needing cross-reference against HCAD (hcad.org) before outreach. Run it
  directly with `python -m src.scraper.sources.harris_county_deeds`.

- **Zillow FSBO was deliberately not built.** Zillow's Terms of Use
  explicitly prohibit scraping ("You may not use any robot, spider, scraper
  or other automated means...") and they don't offer a public API for this —
  only licensed partner access. Scraping them directly risks IP bans and
  legal exposure. If/when you want Houston FSBO leads, prefer a licensed
  data API (RentCast/RealtyMole, Estated, ATTOM, Datafiniti, BatchLeads) or
  an FSBO-specific site whose terms allow it.

## Enrichment

- `src/enrichment/hcad.py` — resolves the placeholder legal-description
  addresses that `harris_county_deeds` leaves behind into real situs/mailing
  addresses, by cross-referencing against the Harris County Appraisal
  District's account data. HCAD's search UI (search.hcad.org,
  hcad.org/property-search) is Cloudflare-bot-walled — same posture as
  Zillow, so we don't scrape it — but HCAD also publishes the same data as a
  sanctioned bulk download ("PDATA": `Real_acct_owner.zip`, ~210MB, no
  auth/rate-limit). This module downloads two exports from it — `real_acct.txt`
  (~1.6M accounts: owner "mailto" name, mailing/situs address, legal
  description) and `owners.txt` (~1.9M individual co-owner rows) — into a
  local lookup DB (`data/hcad/hcad_lookup.db`), then matches each unresolved
  lead against it on three signals: normalized owner-name overlap (grantor
  *and* grantees, against both the account's `mailto` and its individual
  co-owners — `mailto` alone is often a generic aggregate like "CURRENT OWNER"
  that won't resemble a person's name), legal-description agreement
  (subdivision/lot/block/section), and *corroboration* — how many
  independent deed-side names (e.g. several probate heirs) each land on a
  different co-owner of the same account. A match is trusted, and
  `leads.address/city/state/zip` + `notes` updated in place, only when one
  signal is strong enough on its own (near-exact name, legal description
  agreement, or 2+ corroborating names) — guessing wrong is worse than
  leaving a lead flagged for a manual look. Run the full pipeline
  (download → load → match) with `python -m src.enrichment.hcad`, or call
  `cross_reference_leads()` directly to re-match without re-downloading —
  it only touches leads still carrying the scraper's "UNVERIFIED" flag, so
  re-running after improving the matcher picks up where the last pass left
  off (two passes so far have resolved 218 of 585 Harris County leads).

  Two outcomes besides "resolved": (1) **duplicate_property** — a second
  lead resolves to an address another lead already claimed (the `leads`
  table enforces one row per address) — usually two distress events on one
  property (e.g. a lien *and* a probate filing), flagged in `notes` for
  manual merging rather than overwritten; (2) **ambiguous/no_match** — left
  with the original placeholder + "UNVERIFIED" flag for a manual HCAD lookup,
  most often because the deed record's legal description was just "SEE
  INSTRUMENT" (no section/lot/block to disambiguate a common surname) and no
  candidate name was distinctive enough to resolve on its own.

- `src/enrichment/apollo.py` — fills in `owner_phone`/`owner_email` so the
  Twilio/SendGrid integration (see "Sending automatically" under "Outreach")
  has something to send to. Neither the deed records nor HCAD's bulk export
  carry contact info — `hcad.py` only gets you a real *address*. This module
  takes leads HCAD has already resolved (`zip IS NOT NULL`) with a
  person-shaped `owner_name` (entity/trust/bank names are filtered out — see
  `_ENTITY_NOISE` — there's no person behind those for Apollo to find) and
  matches them against Apollo.io's contact database via its People Match API
  (`POST /api/v1/people/match`), writing back whatever phone/email it finds.
  Run it with `python -m src.enrichment.apollo`, or call `enrich_leads()`
  directly. Needs `APOLLO_API_KEY` in `config/.env`
  (https://app.apollo.io/#/settings/integrations/api) — `ApolloError`
  explains what's missing if you skip that.

  Worth knowing: Apollo only returns a phone number synchronously if the
  record already carries a verified one — revealing a withheld number is an
  *async, webhook-only* flow on Apollo's end (a compliance step, not a
  client-side toggle). This module has no public server to receive that
  callback, so it only requests a reveal when `APOLLO_PHONE_WEBHOOK_URL`
  names somewhere that can; otherwise it takes whatever Apollo hands back
  directly. In practice that makes **email the reliable half** of this
  integration and phone numbers a bonus when Apollo already has one on file.

  Each Apollo lookup is a real network round-trip (plus a deliberate
  `request_delay` between them to spread out credit-consuming calls), so —
  unlike `hcad.py`'s all-local-DB matching — `enrich_leads()` reads its
  candidate list in one short transaction and then writes each match back
  individually, so a slow API response never holds the `leads` table's write
  lock open across the whole run.

## Pipeline

1. **Scrape** — implement a `LeadSource` in `src/scraper/scraper.py` for each
   data source (county records, FSBO listings, tax-delinquent lists, etc.)
   and register it in `run()`. Leads are upserted into the `leads` table,
   deduped on (address, city, state, zip) or, where a source provides a
   stable native id (e.g. a deed file number), on (source, source_ref).
2. **Enrich** — run `src/enrichment/hcad.py` to resolve placeholder addresses,
   then `src/enrichment/apollo.py` to fill in phone/email for those resolved
   leads (see "Enrichment" above) — both before outreach.
3. **Work the queue** — `python scripts/outreach_queue.py` shows a prioritized
   worklist of who to contact today (see "Outreach" below); `... log <id>
   <channel> ...` records an attempt you made yourself, `... contact <id>
   <channel> ...` actually sends one via Twilio/SendGrid.
4. **Review pipeline** — `src/outreach/tracker.pipeline_summary()` (also
   printed at the top of the queue) gives a quick count of leads per status.

## Outreach

`src/outreach/tracker.py` logs every call/SMS/email/direct-mail/door-knock
attempt (`outreach_events`, FK to `leads`) and advances a lead's `status`
(new → contacted → responded → negotiating → under_contract → closed/dead).
On top of that, `next_to_contact()` builds a prioritized daily worklist:

- **Filters out leads you can't act on yet** — no resolved mailing address
  (`zip`) and no phone/email. Freshly scraped Harris County leads sit here
  until `src/enrichment/hcad.py` resolves a real address; running it earns
  you a bigger queue.
- **Ranks "responded" leads first** — replying late to someone who already
  raised their hand is the costliest mistake in this pipeline — then never-
  contacted "new" leads, then active "negotiating" deals, then "contacted"
  leads simply waiting on a follow-up window. Within a tier, the longest-
  overdue lead (by time since its last `outreach_events` row, or since it was
  found if never contacted) goes first.

Run it day to day with `scripts/outreach_queue.py`:

```
python scripts/outreach_queue.py                 # pipeline summary + worklist
python scripts/outreach_queue.py --limit 10
python scripts/outreach_queue.py log 42 call --outcome interested --status responded
python scripts/outreach_queue.py log 17 direct_mail --message "Cash offer letter sent"
```

### Sending automatically (Twilio + SendGrid)

`log` records an attempt *you* made elsewhere. `src/outreach/channels.py` +
`tracker.contact_lead()` add the other half — actually placing the SMS/voice
call (Twilio) or sending the email (SendGrid) — wired up via the `contact`
subcommand:

```
python scripts/outreach_queue.py contact 42 sms   --message "Hi, this is..."
python scripts/outreach_queue.py contact 42 call  --message "Hi, this is a recorded message for..."
python scripts/outreach_queue.py contact 42 email --subject "Cash offer" --message "..."

# or send canned, on-brand copy with no --message at all:
python scripts/outreach_queue.py contact 42 sms   --template sms
python scripts/outreach_queue.py contact 42 call  --template voicemail
python scripts/outreach_queue.py contact 42 email --template intro_email
python scripts/outreach_queue.py contact 42 email --template follow_up_email
```

It looks up the lead's `owner_phone`/`owner_email`, sends through the
provider, and logs the outcome to `outreach_events` either way — a failed
send (bad number, bounce, unverified sender) is as visible in the lead's
history as a successful one, and the lead's `status` only advances on success
(default target: `contacted`, override with `--status`). `direct_mail` and
`door_knock` have no API to drive and stay on `log`.

`--template` (`src/outreach/templates.py`) is the faster path day to day —
four ready-written, personal-not-corporate messages (`intro_email`,
`follow_up_email`, `sms`, `voicemail`) signed as Bryan Moran from the Twilio
number, with `{first_name}`/`{property_address}` filled in automatically from
that lead's own `owner_name`/`address` (`templates.render()`) — no typing a
`--message` or `--subject` by hand, and no risk of a copy-paste mismatch
between what you meant to send and what went out.

Both providers are plain REST APIs, called directly with `requests` (no SDK
dependency). Set these in `config/.env` (template in `config/.env.example`)
before using `contact` — `ChannelError` explains exactly what's missing if
you don't:

```
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_FROM_NUMBER=        # E.164, e.g. +18325551234
SENDGRID_API_KEY=
SENDGRID_FROM_EMAIL=       # must be a verified sender in your SendGrid account
```

## Database schema

See `src/db/schema.sql` — two tables: `leads` and `outreach_events`
(foreign key to `leads`).
