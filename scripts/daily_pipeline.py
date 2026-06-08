"""The daily automated pipeline -- scrape, resolve, enrich, score, alert.

Runs the full daily cycle end to end. Wired into Windows Task Scheduler to
fire every morning at 7 AM (see scripts/setup_daily_task.ps1 and the
README's "Daily pipeline" section for how that's registered and how to
check/change/remove it).

What one run does, in order:
  1. Scrape fresh Harris County seller leads (harris_county_deeds) and
     likely-cash-buyer leads (harris_county_buyers)
  2. Resolve seller addresses against HCAD (hcad.cross_reference_leads) --
     a prerequisite both for Apollo (enrich_leads only spends credits on
     leads with a real zip) and for next_to_contact() to ever surface them
  3. Apollo-enrich sellers (owner_phone/owner_email) and buyers
     (buyer_phone/buyer_email -- see apollo.enrich_buyers)
  4. Claude-score every freshly-scraped seller lead -- which also computes
     an MAO range wherever estimated_value is on file, and fires a hot-lead
     alert (now including a cash-buyer shortlist -- see buyer_match.py) to
     bryantushifukato1213@gmail.com for anything scoring 7+ (see
     claude_score.ALERT_SCORE_THRESHOLD / _notify_hot_lead)

WHAT THIS DELIBERATELY DOES NOT DO -- it does not email buyers, and it does
not generate or send contracts, even though the original ask wanted both
wired into the daily run. Both need information that simply doesn't exist at
7 AM scoring time: a real negotiated purchase price and assignment fee (see
src/contracts/generator.py's DealTerms docstring for why those can't be
inferred from `leads`/`buyers`), and confirmation that a specific buyer has
actually said yes (this system is outbound-only -- there's no inbound
email/SMS handling to detect a reply with; see scripts/close_deal.py's
docstring for the infrastructure gap that would take to build). Sending a
real dollar figure -- or a signable legal contract -- to a real third party
on a guess is a different order of mistake than a wrong guess anywhere else
in this pipeline. So those stay manual, human-triggered steps
(scripts/close_deal.py) for once Bryan has real numbers and a real "yes" in
hand; this run's job is to put everything he needs to get there -- the lead,
the MAO range, and his liveliest cash-buyer candidates -- in front of him
the moment a lead clears the bar.

Each stage is wrapped so a failure in one doesn't take the rest of an
unattended run down with it -- a broken scrape shouldn't also cost you that
day's scoring of whatever's already in the database. Failures print loudly
(the same "flag it, don't fake it" honesty as every other module here)
rather than disappearing silently into a cron log nobody reads.

Usage:
    python scripts/daily_pipeline.py
    python scripts/daily_pipeline.py --skip-scrape   # re-run resolve/enrich/score only
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.db.database import init_db
from src.enrichment import apollo, claude_score, hcad
from src.scraper import scraper
from src.scraper.sources.harris_county_buyers import HarrisCountyBuyerSource, run_buyers
from src.scraper.sources.harris_county_deeds import HarrisCountyDeedSource


def _stage(label: str, fn: Callable, *args, **kwargs) -> None:
    """Run one pipeline stage, isolated -- an exception here is logged and
    swallowed so the rest of the run still happens (see module docstring)."""
    print(f"\n=== {label} ===", flush=True)
    try:
        result = fn(*args, **kwargs)
        if result is not None:
            print(f"[{label}] {result}")
    except Exception as exc:
        print(f"[{label}] FAILED -- {exc}")


def _ensure_hcad_lookup() -> None:
    """Build the local HCAD lookup DB if it isn't there yet.

    `download_real_acct` already skips its ~210MB download once the zip is
    present (see its docstring) -- but `build_lookup_db` always rebuilds
    from scratch, which is too heavy to redo every single morning. So this
    bootstraps the lookup DB once, the first time a daily run finds it
    missing, and every run after that just cross-references against whatever
    snapshot exists. HCAD republishes its bulk export periodically -- run
    `python -m src.enrichment.hcad` by hand whenever you want a fresh one.
    """
    if hcad.LOOKUP_DB_PATH.exists():
        return
    print("[hcad] no local lookup DB yet -- building one (one-time; downloads ~210MB if needed)...")
    hcad.download_real_acct()
    counts = hcad.build_lookup_db()
    print(f"[hcad] loaded {counts['hcad_accounts']} accounts, {counts['hcad_owners']} owner records")


def run(skip_scrape: bool = False) -> None:
    started = datetime.now()
    print(f"void-properties daily pipeline -- {started:%Y-%m-%d %H:%M:%S}")

    init_db()

    if not skip_scrape:
        _stage("1a. scrape sellers (harris_county_deeds)", scraper.run, [HarrisCountyDeedSource()])
        _stage("1b. scrape buyers (harris_county_buyers)", run_buyers, [HarrisCountyBuyerSource()])
    else:
        print("\n(--skip-scrape: re-running resolve/enrich/score against what's already in the database)")

    _stage("2a. build/check HCAD lookup", _ensure_hcad_lookup)
    _stage("2b. resolve seller addresses against HCAD", hcad.cross_reference_leads)

    _stage("3a. Apollo-enrich sellers (owner_phone/owner_email)", apollo.enrich_leads)
    _stage("3b. Apollo-enrich buyers (buyer_phone/buyer_email)", apollo.enrich_buyers)

    _stage("4. Claude-score sellers (+ MAO, + hot-lead alerts w/ buyer shortlist)", claude_score.score_leads)

    finished = datetime.now()
    print(f"\nDone in {(finished - started).total_seconds():.0f}s -- {finished:%Y-%m-%d %H:%M:%S}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--skip-scrape", action="store_true",
        help="skip the scrape stages -- just re-run resolve/enrich/score on what's already in the database",
    )
    args = parser.parse_args(argv)
    run(skip_scrape=args.skip_scrape)


if __name__ == "__main__":
    main()
