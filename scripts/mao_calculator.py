"""Maximum Allowable Offer (MAO) calculator for void-properties.

Usage:
    python scripts/mao_calculator.py --arv 220000 --repair-costs 35000 \\
        --square-footage 1450 --holding-period 6

Computes the ceiling on what you can offer a seller and still leave room for:

    - selling costs    8%      of ARV  (commission/costs to resell the finished flip)
    - investor profit  10%     of ARV  (the end buyer's required margin)
    - holding costs    4%/yr   of ARV, prorated for the holding period you give it
    - closing costs    $3,500  flat
    - repair costs     whatever you scope the property to actually need

    MAO = ARV - selling costs - investor profit - holding costs
              - closing costs - repair costs

Square footage doesn't change the MAO itself (none of the rates above are
per-square-foot) — it's used only to print $/sqft figures as a sanity check
on the result. See src/enrichment/mao.py for the full formula and the
reasoning behind each rate (particularly how the holding-cost rate and your
holding period combine).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.enrichment.mao import DEFAULT_HOLDING_PERIOD_MONTHS, calculate_mao, format_breakdown


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arv", type=float, required=True, help="after-repair value")
    parser.add_argument("--repair-costs", type=float, required=True,
                        help="what you've scoped this property to need in repairs")
    parser.add_argument("--square-footage", type=float, default=None,
                        help="optional — used only to print $/sqft sanity-check figures")
    parser.add_argument("--holding-period", type=float, default=DEFAULT_HOLDING_PERIOD_MONTHS,
                        help=f"months you expect to hold the property before resale (default: {DEFAULT_HOLDING_PERIOD_MONTHS:.0f})")
    args = parser.parse_args(argv)

    result = calculate_mao(
        arv=args.arv,
        repair_costs=args.repair_costs,
        square_footage=args.square_footage,
        holding_period_months=args.holding_period,
    )
    print(format_breakdown(result))


if __name__ == "__main__":
    main()
