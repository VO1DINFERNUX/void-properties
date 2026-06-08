"""Maximum Allowable Offer (MAO) calculator for void-properties.

The MAO is the ceiling on what a wholesaler can offer a seller and still
leave enough on the table to cover repairs, the costs of reselling, and the
end buyer's required profit — the number every stage of this pipeline
(scrape -> enrich -> score -> outreach -> contracts) is ultimately working
toward. This module is the pure-calculation core; `scripts/mao_calculator.py`
is its CLI for a specific, fully-scoped deal, and `claude_score.py` calls
`quick_estimate()` to surface a rough range during routine scoring (see
"AUTO-ESTIMATE DURING SCORING" below for why that's a range and not a number).

THE FORMULA — start from the After-Repair Value and subtract everything that
has to come out of it before a wholesale deal pencils:

    MAO = ARV
          - (ARV x SELLING_COST_RATE)                                  # 8%  - commission/costs to resell the finished flip
          - (ARV x INVESTOR_PROFIT_RATE)                               # 10% - the end buyer's required margin
          - (ARV x HOLDING_COST_RATE x holding_period_months / 12)     # 4%/yr of ARV, prorated for the actual hold
          - CLOSING_COSTS                                              # $3,500 flat
          - repair_costs                                               # what the property actually needs — the one figure nobody can hand you

HOLDING_COST_RATE is treated as an *annual* rate of ARV (covering taxes,
insurance, utilities, financing) and prorated by the holding period in
months — the only reading of "4%" that both produces realistic dollar
figures (a flat 4%/month would imply a ~48%/yr carrying cost) and gives the
`holding_period` input something to do, which a flat-against-ARV reading
wouldn't.

Square footage doesn't appear in the formula above — none of the rates given
are per-square-foot — but `MAOResult` carries it through so `mao_per_sqft`/
`arv_per_sqft` can act as a sanity check on the number this returns (does
$/sqft for a finished flip in this neighborhood even make sense?), rather
than silently dropping an input the caller bothered to supply.

AUTO-ESTIMATE DURING SCORING — `claude_score.score_lead()` only ever has
`leads.estimated_value` to work with as an ARV proxy (see that module's own
caveat: it's usually NULL for county-records leads, and whatever's in it is
whatever enrichment or manual entry put there — this project deliberately
doesn't scrape Zillow; see the README's "Lead sources" section). It has no
idea what the place needs in repairs, how big it is, or how long a flip would
take — `leads` carries none of that. Asserting one made-up repair number as
if it were real would be exactly the mistake "flag it, don't fake it" exists
to prevent (same posture as `claude_score`'s qualitative equity inference, or
`hcad.py` refusing to guess an address). So `quick_estimate()` runs the same
formula across LIGHT/MODERATE/HEAVY rehab-scope assumptions
(`_REHAB_SCENARIOS`) at one fixed default hold (`DEFAULT_HOLDING_PERIOD_MONTHS`)
and returns all three — an honest range instead of a confident-looking guess.
Treat it as "is this even in the right neighborhood to pencil", not an offer
figure — run `scripts/mao_calculator.py` with real numbers once you've
actually scoped the property.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

SELLING_COST_RATE = 0.08
CLOSING_COSTS = 3_500.00
INVESTOR_PROFIT_RATE = 0.10
HOLDING_COST_RATE = 0.04  # annual rate of ARV — see module docstring for why

# Stand-ins for "nobody's actually scoped the repairs yet" (see "AUTO-ESTIMATE
# DURING SCORING" above) — rough rehab-scope bands as a fraction of ARV, wide
# enough apart to be informative about the deal's sensitivity to repair scope
# without pretending to know which one is true.
_REHAB_SCENARIOS = (
    ("light rehab (~10% of ARV)", 0.10),
    ("moderate rehab (~20% of ARV)", 0.20),
    ("heavy rehab (~30% of ARV)", 0.30),
)
DEFAULT_HOLDING_PERIOD_MONTHS = 6.0


@dataclass(frozen=True)
class MAOResult:
    arv: float
    repair_costs: float
    square_footage: Optional[float]
    holding_period_months: float
    selling_costs: float
    closing_costs: float
    investor_profit: float
    holding_costs: float
    mao: float

    @property
    def arv_per_sqft(self) -> Optional[float]:
        return (self.arv / self.square_footage) if self.square_footage else None

    @property
    def mao_per_sqft(self) -> Optional[float]:
        return (self.mao / self.square_footage) if self.square_footage else None


def calculate_mao(
    arv: float,
    repair_costs: float,
    square_footage: Optional[float] = None,
    holding_period_months: float = DEFAULT_HOLDING_PERIOD_MONTHS,
) -> MAOResult:
    """Maximum Allowable Offer for one fully-scoped deal — see the module
    docstring for the formula and the reasoning behind each rate.

    `square_footage` doesn't change `mao` (no sqft-based rate was specified
    for this calculator) — it's carried onto the result purely so
    `arv_per_sqft`/`mao_per_sqft` can sanity-check the number against
    comparable per-square-foot figures for the area.
    """
    selling_costs = arv * SELLING_COST_RATE
    investor_profit = arv * INVESTOR_PROFIT_RATE
    holding_costs = arv * HOLDING_COST_RATE * (holding_period_months / 12)

    mao = arv - selling_costs - investor_profit - holding_costs - CLOSING_COSTS - repair_costs

    return MAOResult(
        arv=arv,
        repair_costs=repair_costs,
        square_footage=square_footage,
        holding_period_months=holding_period_months,
        selling_costs=selling_costs,
        closing_costs=CLOSING_COSTS,
        investor_profit=investor_profit,
        holding_costs=holding_costs,
        mao=mao,
    )


def quick_estimate(arv: float) -> list[tuple[str, MAOResult]]:
    """A rough MAO range for a lead where the ARV is all that's known — see
    "AUTO-ESTIMATE DURING SCORING" above for why this is a labeled range
    rather than one asserted number. Returns `[(scenario_label, MAOResult), ...]`,
    light to heavy rehab."""
    return [
        (label, calculate_mao(arv, repair_costs=arv * pct, holding_period_months=DEFAULT_HOLDING_PERIOD_MONTHS))
        for label, pct in _REHAB_SCENARIOS
    ]


def format_breakdown(result: MAOResult) -> str:
    """Human-readable line-item breakdown for one fully-scoped MAOResult —
    shared by the CLI and anything else that wants the full math shown."""
    lines = [
        f"ARV:                       ${result.arv:>14,.2f}",
        f"- Selling costs   (8%):    ${result.selling_costs:>14,.2f}",
        f"- Investor profit (10%):   ${result.investor_profit:>14,.2f}",
        f"- Holding costs   (4%/yr x {result.holding_period_months:.1f} mo): ${result.holding_costs:>14,.2f}",
        f"- Closing costs:           ${result.closing_costs:>14,.2f}",
        f"- Repair costs:            ${result.repair_costs:>14,.2f}",
        f"= Maximum Allowable Offer: ${result.mao:>14,.2f}",
    ]
    if result.square_footage:
        lines.append(
            f"  ({result.square_footage:,.0f} sqft -> "
            f"${result.arv_per_sqft:,.2f}/sqft ARV, ${result.mao_per_sqft:,.2f}/sqft offer)"
        )
    return "\n".join(lines)
