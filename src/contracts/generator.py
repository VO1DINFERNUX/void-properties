"""Closing-stage contract generation for void-properties.

When a deal is ready to close, this fills in the two wholesaling contracts
this project runs on — `contracts/purchase_contract_template.txt` (Seller <->
Buyer, the original acquisition) and `contracts/assignment_contract_template.txt`
(Assignor <-> Assignee, the wholesale flip to an end cash buyer) — using the
`.format()`-placeholder convention `src/outreach/templates.py` already
established (`SENDER_NAME`/`SENDER_PHONE`, `render(...) -> filled text`).

WHY SOME FIELDS COME FROM THE DATABASE AND OTHERS DON'T — `seller_name` and
`property_address` are standing facts about a `leads` row (`owner_name`,
`address`/`city`/`state`/`zip`) that don't change deal to deal, so they're
read straight from the database. `purchase_price`, `buyer_name` (the end cash
buyer — the Assignee), `assignment_fee`, and `close_of_escrow_date` are
NEGOTIATED FOR THIS ONE CLOSING — there's nowhere in `leads`/`buyers` that
could hold "what we agreed to pay for 3703 Indian Mound Trail, closing June
30th" without conflating a standing fact about the property with a one-time
event tied to a specific deal. `DealTerms` takes them as explicit inputs
rather than guessing from `leads.estimated_value` or anything else — putting
a wrong number in a signable legal contract is a different order of mistake
than mis-scraping an address, so there is no inferred fallback here.

Bryan Moran / Void Properties — the wholesaler — is the constant "Buyer" in
the Purchase Contract and "Assignor" in the Assignment, reusing
`templates.SENDER_NAME`/`SENDER_PHONE` so that identity is defined in exactly
one place across outreach and contracts.

Several blanks the source contracts leave open for case-by-case handling
(earnest money, escrow agent details, APN, inspection period, signature
titles/addresses/emails, Assignee deposit specifics, etc.) are deliberately
left blank here too. Auto-filling those would mean inventing figures and
facts this module has no way to know — a worse failure mode in a document
meant to be signed than a visible blank a human fills in by hand. Same
"flag it, don't fake it" posture as every UNVERIFIED marker elsewhere in
this pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Union

from src.db.database import get_connection
from src.outreach.templates import SENDER_NAME, SENDER_PHONE

# The wholesaler's identity — "Buyer" in the Purchase Contract, "Assignor" in
# the Assignment. Reuses templates.SENDER_NAME/SENDER_PHONE (Bryan Moran /
# his Twilio number) rather than redefining it, so outreach and contracts
# can never drift apart on who "you" are in this system.
COMPANY_NAME = "Void Properties"

_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = _ROOT / "contracts"
OUTPUT_DIR = TEMPLATE_DIR / "generated"

PURCHASE_CONTRACT_TEMPLATE = TEMPLATE_DIR / "purchase_contract_template.txt"
ASSIGNMENT_CONTRACT_TEMPLATE = TEMPLATE_DIR / "assignment_contract_template.txt"


class ContractError(ValueError):
    """Raised when there's no lead to generate a contract for."""


@dataclass(frozen=True)
class DealTerms:
    """The terms negotiated for one specific closing.

    See the module docstring for why these can't be read from the database —
    in short, they describe a one-time event ("what we agreed for THIS deal"),
    not a standing fact about the property or its owner.
    """
    purchase_price: float          # what Void Properties is paying the seller
    buyer_name: str                # the end cash buyer this deal is being assigned to ("Assignee")
    assignment_fee: float          # the wholesale spread Void Properties keeps on the assignment
    close_of_escrow_date: Union[date, str]

    @property
    def assignee_purchase_price(self) -> float:
        """Assignee's Purchase Price = Assignor's purchase price + the
        assignment fee — exactly how the Assignment contract itself defines
        it (see its "PURCHASE PRICE AND ASSIGNMENT FEE" section); computed
        here so the two figures can never be entered inconsistently."""
        return self.purchase_price + self.assignment_fee


def _money(amount: float) -> str:
    return f"{amount:,.2f}"


def _coe_date(value: Union[date, str]) -> str:
    return value.strftime("%B %d, %Y") if isinstance(value, date) else str(value)


def _seller_name(lead: dict) -> str:
    return lead.get("owner_name") or "[SELLER NAME UNKNOWN — fill in manually]"


def _property_address(lead: dict) -> str:
    parts = [lead.get("address"), lead.get("city"), lead.get("state"), lead.get("zip")]
    joined = ", ".join(p for p in parts if p)
    return joined or "[PROPERTY ADDRESS UNKNOWN — fill in manually]"


def _lead(lead_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if row is None:
        raise ContractError(f"No lead #{lead_id} in the database")
    return dict(row)


def _fields(lead: dict, terms: DealTerms) -> dict[str, str]:
    return {
        "seller_name": _seller_name(lead),
        "property_address": _property_address(lead),
        "purchase_price": _money(terms.purchase_price),
        "buyer_name": terms.buyer_name,
        "assignment_fee": _money(terms.assignment_fee),
        "assignee_purchase_price": _money(terms.assignee_purchase_price),
        "close_of_escrow_date": _coe_date(terms.close_of_escrow_date),
        "company_name": COMPANY_NAME,
        "signer_name": SENDER_NAME,
        "signer_phone": SENDER_PHONE,
    }


def generate_purchase_contract(lead_id: int, terms: DealTerms) -> str:
    """Fill `purchase_contract_template.txt` for this lead/deal. Returns the
    completed contract text — Seller pulled from the lead's own record,
    Buyer is Void Properties / Bryan Moran."""
    lead = _lead(lead_id)
    text = PURCHASE_CONTRACT_TEMPLATE.read_text(encoding="utf-8")
    return text.format(**_fields(lead, terms))


def generate_assignment_contract(lead_id: int, terms: DealTerms) -> str:
    """Fill `assignment_contract_template.txt` for this lead/deal. Returns
    the completed contract text — Assignor is Void Properties / Bryan Moran,
    Assignee is the end cash buyer named in `terms.buyer_name`."""
    lead = _lead(lead_id)
    text = ASSIGNMENT_CONTRACT_TEMPLATE.read_text(encoding="utf-8")
    return text.format(**_fields(lead, terms))


def generate_closing_packet(
    lead_id: int, terms: DealTerms, out_dir: Path = OUTPUT_DIR
) -> tuple[Path, Path]:
    """Generate both contracts for this lead/deal and write them to
    `contracts/generated/`. Returns `(purchase_contract_path,
    assignment_contract_path)`.

    Filenames embed the lead id and today's date — re-running for the same
    lead after the terms change (a common occurrence before signing) writes
    a new dated draft alongside the old one rather than silently overwriting
    it.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()

    purchase_path = out_dir / f"lead{lead_id}_purchase_contract_{stamp}.txt"
    purchase_path.write_text(generate_purchase_contract(lead_id, terms), encoding="utf-8")

    assignment_path = out_dir / f"lead{lead_id}_assignment_contract_{stamp}.txt"
    assignment_path.write_text(generate_assignment_contract(lead_id, terms), encoding="utf-8")

    return purchase_path, assignment_path
