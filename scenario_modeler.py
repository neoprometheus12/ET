"""
scenario_modeler.py
Disruption Scenario Modeller — simulates specific geopolitical/logistics
events (Hormuz closure, OPEC+ emergency cut, Red Sea shipping suspension)
and computes cascading downstream impacts: SPR draw-down timeline,
refinery run-rate cuts, domestic fuel price pass-through, power-sector
stress, and GDP/current-account trajectory.

METHODOLOGY NOTE — read before presenting this to judges/stakeholders:
Every coefficient below falls into one of two buckets, and the two are
NOT equally reliable:

  (A) SOURCED — pulled from published 2026 analyst/institutional
      estimates (S&P Global, SBI Research, HDFC Bank, CareEdge/ICRA,
      360 ONE Capital, India-briefing.com, BIMCO/Coface Red Sea traffic
      data). These are real published ranges, cited inline below.

  (B) MODELED/ILLUSTRATIVE — reasonable engineering assumptions used to
      connect the sourced numbers into a cascading pipeline (e.g. how
      fast refiners can substitute blocked crude, how SPR volume maps
      to a days-of-cover figure, how a global supply cut translates to
      a price shock). These are clearly flagged in comments and are
      tunable parameters, not verified figures. Treat them as a
      first-pass model structure, not a forecast.

The point of this module is to make the CASCADE — corridor blockage ->
price shock -> SPR draw-down -> refinery cuts -> fuel prices -> power
stress -> GDP/CAD drag — visible and adjustable, not to claim
econometric precision.
"""

from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# (A) SOURCED baseline constants — India oil supply picture, 2026
# ---------------------------------------------------------------------------
INDIA_TOTAL_CONSUMPTION_BPD = 5_500_000       # India-briefing.com, Mar 2026
INDIA_CRUDE_IMPORT_BPD = 4_700_000            # ~88% import dependency x consumption
INDIA_SPR_COVER_DAYS = 9.5                    # per challenge brief — Strategic Petroleum Reserve

# Share of India's crude IMPORTS (not total consumption) that transits each
# corridor. Hormuz is a real, sourced figure post-diversification. The other
# three are illustrative splits of the remaining import volume based on
# known sourcing patterns (Russia/Mediterranean crude via Suez/Red Sea,
# Far East/ESPO Russian Pacific crude via Malacca) — NOT independently
# sourced the way the Hormuz figure is, so treat as adjustable estimates.
CORRIDOR_IMPORT_SHARE = {
    "hormuz": 0.40,          # SOURCED — india-briefing.com, Mar 2026
    "bab_el_mandeb": 0.15,   # illustrative — Red Sea leg of Mediterranean/West Africa crude
    "suez": 0.10,            # illustrative — Suez leg, overlaps physically with Bab-el-Mandeb
    "malacca": 0.08,         # illustrative — Far East / Russian Pacific (ESPO) crude
}
# Remaining ~27% of imports arrive via routes not modeled here (Americas,
# West Africa via Cape of Good Hope, pipeline, domestic production).

INDIA_LNG_HORMUZ_SHARE = 0.50  # SOURCED — >50% of India's LNG imports transit Hormuz (Qatar-heavy)

# ---------------------------------------------------------------------------
# (A) SOURCED economic elasticities — Indian analyst consensus, 2026
# ---------------------------------------------------------------------------
# GDP growth impact per sustained $10/bbl crude increase.
# SBI Research / HDFC Bank: 20-25 bps. 360 ONE Capital: ~40 bps.
# S&P Global's stress scenario (crude to $130 vs $85 base, i.e. +$45)
# implies ~80bps, or ~18bps per $10 — consistent with the lower end.
# Using the midpoint of the cited range as the default.
GDP_IMPACT_PP_PER_10USD = 0.30

# Current account deficit widening per sustained $10/bbl crude increase.
# S&P Global: ~0.4pp of GDP. CareEdge/ICRA/SBI Research: 30-40bps.
CAD_IMPACT_PP_PER_10USD = 0.40

# Retail fuel price pass-through rate: the fraction of a crude price
# increase that actually reaches the pump, after OMC margin absorption
# and excise duty buffering (India's pump prices do NOT move 1:1 with
# crude, unlike fully deregulated markets).
# Source: 360 ONE Capital's ~5% partial pass-through assumption.
FUEL_PRICE_PASS_THROUGH_RATE = 0.05

BARRELS_TO_LITRES = 159  # standard conversion

