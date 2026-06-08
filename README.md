# void-properties

Real estate wholesaling automation system: scrapes leads, stores them in a
local SQLite database, and tracks outreach through the deal pipeline.

## Structure

```
void-properties/
├── config/             # .env (not committed) and example config
├── contracts/          # Purchase/Assignment contract templates + generated/ (gitignored)
├── data/               # local SQLite database file lives here
├── logs/               # run logs
├── scripts/
│   ├── init_db.py              # creates the database from src/db/schema.sql
│   ├── generate_contracts.py   # fills the closing contracts for a lead/deal
│   ├── close_deal.py           # generates + emails a buyer their Assignment Contract once they say yes
│   ├── mao_calculator.py       # Maximum Allowable Offer calculator (CLI)
│   ├── daily_pipeline.py       # the full scrape->resolve->enrich->score->alert run (see "Daily pipeline")
│   ├── run_daily_pipeline.ps1  # wrapper Task Scheduler invokes (logs to logs/daily_pipeline.log)
│   └── setup_daily_task.ps1    # registers the 7 AM Task Scheduler job
└── src/
    ├── db/             # schema + connection helper
    ├── scraper/        # LeadSource implementations + save pipeline
    ├── enrichment/     # address/contact/score enrichment, MAO calculator (mao.py),
    │                   # and buyer-shortlisting (buyer_match.py)
    ├── outreach/       # contact logging and pipeline status tracking
    └── contracts/      # closing-stage contract generation (see "Closing")
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

- `src/scraper/sources/harris_county_buyers.py` — `HarrisCountyBuyerSource`
  searches the same portal (sharing its ASP.NET WebForms search/parsing
  plumbing with `harris_county_deeds` via `_harris_county_clerk.py`) but reads
  the **grantee** side of "DEED" filings to surface likely cash buyers —
  people/entities actively purchasing Harris County property right now. A
  grantee is flagged when its name reads like a real-estate investor/
  wholesaler (`_INVESTOR_PATTERNS` — LLC/LP, "... Properties", "... Capital",
  "Buys Houses", etc.) and/or it shows up as grantee on 2+ *arms-length-
  looking* deeds within the lookback window — a buying *pattern* a regular
  homebuyer wouldn't show.

  "Arms-length-looking" is doing real work there: a first pass over a 30-day
  window found that the single biggest source of "recurring grantee" false
  positives — by a wide margin (it cut the result set from 538 to 191) — was
  **families redistributing property among themselves** (partition/
  distribution deeds, retitling into a family trust), where the grantee
  shares an apparent surname (including hyphenated maiden/married variants —
  "CENO" vs "CENO-WYBLE") with one of that same deed's grantors
  (`_shares_surname_with_grantor`). Those purchases don't count toward the
  recurring signal — though they're still surfaced in `notes` with a "weigh
  less heavily" flag for anyone who wants to sanity-check the call, in the
  same spirit as every other UNVERIFIED marker here: visible for a human to
  judge, not silently discarded.

  Deed records carry no financing info at all, so "cash buyer" here is an
  inference from purchase pattern, not a fact — every result lands in the
  `buyers` table flagged UNVERIFIED, same conservative posture as the seller
  side: confirm via the document image (or HCAD) that a purchase wasn't
  financed before pitching a wholesale assignment to someone. Each run
  recomputes `purchase_count`/`last_purchase_*` fresh from the current
  window and upserts on `(source, buyer_name)`, so overlapping runs correct
  the snapshot rather than double-counting. Run it directly with
  `python -m src.scraper.sources.harris_county_buyers`.

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

  **`enrich_buyers()`** does the same thing for the `buyers` table —
  filling in `buyer_phone`/`buyer_email` (added by `init_db`/`_ensure_columns`;
  see "Database schema") so a hot lead's cash-buyer shortlist (see
  `buyer_match.py` below) comes with a way to actually reach them. Same
  People Match call, same entity filter, same read-then-write-individually
  lock-avoidance pattern — but the entity filter bites much harder here:
  Apollo's `/people/match` only finds *individuals*, and most genuinely-active
  cash buyers are entities (LLCs, trusts, institutional investors) that
  `split_owner_name`/`_ENTITY_NOISE` correctly skip as `skipped_entity` rather
  than mismatch against a person who isn't them. Run both sides with
  `python -m src.enrichment.apollo`, or call `enrich_buyers()` directly.

- `src/enrichment/buyer_match.py` — surfaces a shortlist of cash-buyer
  candidates worth a call about a given hot lead (folded into the hot-lead
  alert email below — see `candidate_summary()`). Worth knowing **why this
  is a shortlist and not a match**: real buyer-seller matching would need a
  shared signal — a buyer's preferred area/property-type/budget against a
  seller's location and likely price — and neither table carries any of that.
  `buyers.last_purchase_address` isn't even a mailing address to geo-match
  against; it's a recorded *legal description* (e.g. `"Desc: CLINTON | Lot: 7
  | Block: 62"`), sharing no vocabulary with `leads.address`
  (`"3703 INDIAN MOUND TRL, CROSBY 77532"`). Rather than fabricate a
  compatibility score from data that can't support one — the same "flag it,
  don't fake it" posture as `claude_score.py`'s equity inference — this just
  ranks buyers by recent activity (has contact info on file, then
  `purchase_count`, then `last_purchase_date`) and hands over an honest
  "liveliest cash-buyer candidates by recent activity" list for Bryan to
  apply his own judgment to. Call `candidate_summary()` for the
  `(buyers, formatted_text)` pair `claude_score._notify_hot_lead` embeds, or
  run `python -m src.enrichment.buyer_match` to preview the current shortlist.

