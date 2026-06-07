"""Cross-reference leads against HCAD bulk property data to resolve addresses.

Harris County Clerk deed records (see ../scraper/sources/harris_county_deeds.py)
only carry grantor/grantee names and a *legal* description — no mailing or
situs address. The Harris County Appraisal District (HCAD) has that data, but
its public search UI (search.hcad.org / hcad.org/property-search) sits behind
a Cloudflare bot challenge — the same posture as Zillow, and for the same
reason we didn't build a Zillow scraper, we don't fight that here either.

HCAD instead publishes the same underlying data as a sanctioned bulk download
("PDATA"): https://hcad.org/hcad-online-services/pdata/ → Property Data →
Real_acct_owner.zip, served as a plain static file with no auth or rate
limiting (~210MB zipped). We pull two files out of it:
  * `real_acct.txt` — one row per account: account number, owner name
    ("mailto"), owner mailing address, situs/property address, and a 4-line
    legal description (lgl_1..lgl_4).
  * `owners.txt` — one row per (account, co-owner): every individual name on
    the account, separately from the `mailto` aggregate. This matters because
    `mailto` is frequently a generic value ("CURRENT OWNER", a trust/LLC name)
    that won't resemble any individual's name, while the deed side often names
    several individuals (e.g. a probate filing's heirs as grantees) — so
    matching against co-owners too, and counting how many independent
    deed-side names land on the same account, recovers a lot of matches that
    `mailto`-only name matching misses.
This module downloads both files, loads the fields we need into a local
lookup DB, and matches leads against it by owner name — corroborated by
legal-description agreement and/or multiple independent co-owner hits — to
fill in real addresses.

Workflow:
    download_real_acct()     -> data/hcad/real_acct.txt, owners.txt
    build_lookup_db()        -> data/hcad/hcad_lookup.db  (hcad_accounts, hcad_owners)
    cross_reference_leads()  -> updates leads.address/city/state/zip + notes

Re-running is safe and cheap: the lookup DB is rebuilt from the local extract
(no repeat download), and cross_reference_leads() only touches leads whose
notes still carry the scraper's "UNVERIFIED - county code is a coarse bucket"
sentinel (see _UNRESOLVED_MARKER) — i.e. ones a prior run didn't confidently
resolve.
"""
from __future__ import annotations

import csv
import re
import sqlite3
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

from ..db.database import get_connection

REAL_ACCT_ZIP_URL = "https://download.hcad.org/data/CAMA/2026/Real_acct_owner.zip"
# Files we extract from the zip: the per-account export, and the per-co-owner
# export (one row per (acct, ln_num) — see build_lookup_db for why both matter).
_EXTRACT_MEMBERS = ("real_acct.txt", "owners.txt")

HCAD_DIR = Path(__file__).resolve().parents[2] / "data" / "hcad"
ZIP_PATH = HCAD_DIR / "Real_acct_owner.zip"
TXT_PATH = HCAD_DIR / "real_acct.txt"
OWNERS_TXT_PATH = HCAD_DIR / "owners.txt"
LOOKUP_DB_PATH = HCAD_DIR / "hcad_lookup.db"

# The deed scraper (harris_county_deeds._build_lead) tags every lead's notes
# with this exact phrase as its "still needs HCAD lookup" sentinel. Matching on
# it both selects the leads to process and (once replaced) prevents reprocessing.
_UNRESOLVED_MARKER = "UNVERIFIED - county code is a coarse bucket"

_NAME_NOISE = re.compile(
    r"\b(EST|ESTATE OF|ESTATE|TRUSTEE|TRUST|TR|JR|SR|II|III|IV|ET AL|ETAL|"
    r"LLC|INC|LP|LTD|CO|DECD|DECEASED)\b"
)
_NON_ALNUM = re.compile(r"[^A-Z0-9 ]")
_WHITESPACE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """Uppercase, strip punctuation/entity & relationship suffixes, collapse whitespace.

    Both deed-record grantor/grantee names ("ROLDAN NICKIE R EST") and HCAD's
    `mailto` owner names are "LAST FIRST [MIDDLE] [suffix]"-shaped, so
    normalizing both sides the same way makes them directly comparable.
    """
    if not name:
        return ""
    cleaned = _NON_ALNUM.sub(" ", name.upper())
    cleaned = _NAME_NOISE.sub(" ", cleaned)
    return _WHITESPACE.sub(" ", cleaned).strip()