# ---------------------------------------------------------------------------
# (B) MODELED assumptions — cascade mechanics, explicitly tunable
# ---------------------------------------------------------------------------
# Fraction of a blocked corridor's volume that India can realistically
# substitute within the scenario window (alternate suppliers, other
# corridors, drawing down commercial stocks beyond the SPR). India's
# diversification to ~40 supplier countries makes some substitution
# plausible, but full substitution is not immediate. Adjustable.
DEFAULT_SUBSTITUTION_CAPACITY = 0.30

# Elasticity used for global-supply-cut scenarios (OPEC+): how many
# USD/bbl a 1% cut in global daily supply moves the price, short-run.
# NOT independently sourced here — oil price short-run elasticity
# estimates vary widely in the literature. Treat as a dial, not a fact.
GLOBAL_SUPPLY_BPD = 101_000_000  # approx world oil supply, illustrative round number
OPEC_CUT_PRICE_ELASTICITY_USD_PER_PCT = 4.0

# GDP/CAD elasticities above are typically quoted for SUSTAINED annual
# shocks. A 2-week spike shouldn't move full-year GDP by the full amount.
# This dampens short scenarios toward zero and reaches full weight at
# GDP_DAMPENING_FULL_WEIGHT_DAYS.
GDP_DAMPENING_FULL_WEIGHT_DAYS = 180

# McKinsey finding cited in the challenge brief: economies without
# automated rerouting/demand-management took 47 days longer to
# stabilize supply after a shock than those with integrated response
# intelligence. Used to contrast "with this system" vs "reactive baseline."
REACTIVE_BASELINE_PENALTY_DAYS = 47


@dataclass
class ScenarioResult:
    scenario_id: str
    name: str
    category: str
    inputs: dict
    price_shock_usd_per_bbl: float
    volume_at_risk_bpd: float
    substitutable_bpd: float
    net_shortfall_bpd: float
    spr_reserve_barrels: float
    spr_days_to_exhaustion: Optional[float]
    refinery_run_rate_cut_pct: float
    retail_fuel_price_increase_paise_per_litre: float
    power_stress_index: float
    power_stress_notes: list
    gdp_growth_impact_pp: float
    cad_impact_pp: float
    with_intelligence_layer: bool
    effective_recovery_days: float
    timeline: list


# ---------------------------------------------------------------------------
# Scenario definitions — defaults are starting points, all overridable
# ---------------------------------------------------------------------------
SCENARIOS = {
    "hormuz_partial_closure": {
        "name": "Strait of Hormuz — Partial Closure",
        "category": "corridor_blockage",
        "corridors_affected": ["hormuz"],
        "default_capacity_reduction_pct": 50,
        "default_duration_days": 21,
        # Calibrated below the real Mar-2026 full-scale spike (~$45-48/bbl)
        # since this is a partial, not full, closure.
        "default_price_shock_usd_per_bbl": 24,
        "description": (
            "Iran or a regional actor partially blocks or threatens the Strait of "
            "Hormuz, cutting transit capacity without a full shutdown. Roughly 40% "
            "of India's crude imports and over half its LNG imports transit this "
            "corridor."
        ),
    },
    "hormuz_full_closure": {
        "name": "Strait of Hormuz — Full Closure",
        "category": "corridor_blockage",
        "corridors_affected": ["hormuz"],
        "default_capacity_reduction_pct": 100,
        "default_duration_days": 14,
        # Calibrated to the real March 2026 Indian crude basket spike
        # (~$62-70 baseline to $113.57/bbl) amid Hormuz disruption fears.
        "default_price_shock_usd_per_bbl": 46,
        "description": (
            "Full closure or effective blockade of the Strait of Hormuz. Roughly 20 "
            "million bpd of global oil (~20% of world consumption) transits this "
            "chokepoint; this is the highest-severity, highest-probability-of-real-"
            "world-precedent scenario in this set."
        ),
    },
    "red_sea_suspension": {
        "name": "Red Sea / Bab-el-Mandeb Shipping Suspension",
        "category": "corridor_blockage",
        "corridors_affected": ["bab_el_mandeb", "suez"],
        # Tanker/bulk carrier transit decline during the 2024-25 Houthi
        # campaign was roughly half the container-shipping decline
        # (~90% container drop vs ~45% for tankers/bulkers) — using the
        # oil-relevant figure, not the more commonly cited container stat.
        "default_capacity_reduction_pct": 45,
        "default_duration_days": 60,
        # Smaller price shock than Hormuz: crude still reaches market via
        # the Cape of Good Hope, just later and at higher freight/insurance
        # cost — this is a cost/delay shock more than a scarcity shock.
        "default_price_shock_usd_per_bbl": 6,
        "description": (
            "Houthi attacks (or similar) force sustained rerouting of Red Sea / "
            "Suez shipping around the Cape of Good Hope, adding ~10-14 days transit "
            "time and raising freight/insurance costs rather than removing crude "
            "from the market outright."
        ),
    },
    "opec_emergency_cut": {
        "name": "OPEC+ Emergency Production Cut",
        "category": "global_price_shock",
        "corridors_affected": [],
        "default_cut_bpd": 1_500_000,
        "default_duration_days": 90,
        "description": (
            "OPEC+ announces a coordinated emergency production cut in response to "
            "a price collapse or political decision, tightening global supply. "
            "Unlike a corridor blockage, crude can still reach India physically — "
            "the impact is a global price shock, not a shipping disruption."
        ),
    },
}


