"""Generate closing contracts for a lead — Purchase Contract + Assignment.

Usage:
    python scripts/generate_contracts.py <lead_id> \\
        --purchase-price 145000 --buyer-name "ABC Capital LLC" \\
        --assignment-fee 12000 --close-date 2026-07-15

Fills `contracts/purchase_contract_template.txt` (Seller <-> Void Properties)
and `contracts/assignment_contract_template.txt` (Void Properties <-> the end
cash buyer) using the lead's own seller name / property address from the
database, plus the deal terms you pass on the command line — see
src/contracts/generator.py's module docstring for why purchase price, buyer
name, assignment fee, and close-of-escrow date have to be supplied here
rather than read from `leads`/`buyers` (they describe what THIS deal closes
at, not a standing fact about the property). Bryan Moran / Void Properties
are filled in automatically as Buyer/Assignor.

Writes both completed contracts to contracts/generated/. A handful of blanks
in the source templates (earnest money, escrow agent, APN, signature
titles/addresses, etc.) are deliberately left for you to fill in by hand —
see the generator's docstring for why.
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.contracts.generator import ContractError, DealTerms, generate_closing_packet


def _parse_close_date(raw: str):
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return raw  # accept free text too, e.g. "July 15, 2026"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("lead_id", type=int)
    parser.add_argument("--purchase-price", type=float, required=True,
                        help="what Void Properties is paying the seller")
    parser.add_argument("--buyer-name", required=True,
                        help="the end cash buyer this deal is being assigned to (the Assignee)")
    parser.add_argument("--assignment-fee", type=float, required=True,
                        help="the wholesale spread you're keeping on the assignment")
    parser.add_argument("--close-date", required=True,
                        help="close of escrow date — e.g. 2026-07-15 or 'July 15, 2026'")
    args = parser.parse_args(argv)

    terms = DealTerms(
        purchase_price=args.purchase_price,
        buyer_name=args.buyer_name,
        assignment_fee=args.assignment_fee,
        close_of_escrow_date=_parse_close_date(args.close_date),
    )

    try:
        purchase_path, assignment_path = generate_closing_packet(args.lead_id, terms)
    except ContractError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)

    print(f"Wrote {purchase_path}")
    print(f"Wrote {assignment_path}")
    print()
    print(f"Assignee's Purchase Price (purchase price + assignment fee, auto-computed): "
          f"${terms.assignee_purchase_price:,.2f}")
    print("Note: escrow agent details, earnest money, APN, inspection period, and")
    print("signature titles/addresses/emails are deliberately left blank for you")
    print("to fill in by hand — see src/contracts/generator.py for why.")


if __name__ == "__main__":
    main()