def _name_tokens(normalized: str) -> list[str]:
    return normalized.split(" ") if normalized else []


def _token_overlap(a: list[str], b: list[str]) -> float:
    if not a or not b:
        return 0.0
    shared = len(set(a) & set(b))
    return shared / max(len(a), len(b))


@dataclass
class LegalTokens:
    subdivision: Optional[str] = None
    section: Optional[str] = None
    lot: Optional[str] = None
    block: Optional[str] = None

    @property
    def is_empty(self) -> bool:
        return not any((self.subdivision, self.section, self.lot, self.block))


_LEGAL_FIELD_RE = re.compile(r"(Desc|Sec|Lot|Block):\s*([^|]+)")


def parse_lead_legal(legal_description: str) -> LegalTokens:
    """Parse the 'Desc: X | Sec: N | Lot: N | Block: N' strings the deed
    scraper stores in `leads.address` back into structured components."""
    fields = {m.group(1).lower(): m.group(2).strip() for m in _LEGAL_FIELD_RE.finditer(legal_description or "")}
    return LegalTokens(
        subdivision=fields.get("desc") or None,
        section=fields.get("sec") or None,
        lot=fields.get("lot") or None,
        block=fields.get("block") or None,
    )


def score_legal_match(lead_legal: LegalTokens, hcad_legal_text: str) -> int:
    """Cheap agreement score between our parsed legal description and HCAD's
    raw `lgl_1..lgl_4` text (typically like "LT 7 BLK 3" / "TERRA DEL SOL" /
    "SEC 9"). Higher = more confident the two records describe the same parcel."""
    if lead_legal.is_empty or not hcad_legal_text:
        return 0
    text = hcad_legal_text.upper()
    score = 0
    if lead_legal.subdivision and lead_legal.subdivision.upper() in text:
        score += 2
    if lead_legal.lot and re.search(rf"\bLOTS?\s+{re.escape(lead_legal.lot)}\b", text):
        score += 1
    if lead_legal.block and re.search(rf"\bBLO?C?K?\.?\s+{re.escape(lead_legal.block)}\b", text):
        score += 1
    if lead_legal.section and re.search(rf"\bSECT?(?:ION)?\.?\s+{re.escape(lead_legal.section)}\b", text):
        score += 1
    return score


_GRANTEE_RE = re.compile(r"Grantee\(s\):\s*([^.]+)\.")


def parse_grantees(notes: str) -> list[str]:
    """Pull grantee names back out of the notes string the deed scraper writes
    ("...Grantee(s): NAME ONE, NAME TWO. UNVERIFIED...")."""
    m = _GRANTEE_RE.search(notes or "")
    if not m or "none listed" in m.group(1).lower():
        return []
    return [n.strip() for n in m.group(1).split(",") if n.strip()]


def situs_address(row: sqlite3.Row) -> str:
    return row["site_addr_1"] or ""


# -- Step 1: download -------------------------------------------------------