def _timeline(net_shortfall_bpd, spr_reserve_barrels, duration_days, price_shock_usd_per_bbl):
    """Day-by-day (weekly resolution) SPR level and cumulative price exposure."""
    points = []
    spr_remaining = spr_reserve_barrels
    step = 7
    day = 0
    while day <= duration_days:
        if net_shortfall_bpd > 0:
            spr_remaining = max(0.0, spr_remaining - net_shortfall_bpd * step)
        points.append({
            "day": day,
            "spr_barrels_remaining": round(spr_remaining, 0),
            "spr_pct_remaining": round(100 * spr_remaining / spr_reserve_barrels, 1) if spr_reserve_barrels else 0,
            "cumulative_price_shock_usd": price_shock_usd_per_bbl,  # flat shock assumption over duration
        })
        day += step
    return points


def simulate_scenario(scenario_id: str, overrides: Optional[dict] = None,
                       with_intelligence_layer: bool = True) -> ScenarioResult:
    if scenario_id not in SCENARIOS:
        raise ValueError(f"Unknown scenario_id: {scenario_id}. Options: {list(SCENARIOS)}")

    base = SCENARIOS[scenario_id]
    params = dict(base)
    if overrides:
        params.update(overrides)

    category = base["category"]
    duration_days = params.get("default_duration_days", 30)
    substitution_capacity = params.get("substitution_capacity", DEFAULT_SUBSTITUTION_CAPACITY)

    # -----------------------------------------------------------------
    # Step 1: volume at risk + price shock
    # -----------------------------------------------------------------
    if category == "corridor_blockage":
        capacity_reduction_pct = params.get("capacity_reduction_pct",
                                             params["default_capacity_reduction_pct"])
        volume_at_risk_bpd = sum(
            CORRIDOR_IMPORT_SHARE.get(c, 0) * INDIA_CRUDE_IMPORT_BPD * (capacity_reduction_pct / 100)
            for c in base["corridors_affected"]
        )
        # Scale the calibrated default price shock by how severe this run is
        # relative to the scenario's own default severity.
        severity_ratio = capacity_reduction_pct / base["default_capacity_reduction_pct"]
        price_shock = params.get("price_shock_usd_per_bbl",
                                  base["default_price_shock_usd_per_bbl"] * severity_ratio)

    elif category == "global_price_shock":
        cut_bpd = params.get("cut_bpd", params["default_cut_bpd"])
        cut_pct_of_global = (cut_bpd / GLOBAL_SUPPLY_BPD) * 100
        price_shock = cut_pct_of_global * OPEC_CUT_PRICE_ELASTICITY_USD_PER_PCT
        # Global cuts don't block a shipping corridor — India still receives
        # crude, just at the higher world price. No corridor-specific volume risk.
        volume_at_risk_bpd = 0.0

    else:
        raise ValueError(f"Unknown scenario category: {category}")

    # -----------------------------------------------------------------
    # Step 2: substitution + SPR draw-down
    # -----------------------------------------------------------------
    substitutable_bpd = volume_at_risk_bpd * substitution_capacity
    net_shortfall_bpd = max(0.0, volume_at_risk_bpd - substitutable_bpd)

    spr_reserve_barrels = INDIA_SPR_COVER_DAYS * INDIA_TOTAL_CONSUMPTION_BPD
    spr_days_to_exhaustion = (
        round(spr_reserve_barrels / net_shortfall_bpd, 1) if net_shortfall_bpd > 0 else None
    )

    # -----------------------------------------------------------------
    # Step 3: refinery run-rate impact
    # Phase 1 (SPR still covering the gap): light precautionary cut only.
    # Phase 2 (SPR exhausted before scenario ends): cut scales with the
    # unmet shortfall as a share of total crude imports.
    # -----------------------------------------------------------------
    if net_shortfall_bpd <= 0:
        refinery_run_rate_cut_pct = 0.0
    elif spr_days_to_exhaustion is not None and spr_days_to_exhaustion >= duration_days:
        refinery_run_rate_cut_pct = 3.0  # precautionary/quality-mix friction, SPR covers volume
    else:
        refinery_run_rate_cut_pct = min(100.0, (net_shortfall_bpd / INDIA_CRUDE_IMPORT_BPD) * 100)

    # -----------------------------------------------------------------
    # Step 4: retail fuel price pass-through
    # -----------------------------------------------------------------
    retail_increase_usd_per_bbl = price_shock * FUEL_PRICE_PASS_THROUGH_RATE
    retail_increase_usd_per_litre = retail_increase_usd_per_bbl / BARRELS_TO_LITRES
    # Converted to paise/litre using a simplified fixed USD-INR reference
    # rate for illustration — update INR_PER_USD if presenting live.
    INR_PER_USD = 86
    retail_increase_paise_per_litre = round(retail_increase_usd_per_litre * INR_PER_USD * 100, 1)

    # -----------------------------------------------------------------
    # Step 5: power sector stress
    # Coal dominates India's grid, so direct linkage is modest — but
    # Hormuz scenarios carry an LNG-linked bonus since >50% of India's
    # LNG imports also transit Hormuz (mostly Qatari supply).
    # -----------------------------------------------------------------
    power_stress_notes = []
    power_stress_index = min(100.0, price_shock * 1.2)
    power_stress_notes.append(
        f"Diesel/fuel-oil backup generation cost pressure from a ${price_shock:.0f}/bbl crude shock."
    )
    if "hormuz" in base.get("corridors_affected", []):
        lng_bonus = 20.0 * (volume_at_risk_bpd / (CORRIDOR_IMPORT_SHARE["hormuz"] * INDIA_CRUDE_IMPORT_BPD)) \
            if volume_at_risk_bpd else 0.0
        power_stress_index = min(100.0, power_stress_index + lng_bonus)
        power_stress_notes.append(
            "Over 50% of India's LNG imports also transit Hormuz (Qatar-heavy) — "
            "gas-fired peaking plants face a compounding fuel cost/availability shock."
        )

    # -----------------------------------------------------------------
    # Step 6: GDP / CAD trajectory, dampened for short-duration shocks
    # -----------------------------------------------------------------
    duration_dampener = min(1.0, duration_days / GDP_DAMPENING_FULL_WEIGHT_DAYS)
    gdp_growth_impact_pp = -round((price_shock / 10) * GDP_IMPACT_PP_PER_10USD * duration_dampener, 3)
    cad_impact_pp = round((price_shock / 10) * CAD_IMPACT_PP_PER_10USD * duration_dampener, 3)

    # -----------------------------------------------------------------
    # Step 7: "with vs without automated response intelligence"
    # Per the McKinsey finding in the brief: reactive (no automated
    # rerouting/demand-management) response takes 47 days longer to
    # stabilize supply than an integrated, anticipatory response.
    # -----------------------------------------------------------------
    effective_recovery_days = duration_days if with_intelligence_layer else duration_days + REACTIVE_BASELINE_PENALTY_DAYS

    timeline = _timeline(net_shortfall_bpd, spr_reserve_barrels, duration_days, price_shock)

    return ScenarioResult(
        scenario_id=scenario_id,
        name=base["name"],
        category=category,
        inputs={
            "duration_days": duration_days,
            "substitution_capacity": substitution_capacity,
            **{k: v for k, v in params.items() if k not in ("name", "category", "description", "corridors_affected")},
        },
        price_shock_usd_per_bbl=round(price_shock, 2),
        volume_at_risk_bpd=round(volume_at_risk_bpd, 0),
        substitutable_bpd=round(substitutable_bpd, 0),
        net_shortfall_bpd=round(net_shortfall_bpd, 0),
        spr_reserve_barrels=round(spr_reserve_barrels, 0),
        spr_days_to_exhaustion=spr_days_to_exhaustion,
        refinery_run_rate_cut_pct=round(refinery_run_rate_cut_pct, 1),
        retail_fuel_price_increase_paise_per_litre=retail_increase_paise_per_litre,
        power_stress_index=round(power_stress_index, 1),
        power_stress_notes=power_stress_notes,
        gdp_growth_impact_pp=gdp_growth_impact_pp,
        cad_impact_pp=cad_impact_pp,
        with_intelligence_layer=with_intelligence_layer,
        effective_recovery_days=effective_recovery_days,
        timeline=timeline,
    )


def list_scenarios() -> list:
    """Metadata for every predefined scenario, for a frontend picker."""
    return [
        {
            "id": sid,
            "name": s["name"],
            "category": s["category"],
            "description": s["description"],
            "defaults": {k: v for k, v in s.items() if k.startswith("default_")},
            "corridors_affected": s.get("corridors_affected", []),
        }
        for sid, s in SCENARIOS.items()
    ]
