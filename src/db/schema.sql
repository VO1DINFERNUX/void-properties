-- void-properties local database schema (SQLite)

CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,              -- where the lead came from (e.g. "harris_county_deeds", "fsbo_site")
    source_ref      TEXT,                       -- source-native unique id (e.g. county deed file number); NULL when the source has none
    address         TEXT NOT NULL,
    city            TEXT,
    state           TEXT,
    zip             TEXT,
    owner_name      TEXT,
    owner_phone     TEXT,
    owner_email     TEXT,
    estimated_value REAL,
    motivation_tags TEXT,                       -- comma-separated (e.g. "pre-foreclosure,vacant,tax-delinquent")
    status          TEXT NOT NULL DEFAULT 'new' CHECK (status IN
                        ('new', 'contacted', 'responded', 'negotiating',
                         'under_contract', 'closed', 'dead')),
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (address, city, state, zip)
);

CREATE TABLE IF NOT EXISTS outreach_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    channel     TEXT NOT NULL CHECK (channel IN ('call', 'sms', 'email', 'direct_mail', 'door_knock')),
    direction   TEXT NOT NULL DEFAULT 'outbound' CHECK (direction IN ('outbound', 'inbound')),
    message     TEXT,
    response    TEXT,
    outcome     TEXT,                           -- e.g. "no_answer", "interested", "not_interested", "callback_requested"
    occurred_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Real dedup key for sources that hand us a stable native id (e.g. a county
-- deed file number or MLS/listing id). The address-based UNIQUE above is
-- useless for such sources because city/state/zip are often NULL, and SQL
-- treats NULL as distinct from NULL, so it never collides.
CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_source_ref
    ON leads(source, source_ref) WHERE source_ref IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_outreach_lead_id ON outreach_events(lead_id);

-- Claude-based 1-10 scoring lives on `leads.deal_score` /
-- `leads.deal_score_rationale` — added by `init_db()` itself (see
-- database.py's `_ensure_columns`), since SQLite's ALTER TABLE has no
-- `ADD COLUMN IF NOT EXISTS` and `executescript` can't conditionally skip
-- a statement that errors on a column that already exists.

-- Likely cash buyers, surfaced from Harris County deed grantees (see
-- src/scraper/sources/harris_county_buyers.py) — recurring or investor-
-- entity-shaped purchasers worth approaching about a wholesale assignment.
-- Each run recomputes purchase_count/last_purchase_* fresh from the lookback
-- window rather than incrementing, so re-runs don't double-count.
CREATE TABLE IF NOT EXISTS buyers (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    source                TEXT NOT NULL,
    source_ref            TEXT,                  -- most recent deed file number observed for this buyer
    buyer_name            TEXT NOT NULL,         -- grantee name as recorded (often an entity)
    purchase_count        INTEGER NOT NULL DEFAULT 1,
    last_purchase_address TEXT,
    last_purchase_date    TEXT,
    notes                 TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source, buyer_name)
);

-- buyer_phone/buyer_email — added by init_db() itself (see database.py's
-- _ensure_columns, same ALTER-TABLE-guarded-by-PRAGMA pattern as
-- leads.deal_score above). Filled in by src.enrichment.apollo.enrich_buyers,
-- the buyer-side mirror of enrich_leads — see that function's docstring for
-- why most of the *active* buyers here (LLCs, trusts, institutions) won't
-- get a hit through Apollo's person-match API even so.

CREATE INDEX IF NOT EXISTS idx_buyers_purchase_count ON buyers(purchase_count);
