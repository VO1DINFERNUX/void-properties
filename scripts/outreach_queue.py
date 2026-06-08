"""Daily outreach worklist & logging for void-properties.

Usage:
    python scripts/outreach_queue.py                 # show today's worklist
    python scripts/outreach_queue.py --limit 10
    python scripts/outreach_queue.py log 42 call --outcome interested --status responded
    python scripts/outreach_queue.py contact 42 sms --message "Hi, this is..."
    python scripts/outreach_queue.py contact 42 email --subject "Cash offer" --message "..."
    python scripts/outreach_queue.py contact 42 sms --template sms
    python scripts/outreach_queue.py contact 42 email --template intro_email
    python scripts/outreach_queue.py followups               # preview who's due for a nudge
    python scripts/outreach_queue.py followups --send        # actually send them

`log` records an attempt *you* made yourself (a call, a knock on the door).
`contact` actually sends — SMS/voice via Twilio, email via SendGrid — and
logs the result either way; see config/.env.example for the credentials it
needs. Use `--template <name>` (see src/outreach/templates.py for the canned
intro_email/follow_up_email/sms/voicemail copy) to send pre-written, on-brand
outreach filled in automatically from the lead's own name and address — no
`--message` typing required, and no `--subject` either for the email templates,
which carry their own.

`followups` finds leads stuck on 'contacted' with exactly one outbound email
sent 3+ days ago and no reply, and sends each one `follow_up_email`. It only
*previews* that list by default — pass `--send` to actually fire the emails;
this is automated outreach to real people, so it doesn't go out silently.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.outreach import templates, tracker


def _show_queue(limit: int) -> None:
    summary = tracker.pipeline_summary()
    print("Pipeline:", ", ".join(f"{status}={count}" for status, count in sorted(summary.items())) or "(no leads yet)")
    print()

    leads = tracker.next_to_contact(limit=limit)
    if not leads:
        print("Nothing to contact right now — either everything active was")
        print("touched recently, or the remaining leads still need an address")
        print("(run `python -m src.enrichment.hcad` to resolve more).")
        return

    print(f"Next {len(leads)} to contact (use `log <id> <channel> ...` to record an attempt):")
    for lead in leads:
        print(" ", tracker.format_worklist_row(lead))


def _log(args: argparse.Namespace) -> None:
    event_id = tracker.log_outreach(
        lead_id=args.lead_id,
        channel=args.channel,
        message=args.message,
        response=args.response,
        outcome=args.outcome,
        direction=args.direction,
        new_status=args.status,
    )
    suffix = f", status -> {args.status}" if args.status else ""
    print(f"Logged outreach_events#{event_id} ({args.channel}) for lead #{args.lead_id}{suffix}")


def _contact(args: argparse.Namespace) -> None:
    if bool(args.message) == bool(args.template):
        print("error: pass exactly one of --message or --template", file=sys.stderr)
        raise SystemExit(2)
    if args.channel == "email" and args.message and not args.subject:
        print(
            "error: --subject is required for `contact ... email --message ...` "
            "(--template supplies its own subject)",
            file=sys.stderr,
        )
        raise SystemExit(2)

    try:
        event_id, result = tracker.contact_lead(
            lead_id=args.lead_id,
            channel=args.channel,
            message=args.message,
            template=args.template,
            subject=args.subject,
            new_status=args.status,
        )
    except (tracker.ChannelError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)

    if result.ok:
        print(
            f"Sent {args.channel} to lead #{args.lead_id} "
            f"(provider id {result.provider_id or '?'}, {result.detail}); "
            f"logged as outreach_events#{event_id}, status -> {args.status}"
        )
    else:
        print(f"Send FAILED for lead #{args.lead_id} ({args.channel}): {result.detail}")
        print(f"Logged as outreach_events#{event_id} (status left unchanged)")


def _followups(args: argparse.Namespace) -> None:
    due = tracker.due_for_followup(limit=args.limit)
    if not due:
        print(f"Nothing due for a follow-up — no 'contacted' lead has gone {tracker.FOLLOWUP_DELAY_DAYS}+ days without a reply after exactly one email.")
        return

    if not args.send:
        print(f"{len(due)} lead(s) due for a follow-up email (preview — pass --send to actually send):")
        for lead in due:
            print(f"  #{lead['id']} {lead.get('owner_name') or 'unknown owner'} "
                  f"-- {lead.get('owner_email')} -- {lead['days_since_email']:.1f}d since last email")
        return

    result = tracker.send_followups(limit=args.limit)
    print(f"Sent {result['sent']} follow-up email(s), {result['failed']} failed (see outreach_events for details).")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=20, help="how many leads to show in the worklist (default 20)")
    sub = parser.add_subparsers(dest="command")

    log_p = sub.add_parser("log", help="record an outreach attempt against a lead")
    log_p.add_argument("lead_id", type=int)
    log_p.add_argument("channel", choices=sorted(tracker.VALID_CHANNELS))
    log_p.add_argument("--message", help="what you sent/said")
    log_p.add_argument("--response", help="how the owner responded, if at all")
    log_p.add_argument("--outcome", help="e.g. no_answer, interested, not_interested, callback_requested")
    log_p.add_argument("--direction", choices=("outbound", "inbound"), default="outbound")
    log_p.add_argument("--status", choices=sorted(tracker.VALID_STATUSES), help="advance the lead's pipeline status")

    contact_p = sub.add_parser("contact", help="actually send an SMS/call/email (Twilio/SendGrid) and log the result")
    contact_p.add_argument("lead_id", type=int)
    contact_p.add_argument("channel", choices=("sms", "call", "email"))
    contact_p.add_argument("--message", help="message body you write yourself (read aloud for `call`)")
    contact_p.add_argument(
        "--template", choices=sorted(templates.TEMPLATES),
        help="send canned, on-brand copy filled in from the lead's own name/address instead of --message",
    )
    contact_p.add_argument(
        "--subject",
        help="email subject — required alongside --message for `email`; --template supplies its own",
    )
    contact_p.add_argument(
        "--status", choices=sorted(tracker.VALID_STATUSES), default="contacted",
        help="status to set on a successful send (default: contacted)",
    )

    followups_p = sub.add_parser(
        "followups",
        help=f"preview/send {tracker.FOLLOWUP_DELAY_DAYS}-day no-reply follow-up emails",
    )
    followups_p.add_argument(
        "--send", action="store_true",
        help="actually send the follow-up emails (default: preview only)",
    )

    args = parser.parse_args(argv)
    if args.command == "log":
        _log(args)
    elif args.command == "contact":
        _contact(args)
    elif args.command == "followups":
        _followups(args)
    else:
        _show_queue(args.limit)


if __name__ == "__main__":
    main()