def download_real_acct(force: bool = False) -> Path:
    """Download Real_acct_owner.zip from HCAD's public bulk-data channel and
    extract real_acct.txt + owners.txt. Skips the (slow, ~210MB) download if
    already present."""
    HCAD_DIR.mkdir(parents=True, exist_ok=True)

    if not ZIP_PATH.exists() or force:
        with requests.get(
            REAL_ACCT_ZIP_URL,
            headers={"User-Agent": "void-properties-enrichment/0.1 (public bulk data download)"},
            stream=True,
            timeout=300,
        ) as resp:
            resp.raise_for_status()
            with open(ZIP_PATH, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    f.write(chunk)

    with zipfile.ZipFile(ZIP_PATH) as zf:
        for member in _EXTRACT_MEMBERS:
            dest = HCAD_DIR / member
            if dest.exists() and not force:
                continue
            with zf.open(member) as src, open(dest, "wb") as dst:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    dst.write(chunk)

    return TXT_PATH


# -- Step 2: load into a local lookup table ---------------------------------

def _load_tsv(conn, txt_path, table, num_columns, row_to_values, batch_size):
    """Stream a tab-delimited HCAD export into `table` in batches. `row_to_values`
    converts a csv.DictReader row (keyed by the TSV's own header) into the
    `num_columns`-length tuple matching the destination table's column order."""
    total = 0
    batch: list[tuple] = []
    with open(txt_path, encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        placeholders = ",".join("?" * num_columns)
        insert_sql = f"INSERT INTO {table} VALUES ({placeholders})"
        for row in reader:
            batch.append(row_to_values(row))
            if len(batch) >= batch_size:
                conn.executemany(insert_sql, batch)
                total += len(batch)
                batch.clear()
        if batch:
            conn.executemany(insert_sql, batch)
            total += len(batch)
    return total


def build_lookup_db(hcad_dir: Path = HCAD_DIR, db_path: Path = LOOKUP_DB_PATH, batch_size: int = 5000) -> dict[str, int]:
    """Load the columns we care about from real_acct.txt and owners.txt into a
    local SQLite lookup keyed/indexed by normalized owner name.

    Two tables, because a single account's `mailto` is often a generic
    aggregate ("CURRENT OWNER", a trust/LLC name) that won't match any
    individual person's name — while `owners.txt` lists each co-owner
    separately (one row per (acct, ln_num)), which is exactly what shows up
    on the deed side for e.g. probate leads naming several heirs as
    grantees. Matching against *both* lets a lead corroborate a candidate
    account via several independent owner-name hits, not just the one
    `mailto` string. Returns row counts loaded per table.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute(
            """
            CREATE TABLE hcad_accounts (
                acct        TEXT PRIMARY KEY,
                owner_name  TEXT,
                norm_name   TEXT,
                mail_addr_1 TEXT,
                mail_addr_2 TEXT,
                mail_city   TEXT,
                mail_state  TEXT,
                mail_zip    TEXT,
                site_addr_1 TEXT,
                site_addr_2 TEXT,
                site_addr_3 TEXT,
                legal_text  TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE hcad_owners (
                acct      TEXT,
                name      TEXT,
                norm_name TEXT
            )
            """
        )

        def account_values(row):
            owner_name = (row.get("mailto") or "").strip()
            legal_text = " ".join((row.get(f"lgl_{i}") or "").strip() for i in (1, 2, 3, 4)).strip()
            return (
                row.get("acct", "").strip(),
                owner_name,
                normalize_name(owner_name),
                row.get("mail_addr_1", "").strip(),
                row.get("mail_addr_2", "").strip(),
                row.get("mail_city", "").strip(),
                row.get("mail_state", "").strip(),
                row.get("mail_zip", "").strip(),
                row.get("site_addr_1", "").strip(),
                row.get("site_addr_2", "").strip(),
                row.get("site_addr_3", "").strip(),
                legal_text,
            )

        def owner_values(row):
            name = (row.get("name") or "").strip()
            return (row.get("acct", "").strip(), name, normalize_name(name))

        counts = {
            "hcad_accounts": _load_tsv(
                conn, hcad_dir / "real_acct.txt", "hcad_accounts",
                12, account_values, batch_size,
            ),
            "hcad_owners": _load_tsv(
                conn, hcad_dir / "owners.txt", "hcad_owners",
                3, owner_values, batch_size,
            ),
        }

        conn.execute("CREATE INDEX idx_hcad_accounts_norm_name ON hcad_accounts(norm_name)")
        conn.execute("CREATE INDEX idx_hcad_owners_norm_name ON hcad_owners(norm_name)")
        conn.execute("CREATE INDEX idx_hcad_owners_acct ON hcad_owners(acct)")
        conn.commit()
        return counts
    finally:
        conn.close()


# -- Step 3: match leads against the lookup table ---------------------------
#
# Matching purely against an account's `mailto` (as the first pass did) misses
# a lot: `mailto` is frequently a generic aggregate — "CURRENT OWNER", a trust
# or LLC name — that won't resemble any individual person's name, even though
# the *deed* grantor/grantees are individuals (heirs, spouses, etc.). HCAD's
# `owners.txt` carries each co-owner separately (one row per (acct, ln_num)),
# so we search and score against those too, and treat "N independent deed-side
# names each match a distinct co-owner on the same account" as strong
# corroboration in its own right — exactly the shape of a probate lead naming
# several heirs as grantees.

def _candidate_accounts(hcad_conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    """Find HCAD accounts where either the `mailto` owner *or* any individual
    co-owner's normalized name shares the same first token (i.e. last name,
    given the deed records' "LAST FIRST ..." format) as `name`."""
    tokens = _name_tokens(normalize_name(name))
    if not tokens:
        return []
    prefix = f"{tokens[0]} %"
    return hcad_conn.execute(
        """
        SELECT * FROM hcad_accounts WHERE norm_name LIKE :prefix
        UNION
        SELECT a.* FROM hcad_accounts a
        JOIN hcad_owners o ON o.acct = a.acct
        WHERE o.norm_name LIKE :prefix
        LIMIT 25
        """,
        {"prefix": prefix},
    ).fetchall()


def _account_owner_token_lists(hcad_conn: sqlite3.Connection, account: sqlite3.Row) -> list[list[str]]:
    """All names associated with an account (the `mailto` aggregate plus every
    individual co-owner from owners.txt), each pre-tokenized for overlap scoring."""
    rows = hcad_conn.execute(
        "SELECT norm_name FROM hcad_owners WHERE acct = ?", (account["acct"],)
    ).fetchall()
    token_lists = [_name_tokens(r["norm_name"]) for r in rows]
    token_lists.append(_name_tokens(account["norm_name"]))
    return [tl for tl in token_lists if tl]


def _best_match(hcad_conn: sqlite3.Connection, candidate_names: list[str], lead_legal: LegalTokens):
    """Score each candidate account the lead's grantor/grantee names turn up:
      - name_score: best single-name token overlap against any owner on the
        account (mailto or an individual co-owner)
      - legal_score: agreement between the lead's parsed legal description and
        the account's lgl_1..4 text (subdivision/lot/block/section)
      - corroboration: how many *distinct* deed-side candidate names each
        independently match a different owner on the account — multiple
        independent hits (e.g. three heirs each matching a co-owner row) is
        strong evidence even when no single overlap is near-exact and the
        legal description is empty ("SEE INSTRUMENT")
    Returns (account_row, name_score, legal_score, corroboration) or None.
    """
    best = None
    seen_accts: set[str] = set()
    cand_token_lists = [tl for tl in (_name_tokens(normalize_name(n)) for n in candidate_names) if tl]

    for name in candidate_names:
        name_tokens = _name_tokens(normalize_name(name))
        if not name_tokens:
            continue
        for account in _candidate_accounts(hcad_conn, name):
            if account["acct"] in seen_accts:
                continue
            seen_accts.add(account["acct"])

            owner_token_lists = _account_owner_token_lists(hcad_conn, account)
            if not owner_token_lists:
                continue

            name_score = max(_token_overlap(name_tokens, ot) for ot in owner_token_lists)
            corroboration = sum(
                1 for ct in cand_token_lists
                if max(_token_overlap(ct, ot) for ot in owner_token_lists) >= 0.5
            )
            if name_score < 0.5 and corroboration < 2:
                continue

            legal_score = score_legal_match(lead_legal, account["legal_text"])
            candidate = (account, name_score, legal_score, corroboration)
            if best is None or (legal_score, corroboration, name_score) > (best[2], best[3], best[1]):
                best = candidate
    return best


def cross_reference_leads(limit: Optional[int] = None) -> dict[str, int]:
    """Match unresolved Harris County deed leads against the local HCAD lookup
    table and update their address fields in place. Returns counts."""
    stats = {"checked": 0, "resolved": 0, "duplicate_property": 0, "ambiguous": 0, "no_match": 0}

    hcad_conn = sqlite3.connect(LOOKUP_DB_PATH)
    hcad_conn.row_factory = sqlite3.Row
    try:
        with get_connection() as conn:
            query = (
                "SELECT id, owner_name, address, notes FROM leads "
                "WHERE source = 'harris_county_deeds' AND notes LIKE ? "
            )
            params: list = [f"%{_UNRESOLVED_MARKER}%"]
            if limit is not None:
                query += "LIMIT ?"
                params.append(limit)
            leads = conn.execute(query, params).fetchall()

            for lead in leads:
                stats["checked"] += 1
                lead_legal = parse_lead_legal(lead["address"])
                candidate_names = [n for n in (lead["owner_name"], *parse_grantees(lead["notes"])) if n]
                match = _best_match(hcad_conn, candidate_names, lead_legal)

                if match is None:
                    stats["no_match"] += 1
                    continue

                account, name_score, legal_score, corroboration = match
                # Trust the match if any one signal is strong on its own:
                #   - legal description agrees on subdivision/lot/block/etc., or
                #   - the name is a near-exact match, or
                #   - 2+ independent deed-side names (e.g. several heirs) each
                #     match a distinct co-owner on the same account.
                # Wrong auto-fills are worse than "still needs a manual look",
                # so anything short of one of these stays flagged.
                if legal_score < 2 and name_score < 0.8 and corroboration < 2:
                    stats["ambiguous"] += 1
                    continue

                # Drop the scraper's "UNVERIFIED ... cross-reference against HCAD"
                # tail (it's now done) and keep the deed-file/grantor-grantee prefix.
                prefix = lead["notes"].split(_UNRESOLVED_MARKER, 1)[0].rstrip()
                resolved_note = (
                    f"{prefix} RESOLVED VIA HCAD: acct {account['acct']}, "
                    f"owner '{account['owner_name']}', situs '{situs_address(account)}, "
                    f"{account['site_addr_2']} {account['site_addr_3']}', mailing "
                    f"'{account['mail_addr_1']} {account['mail_addr_2']}, "
                    f"{account['mail_city']} {account['mail_state']} {account['mail_zip']}' "
                    f"(name_score={name_score:.2f}, legal_score={legal_score}, "
                    f"corroborating_names={corroboration}). Still confirm the actual "
                    f"instrument sub-type via the document image before outreach."
                )

                try:
                    conn.execute(
                        """
                        UPDATE leads
                           SET address = ?, city = ?, state = ?, zip = ?, notes = ?,
                               updated_at = datetime('now')
                         WHERE id = ?
                        """,
                        (
                            situs_address(account) or lead["address"],
                            account["site_addr_2"] or "Houston",
                            "TX",
                            account["site_addr_3"] or None,
                            resolved_note,
                            lead["id"],
                        ),
                    )
                    stats["resolved"] += 1
                except sqlite3.IntegrityError:
                    # leads.UNIQUE(address, city, state, zip) tripped — another
                    # lead already resolved to this same property. That means
                    # two distress events hit one address (e.g. a lien AND a
                    # probate filing) — a *stronger* signal, not a conflict.
                    # Keep this lead's placeholder address (so the constraint
                    # is satisfied) but record the match for manual merging.
                    conn.execute(
                        "UPDATE leads SET notes = ?, updated_at = datetime('now') WHERE id = ?",
                        (
                            f"{prefix} DUPLICATE PROPERTY: another lead already resolved to "
                            f"acct {account['acct']} ({situs_address(account)}, "
                            f"{account['site_addr_2']} {account['site_addr_3']}) - same address, "
                            f"likely a second distress event on one property. Consider merging "
                            f"these leads (name_score={name_score:.2f}, legal_score={legal_score}, "
                            f"corroborating_names={corroboration}).",
                            lead["id"],
                        ),
                    )
                    stats["duplicate_property"] += 1
    finally:
        hcad_conn.close()

    return stats


if __name__ == "__main__":
    print("Downloading HCAD bulk account data (skips if already present)...")
    download_real_acct()
    print(f"Building local lookup DB at {LOOKUP_DB_PATH} ...")
    counts = build_lookup_db()
    print(f"Loaded {counts['hcad_accounts']} accounts, {counts['hcad_owners']} owner records.")
    print("Cross-referencing leads...")
    print(cross_reference_leads())
