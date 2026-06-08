"""Generate + send the closing packet once a buyer has actually said yes.

Usage:
    python scripts/close_deal.py <lead_id> \\
        --buyer-name "ABC Capital LLC" --buyer-email "deals@abccapital.com" \\
        --purchase-price 145000 --assignment-fee 12000 --close-date 2026-07-15
            # PREVIEW — generates both contracts into contracts/generated/ and
            # shows exactly what would be emailed and to whom. Sends nothing.

    python scripts/close_deal.py <lead_id> ... --send
            # actually emails the buyer their Assignment Contract, and both
            # contracts to you as the closing-packet record

WHY THIS IS A MANUAL COMMAND, NOT PART OF THE DAILY PIPELINE — step 7 of the
original ask wanted contracts auto-sent "when a buyer responds yes". Two
things make that impossible to automate honestly today: (1) this system is
outbound-only — there's no inbound email/SMS handling, so detecting a reply
would mean standing up a public, always-on webhook server (real new
infrastructure this project doesn't have); and (2) "yes" only means something
once Bryan has *real* purchase-price/assignment-fee/close-date numbers to
react to — exactly the deal-specific terms `DealTerms` requires as explicit
input rather than a database lookup (see src/contracts/generator.py's
docstring for why those can't be inferred). So the realistic trigger is Bryan
hearing "yes" himself — by phone, by reply, in person — with the real terms
already in hand. This command is what turns that moment into a generated and
sent closing packet in one step, reusing `generate_closing_packet` exactly
like `generate_contracts.py` does.

WHY ONLY THE ASSIGNMENT CONTRACT GOES TO THE BUYER — the Purchase Contract
names the seller and what Void Properties is paying THEM. A wholesaler
conventionally keeps that from the end buyer: a buyer who can see the
seller's identity and your acquisition price can go around you straight to
the seller and cut you out of your own assignment fee. (The Assignment
Contract already discloses the fee and the assignee's purchase price — that's
the buyer's business; the seller's identity and your terms with them aren't.)
Both contracts still get generated and written to contracts/generated/, and
BOTH get emailed to you as the closing-packet record — you decide what, if
anything, the seller ever sees of either document.

REAL-SEND RISK — this emails a signable legal document to a real third party,
the same order of risk `outreach_queue.py followups` guards against. It
previews by default; `--send` is required to actually fire.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.contracts.generator import COMPANY_NAME, ContractError, DealTerms, generate_closing_packet
from src.db.database import get_connection
from src.outreach import channels
from src.outreach.templates import SENDER_NAME, SENDER_PHONE

# Where the operator's copy of the closing packet goes — same inbox
# claude_score's hot-lead alerts use; this is a notification/record for
# Bryan, not outreach to a lead, so (like _notify_hot_lead) it bypasses
# tracker/log_outreach entirely.
OPERATOR_EMAIL = "bryantushifukato1213@gmail.com"

_RULE = "=" * 60


def _parse_close_date(raw: str):
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return raw  # accept free text too, e.g. "July 15, 2026"


def _lead_summary(lead_id: int) -> tuple[str, str]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT owner_name, address, city, state, zip FROM leads WHERE id = ?", (lead_id,)
        ).fetchone()
    if row is None:
        return "(unknown owner)", "(address unknown)"
    lead = dict(row)
    parts = [lead.get("address"), lead.get("city"), lead.get("state"), lead.get("zip")]
    address = ", ".join(p for p in parts if p) or "(address unknown)"
    return lead.get("owner_name") or "(unknown owner)", address


def _send(to: str, subject: str, body: str, label: str) -> None:
    try:
        result = channels.send_email(to, subject, body)
    except channels.ChannelError as exc:
        print(f"FAILED to send {label} -- {exc}")
        return
    if result.ok:
        print(f"Sent {label} to {to} (provider id {result.provider_id or '?'})")
    else:
        print(f"FAILED to send {label} to {to} -- {result.detail}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("lead_id", type=int)
    parser.add_argument("--buyer-name", required=True,
                        help="the end cash buyer this deal is being assigned to (the Assignee)")
    parser.add_argument("--buyer-email", required=True,
                        help="where to send the Assignment Contract for review/signature")
    parser.add_argument("--purchase-price", type=float, required=True,
                        help="what Void Properties is paying the seller")
    parser.add_argument("--assignment-fee", type=float, required=True,
                        help="the wholesale spread you're keeping on the assignment")
    parser.add_argument("--close-date", required=True,
                        help="close of escrow date -- e.g. 2026-07-15 or 'July 15, 2026'")
    parser.add_argument("--send", action="store_true",
                        help="actually email the closing packet (default: generate + preview only)")
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

    owner_name, address = _lead_summary(args.lead_id)

    print(f"Wrote {purchase_path}")
    print(f"Wrote {assignment_path}")
    print(f"Assignee's Purchase Price (purchase price + assignment fee, auto-computed): "
          f"${terms.assignee_purchase_price:,.2f}")
    print()

    if not args.send:
        print("PREVIEW -- nothing has been sent. Pass --send to actually email:")
        print(f"  -> Assignment Contract to {args.buyer_name} <{args.buyer_email}>")
        print(f"  -> both contracts (your closing-packet record) to {OPERATOR_EMAIL}")
        print()
        print("Note: the Purchase Contract is deliberately NOT sent to the buyer --")
        print("see this script's module docstring for why.")
        return

    assignment_text = assignment_path.read_text(encoding="utf-8")
    purchase_text = purchase_path.read_text(encoding="utf-8")

    buyer_subject = f"Assignment Contract for your review -- {COMPANY_NAME}"
    buyer_body = (
        f"Hi {args.buyer_name},\n\n"
        f"Following up on our conversation -- here's the Assignment Contract "
        f"for your review and signature. Reply here or call/text me directly "
        f"at {SENDER_PHONE} with any questions before you sign.\n\n"
        f"{_RULE}\n{assignment_text}\n{_RULE}\n\n"
        f"Talk soon,\n{SENDER_NAME}\n{SENDER_PHONE}"
    )
    _send(args.buyer_email, buyer_subject, buyer_body, "the Assignment Contract")

    operator_subject = f"Closing packet sent -- {owner_name} ({address})"
    operator_body = (
        f"Closing packet generated and the Assignment Contract emailed to the "
        f"buyer for lead #{args.lead_id}:\n\n"
        f"  Seller (lead):    {owner_name} -- {address}\n"
        f"  Buyer:            {args.buyer_name} <{args.buyer_email}>\n"
        f"  Purchase price:   ${terms.purchase_price:,.2f}\n"
        f"  Assignment fee:   ${terms.assignment_fee:,.2f}\n"
        f"  Assignee pays:    ${terms.assignee_purchase_price:,.2f}\n"
        f"  Close of escrow:  {terms.close_of_escrow_date}\n\n"
        f"Only the Assignment Contract went to the buyer (deliberately -- see "
        f"scripts/close_deal.py's docstring for why the Purchase Contract stays "
        f"between you and the seller). Both are below for your records; send "
        f"the Purchase Contract on to the seller yourself whenever that side's "
        f"ready, and update the lead's status once paperwork actually comes "
        f"back signed (this only sent it for review -- a send isn't a signature).\n\n"
        f"{_RULE}\nPURCHASE CONTRACT (Seller <-> Void Properties -- NOT sent to the buyer)\n{_RULE}\n"
        f"{purchase_text}\n\n"
        f"{_RULE}\nASSIGNMENT CONTRACT (sent to the buyer above)\n{_RULE}\n"
        f"{assignment_text}\n"
        f"\n-- sent automatically by scripts/close_deal.py (lead #{args.lead_id})"
    )
    _send(OPERATOR_EMAIL, operator_subject, operator_body, "your closing-packet copy")


if __name__ == "__main__":
    main()