- `src/enrichment/claude_score.py` — ranks each lead 1-10 for outreach
  priority by handing Claude its scraped `motivation_tags` and `notes` (the
  file number, instrument type, and grantor/grantee names a county-records
  source leaves behind) and asking it to weigh **distress signals**, **likely
  equity**, and **motivation**, returning `{"score": 1-10, "rationale": "..."}`
  written back to `leads.deal_score`/`deal_score_rationale`. Worth knowing:
  the `leads` table carries no lien/mortgage/payoff data, so "equity" is never
  a computed figure here — Claude is asked to *qualitatively infer* it from
  textual clues (an old legal description and a probate filing read very
  differently from a fresh judgment lien) and to say plainly when there isn't
  enough to go on. Treat `deal_score` as "which leads to read first", not a
  verified valuation — the same "flag it, don't fake it" posture as every
  other UNVERIFIED marker in this pipeline. Calls the Anthropic Messages API
  directly via `requests` (`claude-haiku-4-5-20251001` by default — cheap
  enough to score every lead, swappable for Sonnet if you want deeper
  reasoning on a smaller batch). Targets leads with `deal_score IS NULL` and
  a non-empty `motivation_tags` (nothing to weigh on a lead with no signal at
  all), reading its candidate list in one short transaction and scoring/
  writing each one individually — same lock-avoidance pattern as
  `apollo.enrich_leads()`. Run it with `python -m src.enrichment.claude_score`,
  or call `score_leads()` directly. Needs `ANTHROPIC_API_KEY` in
  `config/.env` (https://console.anthropic.com/settings/keys) — `ScoreError`
  explains what's missing if you skip that.

  **MAO is wired in here too** — when a lead carries an `estimated_value`
  (this project's only ARV proxy; see "MAO calculator" below for where that
  number can come from and why it's usually empty), scoring also prints a
  light/moderate/heavy MAO range alongside the score and appends the full
  breakdown to `deal_score_rationale`, e.g.:
  `lead #7: scored 8/10 — MAO ~ $119,000-$168,000 (ARV $245,000, light-to-heavy rehab range)`.

  **And any lead scoring 7+ triggers an immediate hot-lead email** straight
  to Bryan (`ALERT_EMAIL`/`ALERT_SCORE_THRESHOLD` in `claude_score.py`) with
  its name, address, score, MAO range, Claude's own rationale for why it's
  promising, **and a shortlist of his liveliest cash-buyer candidates** (see
  `buyer_match.candidate_summary()` just above) — everything he needs to
  move on a hot lead the moment it clears the bar, in one email — sent via
  the same SendGrid integration `outreach_queue.py
  contact ... email` uses (`channels.send_email`, needs `SENDGRID_API_KEY`/
  `SENDGRID_FROM_EMAIL`). This is a notification *to the operator*, not
  outreach to the lead, so it deliberately bypasses `tracker`/`log_outreach`
  — routing it through there would log a false "you contacted this lead"
  event against someone nobody's reached out to. A failed send prints the
  same honest way every other send failure in this pipeline does, and
  doesn't abort the run — a silent alert failure is how a good deal slips
  through.

## MAO calculator

`src/enrichment/mao.py` computes the **Maximum Allowable Offer** — the
ceiling on what you can offer a seller and still leave room for repairs, the
costs of reselling, and the end buyer's required profit:

```
MAO = ARV - (ARV x 8% selling costs) - (ARV x 10% investor profit)
          - (ARV x 4%/yr holding costs, prorated for the holding period)
          - $3,500 closing costs - repair costs
```

Run it directly for a specific, fully-scoped deal:

```
python scripts\mao_calculator.py --arv 220000 --repair-costs 35000 --square-footage 1450 --holding-period 6
```

Square footage doesn't change the MAO — none of the rates above are
per-square-foot — it's used only to print `$/sqft` figures (ARV and offer)
as a sanity check on the result against comparable per-foot pricing in the
area. The holding-cost rate is read as an *annual* rate of ARV and prorated
by the holding period in months — the only reading that both produces
realistic carrying-cost dollar amounts and gives the holding-period input
something to do.

**Where this plugs into scoring**: `claude_score.score_leads()` calls
`mao.quick_estimate()` for any lead with an `estimated_value` on file (see
above). Since `leads` carries no repair-cost, square-footage, or
holding-period data — and `estimated_value` itself is usually NULL for
county-records leads, populated only by manual entry or future enrichment
(this project deliberately doesn't scrape Zillow; see "Lead sources") —
asserting one made-up repair number into a number this confident-looking
would be exactly the mistake "flag it, don't fake it" exists to prevent.
`quick_estimate()` instead runs the same formula across light/moderate/heavy
rehab-scope assumptions (10%/20%/30% of ARV) at a fixed 6-month hold and
returns all three — an honest range, not a guess dressed up as a fact. Once
you've actually scoped a property's repairs, run `mao_calculator.py` directly
for the real number.

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

### Daily pipeline (automated, 7 AM)

`scripts/daily_pipeline.py` runs steps 1-2 above end to end, then scores and
alerts, completely unattended:

```
1a/1b. scrape sellers + buyers  ->  2a/2b. resolve seller addresses (HCAD)
  ->  3a/3b. Apollo-enrich sellers + buyers (phone/email)
  ->  4. Claude-score sellers (+ MAO, + hot-lead alert w/ buyer shortlist)
```

It's registered as the Windows Scheduled Task **`VoidProperties-DailyPipeline`**,
firing every morning at 7:00 AM via `scripts/run_daily_pipeline.ps1` (a thin
wrapper that pins a real `python.exe` — Task Scheduler doesn't reliably
resolve the WindowsApps execution-alias shim `python` resolves to
interactively — and appends timestamped output, including stderr, to
`logs/daily_pipeline.log`). Each stage is wrapped individually (`_stage()`)
so one broken stage (a missing API key, a network blip, a dead source site)
can't take the rest of an unattended morning down with it; failures print
loudly into the log rather than vanishing silently.

```powershell
# register / re-register the task (idempotent — safe to re-run after edits)
powershell -ExecutionPolicy Bypass -File scripts\setup_daily_task.ps1

# check on it
Get-ScheduledTask -TaskName VoidProperties-DailyPipeline | Get-ScheduledTaskInfo

# run it on demand (outside its 7 AM trigger)
Start-ScheduledTask -TaskName VoidProperties-DailyPipeline

# remove it entirely
Unregister-ScheduledTask -TaskName VoidProperties-DailyPipeline -Confirm:$false

# run it by hand without Task Scheduler at all (e.g. to watch it live)
python scripts\daily_pipeline.py              # full run
python scripts\daily_pipeline.py --skip-scrape  # re-run resolve/enrich/score only
```

**What this deliberately does NOT do** — even though the original ask wanted
it all wired into one unattended run, this stops short of emailing buyers
deal terms or generating/sending contracts on a "yes." Both need information
that simply doesn't exist at 7 AM scoring time: a real negotiated purchase
price and assignment fee (`DealTerms` requires these as explicit input —
they can't be inferred from `leads`/`buyers`), and confirmation that a
specific buyer has actually said yes (this system is **outbound-only** —
there's no inbound email/SMS handling to detect a reply with; building that
would mean standing up a public, always-on webhook server, real new
infrastructure this project doesn't have). Sending a real dollar figure, or
a signable legal contract, to a real third party on a guess is a different
order of mistake than a wrong guess anywhere else in this pipeline — so
those stay a manual, human-triggered step once Bryan has real numbers and a
real "yes" in hand. See `scripts/close_deal.py` under "Closing" below.

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

### Following up automatically

A lead that goes quiet after one email is easy to lose track of in a list of
hundreds. `tracker.due_for_followup()` finds leads stuck on `contacted` with
*exactly one* outbound email logged, sent `FOLLOWUP_DELAY_DAYS` (3) or more
days ago and still no reply — long enough that it's not mistaken for a normal
reply-delay, short enough that the lead is still warm — and `send_followups()`
fires the `follow_up_email` template at each of them, logging the result
(success or failure) the same honest way `contact_lead()` always does. A
sent follow-up brings the lead's outbound-email count to 2, so it naturally
won't be picked up again — no risk of nudging the same silent lead forever.

```
python scripts/outreach_queue.py followups               # preview who's due
python scripts/outreach_queue.py followups --send        # actually send them
```

It previews by default; `--send` is required to actually fire — this sends
automated messages to real people, so it doesn't go out silently.

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

## Closing

Once a deal is actually under contract and ready to close, two documents have
to go out: a **Purchase Contract** (you and the seller — the original
acquisition) and an **Assignment of Contract** (you and the end cash buyer —
the wholesale flip that's the whole point of the deal). `contracts/` holds a
plain-text template for each — `purchase_contract_template.txt` and
`assignment_contract_template.txt`, modeled on this project's actual TP
wholesaling forms — with `{}` placeholders in the same `.format()` style
`src/outreach/templates.py` already uses for outreach copy.

`src/contracts/generator.py` fills them in. Run it with:

```
python scripts\generate_contracts.py <lead_id> ^
    --purchase-price 145000 --buyer-name "ABC Capital LLC" ^
    --assignment-fee 12000 --close-date 2026-07-15
```

This writes a completed `lead<id>_purchase_contract_<date>.txt` and
`lead<id>_assignment_contract_<date>.txt` to `contracts/generated/`
(gitignored — these carry real names, addresses, and deal terms).

**Why some fields come from the database and others are CLI flags:**
`seller_name`/`property_address` are standing facts about a `leads` row
(`owner_name`/`address`+`city`+`state`+`zip`) — pulled automatically, same as
every outreach template. But `purchase_price`, `buyer_name` (the end cash
buyer — the "Assignee"), `assignment_fee`, and `close_of_escrow_date` describe
*this one closing* — there's nowhere in `leads`/`buyers` that could hold "what
we agreed to pay for this property, closing on this date" without conflating
a one-time negotiated event with a standing fact about the property. Putting
a wrong number in a document meant to be signed is a different order of
mistake than mis-scraping an address, so `DealTerms` requires these explicitly
rather than guessing from `leads.estimated_value` or anywhere else —
`assignee_purchase_price` (Assignee's Purchase Price) is the one figure
computed automatically, as `purchase_price + assignment_fee`, exactly how the
Assignment contract itself defines it, so the two numbers can never be
entered inconsistently. Bryan Moran / Void Properties fill in as Buyer (in
the Purchase Contract) and Assignor (in the Assignment) from constants —
reusing `templates.SENDER_NAME`/`SENDER_PHONE` so that identity lives in
exactly one place across outreach and contracts.

A handful of blanks the source contracts leave open for case-by-case handling
— earnest money, escrow agent name/address, APN, inspection period,
signature titles/addresses/emails, Assignee deposit specifics — are
deliberately left blank in the generated documents too. Auto-filling those
would mean inventing figures this module has no way to know, which is a worse
failure mode in a signable legal document than a visible blank a human
completes by hand — the same "flag it, don't fake it" posture as every
UNVERIFIED marker elsewhere in this pipeline.

### Closing the loop once a buyer says yes

`scripts/close_deal.py` is the manual, human-triggered step `daily_pipeline.py`
deliberately stops short of (see "Daily pipeline" above for why that line gets
drawn there). Once Bryan has heard a real "yes" from a real buyer — by phone,
by reply, in person — and has the real numbers in hand, this turns that moment
into a generated *and sent* closing packet in one command, reusing
`generate_closing_packet` exactly like `generate_contracts.py` does:

```powershell
# preview — generates both contracts into contracts/generated/ and shows
# exactly what WOULD be emailed and to whom. Sends nothing.
python scripts\close_deal.py <lead_id> ^
    --buyer-name "ABC Capital LLC" --buyer-email "deals@abccapital.com" ^
    --purchase-price 145000 --assignment-fee 12000 --close-date 2026-07-15

# add --send to actually fire — same "preview by default" convention as
# `outreach_queue.py followups`, because this emails a real signable legal
# document to a real third party
python scripts\close_deal.py <lead_id> ... --send
```

**Only the Assignment Contract goes to the buyer** — deliberately. The
Purchase Contract names the seller and what Void Properties is paying *them*;
a buyer who can see the seller's identity and your acquisition price could go
around you straight to the seller and cut you out of your own assignment fee.
(The Assignment Contract already discloses the fee and the assignee's purchase
price — that's legitimately the buyer's business; the seller's identity and
your terms with them aren't.) Both contracts still get written to
`contracts/generated/` and **both** get emailed to Bryan
(`bryantushifukato1213@gmail.com`) as his closing-packet record — same
operator-notification inbox `claude_score`'s hot-lead alerts use, and like
those, this bypasses `tracker`/`log_outreach` (it's a record for Bryan, not
outreach to a lead).

## Database schema

See `src/db/schema.sql` — `leads` (with `deal_score`/`deal_score_rationale`
from `claude_score.py`), `outreach_events` (foreign key to `leads`), and
`buyers` (populated by `harris_county_buyers.py`, upserted on
`(source, buyer_name)`, with `buyer_phone`/`buyer_email` filled in by
`apollo.enrich_buyers` — see "Enrichment"). The `deal_score`/
`deal_score_rationale` and `buyer_phone`/`buyer_email` columns are all added
by `init_db()` itself rather than `CREATE TABLE`/`ALTER ... ADD COLUMN` in
schema.sql — SQLite has no `ADD COLUMN IF NOT EXISTS`, so a plain `ALTER`
there would fail every re-run once the column exists; `database._ensure_columns`
checks `PRAGMA table_info` first and adds only what's missing (see
`_ADDED_COLUMNS` in `src/db/database.py`).
