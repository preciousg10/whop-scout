"""Ranking scores. Two live side by side, on purpose:

`pre_score` — the original CRUDE sort hint, kept unchanged and labeled as such:
    pay_per_1k * budget_remaining_fraction / max(participants, 1)
It ignores everything that actually matters and is only "look at these first".

`composite_score` — the richer rank the report sorts by. It multiplies the levers
that make a campaign worth a clip and then applies honest penalties. VIEWS DOMINATE
RATE: a $0.50/1k campaign for a hugely popular, highly clippable creator beats a $3/1k
one for a small creator whose clips get 800 views. So pay rate is only a modest nudge,
while reach + expected-earnings-per-clip drive the rank:

    base       = budget_remaining * clippability * reach_factor * pay_rate_factor
    composite  = base * confidence_factor * minimum_penalty * below_min_penalty
                      * earnings_factor                        (headline: reach × rate)
                      * repeatable_factor                      (THE clip-quality lever)
                      * max_payout_factor * velocity_factor * competition_factor
                      * data_confidence_factor                 (discount UNKNOWN-heavy ranks)
    (a disqualified campaign is forced to composite 0 — it sinks, but is never hidden.)

  - data_confidence_factor (1.0 down to 0.35): because every UNKNOWN input maps to a
    neutral 1.0, a campaign with NO real data can float up on nothing but neutrals. This
    discounts by how many of 5 CORE signals (repeatable clippability, expected earnings/
    clip, creator reach, footage accessibility, content type) were actually measured —
    a proven campaign should outrank a mystery one. Ties break toward more-known campaigns.

  - pay_rate_factor (~0.75x..1.4x): pay rate as a MODEST multiplier — $0.50/1k is fine
    when reach + clippability are strong. Deliberately narrow so it can't dominate.
  - reach_factor (~0.6x..2.8x): a PRIMARY view-driver, deliberately WIDE so it dominates
    the pay rate. reach is RECENT TRACTION (avg recent views) when we have it, else
    follower/subscriber reach, else a neutral 1.0 baseline (NOT a fabricated follower
    number — the confidence penalty covers it).
  - earnings_factor (~0.3x..2.5x): expected earnings per clip = proven clipper MEDIAN
    views × pay_per_1k / 1000 — the number that actually matters (what a typical clip
    earns me). Heavy lever when clipper data exists; UNKNOWN -> neutral 1.0.
  - clippability: crude 0..1 proxy — short-form platforms + provided source footage.
  - confidence_factor: HIGH 1.0 / LOW 0.7 / UNKNOWN 0.5 — we trust reach less when
    we couldn't verify it.
  - minimum_penalty: 0.3 when a single ~1k-view clip earns nothing (HIGH_MINIMUM),
    else 1.0. This is what "deprioritize high-minimum hard" means numerically.
  - below_min_penalty: 0.15 (STRICT) when the PROVEN expected views for a typical clip
    fall below the views needed to reach the minimum payout — a typical clip earns $0.
    Only fires when both numbers are known; UNKNOWN never penalizes.

THE clip-quality lever (widest dynamic range among the clip signals):
  - repeatable_factor (0.5x..2.5x): `proven_clips` measures how REPEATABLY clippable this
    creator is via dedicated clipper accounts — pooled MEDIAN views, consistency, and
    spread ACROSS clipper accounts. This is the signal that survives a fat-tailed clip
    distribution; it replaces views-per-submission (dropped — an average over 2,000 clips
    reads "healthy" off two viral hits while 1,998 clippers earned nothing, so it can't
    tell "clips reliably land" from "two got lucky"). Unknown -> 1.0.

Supporting levers:
  - max_payout_factor: >=$300/video or uncapped -> ~1.0+; a low per-video cap
    proportionally penalizes (it caps the viral upside the model depends on).
  - velocity_factor: payout velocity (paid/total/day). An OLD campaign creeping along
    is a strong negative; a FRESH one is neutral (never penalized for being new).
  - competition_factor: participants per $1k of budget — 3,000 clippers on $30k is a
    very different fight from 20, and gets penalized.

Unknown inputs always map to a NEUTRAL 1.0 and are surfaced as UNKNOWN — never guessed.

`composite_score(c)` returns (score, breakdown) so the report can show WHY a
campaign ranks where it does rather than a black-box number.
"""
import math
import re
from datetime import datetime

CONFIDENCE_FACTORS = {"HIGH": 1.0, "LOW": 0.7, "UNKNOWN": 0.5}
HIGH_MINIMUM_PENALTY = 0.3
# Strict minimum-payout gate: when the PROVEN expected views for a typical clip fall below
# the views needed to reach a campaign's minimum payout, a typical clip earns $0 — penalize
# hard. Only fires when both numbers are known (never guessed).
EXPECTED_BELOW_MIN_PENALTY = 0.15
# Minimum-VIEW payout gate (DISTINCT from the dollar minimum above): views a single video
# must reach before ANY payout ("VIDEO MUST REACH 10K FOR PAYOUT"). Brutal for a zero-audience
# start — a typical early clip never clears the gate and earns $0 — so the penalty scales hard
# with the threshold: ~0.6x at 1K, ~0.12x (SEVERE) at 10K, ~0.04x at 50K+. None -> neutral 1.0.
MIN_VIEW_THRESHOLD_POINTS = [
    (100, 0.95), (500, 0.85), (1000, 0.60), (2500, 0.40),
    (5000, 0.25), (10000, 0.12), (25000, 0.06), (50000, 0.04),
]
# Repeatable-clippability multiplier range: score 0 -> 0.5x, 0.5 -> 1.5x, 1 -> 2.5x.
# A 5x dynamic range makes this the heaviest single lever, as intended.
REPEATABLE_MIN_FACTOR = 0.5
REPEATABLE_SPAN = 2.0

# Payout velocity: campaigns younger than this many days are NEVER penalized for a low
# velocity — a fresh campaign hasn't had time to pay out. (Handled in velocity_factor.)
VELOCITY_MIN_AGE_DAYS = 5.0
# Max payout per video floor — at/above this a cap doesn't hurt the model.
MAX_PAYOUT_FLOOR = 300.0

# --- style fit (chaos-clip channel affinity) -----------------------------------
# How much a campaign's STYLE fits a chaotic/high-energy clip channel (stream highlights,
# reactions, gaming/action, memes) vs. polished produced content (jewelry, corporate, music
# videos). Built ONLY from signals already captured — category (extract.classify_category) +
# footage content-type & action-density (intake.py) — so it adds NO scraping. It's a
# MULTIPLIER on the composite ALONGSIDE the money signals, never replacing them.
#
# STYLE_FIT_WEIGHT is the single tuning knob (the swing): factor = 1 + WEIGHT*(2*fit - 1), so a
# perfect chaos fit (fit=1) -> (1+WEIGHT)x, polished (fit=0) -> (1-WEIGHT)x, neutral/unknown
# (fit=0.5) -> exactly 1.0x. Set 0.0 to DISABLE the signal entirely; raise toward 1.0 to make
# style dominate. Default 0.6 => a 0.4x .. 1.6x swing.
STYLE_FIT_WEIGHT = 0.6
STYLE_FIT_FLOOR = 0.1   # never let style alone zero a campaign (money signals still decide)

# Per-category chaos affinity in [0,1] (1 = peak chaos-clippable, 0 = polished/produced).
# EDIT THIS to re-profile the channel (e.g. a talking-head channel would raise podcast_talking).
STYLE_FIT_CATEGORY = {
    "streamer_irl":    1.0,   # stream highlights / IRL / subathons — peak chaos
    "gaming":          0.9,   # action gameplay
    "meme":            0.9,   # funny / viral / shitpost
    "sports":          0.85,  # action moments
    "podcast_talking": 0.5,   # talking-head — clippable but not chaotic (neutral)
    "news":            0.45,  # commentary / current-events talk
    "movie_tv":        0.5,   # films / trailers / cinematic — variable
    "music":           0.25,  # polished / produced
    "brand_product":   0.15,  # jewelry, corporate, product promos — produced
    "other":           0.5,   # unknown category — neutral, never a guessed penalty
}
# Additive nudges from the footage CONTENT TYPE (intake.py), applied only when known.
STYLE_FIT_CONTENT = {
    "standard_stream_vod":     +0.10,  # raw stream footage — chaos-friendly
    "podcast_interview":        0.0,   # talking — neutral
    "slideshow_photo":         -0.20,  # produced / static
    "music_video":             -0.30,  # highly produced
    "short_form_only_unusual": -0.15,
    "ugc_requires_my_face":    -0.15,
    "other":                   -0.10,
}
# Additive nudges from the ACTION-DENSITY band (intake.py), applied only when scored.
STYLE_FIT_DENSITY = {"eventful": +0.15, "mixed": 0.0, "logistics_heavy": -0.25}


def _interp_log(x, points):
    """Piecewise-linear-in-log interpolation. `points` = ascending [(x_i, y_i), ...].
    Clamps to the end y-values outside the range. x<=0 -> first y."""
    if x is None or x <= 0:
        return points[0][1]
    lx = math.log10(x)
    if lx <= math.log10(points[0][0]):
        return points[0][1]
    if lx >= math.log10(points[-1][0]):
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if points[0][0] <= x <= x1 or (x0 <= x <= x1):
            if x0 <= x <= x1:
                lx0, lx1 = math.log10(x0), math.log10(x1)
                t = (lx - lx0) / (lx1 - lx0) if lx1 > lx0 else 0.0
                return y0 + t * (y1 - y0)
    return points[-1][1]


# =============================================================================
# VIEW-DRIVING PRIMARY LEVERS. A low pay rate on a hugely popular, highly clippable
# creator beats a high rate on a small one — so pay rate is a MODEST nudge, while creator
# reach and (when clipper data exists) expected earnings-per-clip dominate the rank.
# =============================================================================
def pay_rate_factor(pay_per_1k):
    """Pay rate as a GENTLE multiplier, NOT a primary driver. Modest range (~0.75x..1.4x):
    $0.50/1k -> ~0.85, $1 -> 1.0, $2 -> ~1.1, $5 -> ~1.3, $10+ -> ~1.4. Even $0.50/1k is
    perfectly acceptable when reach + clippability are strong. Unknown -> neutral 1.0."""
    if not pay_per_1k:
        return 1.0
    return round(_interp_log(pay_per_1k,
                             [(0.25, 0.75), (0.5, 0.85), (1.0, 1.0), (2.0, 1.1),
                              (5.0, 1.3), (10.0, 1.4)]), 4)


# --- budget as ABSOLUTE DOLLARS remaining (not a bare percentage) ---------------
# A budget signal read as a bare FRACTION is meaningless without the total: "90% left"
# on a $10,000 campaign is $9,000 (a real pool); "90% left" on a $100 campaign is $90
# (worthless) — yet the old base multiplied by the fraction alone, so they scored the
# same. So the budget lever is now driven by ACTUAL DOLLARS REMAINING = total × fraction.
# Log-scaled: a near-empty pool floors low (~0.15), an ordinary pool is neutral (~$2k →
# 1.0), a huge pool boosts (up to ~2.6). Near-empty still sinks because tiny fraction →
# tiny dollars regardless of total.
BUDGET_DOLLAR_POINTS = [
    (50, 0.15), (150, 0.30), (400, 0.50), (900, 0.70), (2000, 1.00),
    (5000, 1.35), (15000, 1.80), (40000, 2.20), (100000, 2.60),
]
# The big-budget BOOST (the part of budget_factor above the neutral 1.0) is only "real
# earning opportunity" if the pay rate is decent — a $100k pool at a garbage $0.10/1k CPM
# is not a $100k opportunity. So the boost above neutral is scaled by this rate-quality
# fraction (0..1). Pay rate ALSO keeps its own separate `pay_rate_factor` lever; this only
# gates the budget BONUS (never adds a second penalty — the near-empty/ordinary floor at or
# below 1.0 is untouched). Unknown rate → full boost (1.0), so it's never a guessed penalty.
BUDGET_RATE_QUALITY_POINTS = [
    (0.10, 0.25), (0.25, 0.35), (0.50, 0.50), (1.00, 0.70), (2.00, 0.90), (3.00, 1.00),
]


def budget_dollars_remaining(c):
    """Actual dollars still in the pool = budget_total × budget_remaining_fraction, or None
    (UNKNOWN) when either is missing. Pure — reads already-scraped fields."""
    total = c.get("budget_total")
    frac = c.get("budget_remaining_fraction")
    if not isinstance(total, (int, float)) or isinstance(total, bool):
        return None
    if not isinstance(frac, (int, float)) or isinstance(frac, bool):
        return None
    return max(0.0, total * frac)


def _budget_rate_quality(pay_per_1k):
    """Fraction (0..1) of the big-budget boost a campaign's pay rate earns. Unknown rate ->
    1.0 (full boost — never a guessed penalty; pay_rate_factor covers rate quality itself)."""
    if not pay_per_1k:
        return 1.0
    return round(_interp_log(pay_per_1k, BUDGET_RATE_QUALITY_POINTS), 4)


def budget_factor(dollars_remaining, pay_per_1k):
    """Budget lever from ABSOLUTE dollars remaining (replaces the old bare-fraction multiplier).
    Log-scaled: near-empty floors (~0.15), ordinary (~$2k) is neutral 1.0, huge pools boost
    (up to ~2.6). The boost ABOVE neutral is tempered by pay rate (a huge pool at a garbage CPM
    isn't real earning opportunity), while the at/below-neutral range is untouched so a small or
    near-empty pool always sinks. Unknown dollars -> neutral 1.0 (never guessed)."""
    if dollars_remaining is None:
        return 1.0
    raw = _interp_log(dollars_remaining, BUDGET_DOLLAR_POINTS)
    if raw <= 1.0:
        return round(raw, 4)
    return round(1.0 + (raw - 1.0) * _budget_rate_quality(pay_per_1k), 4)


def reach_factor(reach):
    """Creator reach/popularity — a PRIMARY view-driver, deliberately WIDE (~0.6x..2.8x)
    so it dominates the pay rate (which spans only ~0.75x..1.4x). `reach` is recent
    traction (avg recent views) when known, else follower/subscriber reach (chosen
    upstream in `_reach_input`). Unknown -> neutral 1.0 (NOT a fabricated follower claim —
    the confidence factor covers the uncertainty)."""
    if not reach:
        return 1.0
    return round(_interp_log(reach,
                             [(1_000, 0.6), (10_000, 1.0), (100_000, 1.6),
                              (1_000_000, 2.2), (10_000_000, 2.8)]), 4)


def expected_earnings(c):
    """Translate proven reach + pay rate into the number that ACTUALLY matters — what a
    typical clip would earn me:

        expected_views_per_clip    = proven clipper MEDIAN views for this creator
        expected_earnings_per_clip = expected_views_per_clip * pay_per_1k / 1000

    The clipper median (pooled across the dedicated clipper accounts `proven_clips` found)
    is the honest 'typical clip' estimate for a zero-audience start — deliberately NOT the
    creator's own channel. Both figures are UNKNOWN (None) when clipper data or the rate is
    missing — never guessed. Returns a dict; pure/testable, no scraping."""
    rc = c.get("repeatable_clippability") or {}
    med = rc.get("median_views")
    evpc = med if isinstance(med, (int, float)) and not isinstance(med, bool) else None
    pay = c.get("pay_per_1k")
    out = {"expected_views_per_clip": evpc, "pay_per_1k": pay,
           "expected_earnings_per_clip": None, "basis": None}
    if evpc is None:
        out["basis"] = "no proven clipper median (clipper data UNKNOWN)"
        return out
    if not pay:
        out["basis"] = "pay rate unknown"
        return out
    out["expected_earnings_per_clip"] = round(evpc * pay / 1000.0, 2)
    out["basis"] = f"{evpc:,.0f} median views × ${pay:.2f}/1k"
    return out


def earnings_factor(expected_earnings_per_clip):
    """Expected $/clip is the number that matters, so it's a HEAVY lever (~0.3x..2.5x):
    <$1/clip heavy penalty, ~$5 neutral, >$50 heavy boost. Unknown -> neutral 1.0 (clipper
    median or rate missing — never guessed)."""
    e = expected_earnings_per_clip
    if e is None:
        return 1.0
    if e <= 0:
        return 0.3
    return round(_interp_log(e, [(0.5, 0.3), (1.0, 0.5), (5.0, 1.0),
                                 (20.0, 1.8), (50.0, 2.3), (200.0, 2.5)]), 4)


# --- data confidence (discount campaigns ranking on UNKNOWNs) ------------------
# The composite maps every UNKNOWN input to a neutral 1.0, so a campaign with NO real data
# can float to the top on nothing but neutrals. This factor discounts exactly that: an
# unverified campaign is a gamble, a verified one a known quantity — rank the proven one
# higher. Keyed on how many of the 5 CORE signals were actually measured.
DATA_CONFIDENCE_FACTORS = {5: 1.0, 4: 0.85, 3: 0.7, 2: 0.55, 1: 0.45, 0: 0.35}


def core_signals_known(c):
    """(known_count, total) over the 5 CORE signals whose presence means the rank rests on
    REAL data, not neutral defaults: repeatable clippability, expected earnings/clip, creator
    reach, footage accessibility, content type. Pure — reads already-filled fields only."""
    rc = c.get("repeatable_clippability") or {}
    src = c.get("source") or {}
    checks = (
        rc.get("score") is not None,                                       # repeatable clippability
        rc.get("median_views") is not None and bool(c.get("pay_per_1k")),  # expected $/clip derivable
        src.get("recent_avg_views") is not None
        or src.get("reach_estimate") is not None,                          # creator reach
        (c.get("footage_access") or {}).get("status") in ("ok", "partial", "none"),  # accessibility
        (c.get("content_type") or {}).get("type") is not None,             # content type
    )
    return sum(1 for x in checks if x), len(checks)


def data_confidence_factor(known_count):
    """5/5 core signals known -> 1.0 (no penalty); most known -> slight discount; most
    UNKNOWN -> heavy discount; nothing known -> heaviest (0.35). Never boosts above 1.0."""
    return DATA_CONFIDENCE_FACTORS.get(known_count, 1.0)


def liveness_factor(liveness_penalty_factor):
    """Footage link-liveness derank. The resolved factor is computed upstream (liveness.py
    sets rec['liveness_penalty_factor'] = cfg.liveness_dead_penalty when all footage is dead,
    else 1.0), so this just validates it: a positive number is used as-is, anything else ->
    neutral 1.0 (fail-open — a live/unknown/unchecked campaign is EXACTLY unchanged)."""
    f = liveness_penalty_factor
    return f if isinstance(f, (int, float)) and not isinstance(f, bool) and f > 0 else 1.0


def footage_presence_factor(footage_presence_penalty_factor):
    """No-public-footage derank. The resolved factor is computed upstream (enrich_active sets
    rec['footage_presence_factor'] = cfg.no_footage_penalty when a FULLY-SCRAPED campaign
    exposes ZERO public footage links, else 1.0), so this just validates it: a positive number
    is used as-is, anything else -> neutral 1.0 (fail-open — a campaign WITH footage, or one
    whose footage presence is undeterminable, is left EXACTLY unchanged). SEPARATE from
    liveness_factor: this fires when NO public link exists, liveness when an existing link is
    dead. Both mean 'can't clip', for different reasons."""
    f = footage_presence_penalty_factor
    return f if isinstance(f, (int, float)) and not isinstance(f, bool) and f > 0 else 1.0


def approval_rate_scaled(rate, floor=65.0, mild=0.85, severe_at=20.0, severe=0.05):
    """Scaled approval-rate derank — the penalty grows with HOW LOW the approval rate is.

    A low approval rate means most submissions are rejected unpaid (wasted effort). UNKNOWN
    (None) or a rate at/above `floor` -> 1.0 (fail-open, no penalty). Just below the floor is a
    MILD derank (`mild`); between the floor and `severe_at` it interpolates linearly down; at or
    below `severe_at` it floors at `severe` — a near-exclusion, so a 6%-approval campaign
    (Michael Sartain's Clipping Army) sinks to the bottom instead of ranking #1. Pure/testable."""
    if not isinstance(rate, (int, float)) or isinstance(rate, bool):
        return 1.0
    if rate >= floor:
        return 1.0
    if rate <= severe_at:
        return round(severe, 4)
    t = (floor - rate) / (floor - severe_at)   # 0 at the floor -> 1 at severe_at
    return round(mild + t * (severe - mild), 4)


def approval_rate_factor(approval_rate_penalty_factor):
    """Approval-rate derank. The resolved factor is computed upstream (enrich_active sets
    rec['approval_rate_factor'] via approval_rate_scaled — mild near the floor, ~0.05 for a
    very-low rate, 1.0 for a high/UNKNOWN rate), so this just validates it: a positive number is
    used as-is, anything else -> neutral 1.0 (fail-open — a high-approval OR UNKNOWN-approval
    campaign is left EXACTLY unchanged)."""
    f = approval_rate_penalty_factor
    return f if isinstance(f, (int, float)) and not isinstance(f, bool) and f > 0 else 1.0


def dedicated_page_factor(dedicated_page_penalty_factor):
    """Dedicated-page/account-required derank. The resolved factor is computed upstream
    (enrich_active sets rec['dedicated_page_factor'] = cfg.dedicated_page_penalty when the rules
    demand a page/account used ONLY for this campaign's content, else 1.0), so this just
    validates it: a positive number is used as-is, anything else -> neutral 1.0 (fail-open — a
    campaign usable from a general account is left EXACTLY unchanged). A dedicated page burns a
    whole account slot, so it should rank below campaigns I can feed from an account I run."""
    f = dedicated_page_penalty_factor
    return f if isinstance(f, (int, float)) and not isinstance(f, bool) and f > 0 else 1.0


def self_sourced_factor(self_sourced_penalty_factor):
    """Self-sourced-footage derank. The resolved factor is computed upstream (enrich_active /
    enrich_self_sourced_docs set rec['self_sourced_factor'] = cfg.self_sourced_penalty when the
    rules tell clippers to find their OWN footage, else 1.0), so this just validates it: a
    positive number is used as-is, anything else -> neutral 1.0 (fail-open — a campaign that
    PROVIDES footage, or whose rules don't clearly demand self-sourcing, is left EXACTLY
    unchanged). Self-sourced footage is un-clippable by a footage-download pipeline."""
    f = self_sourced_penalty_factor
    return f if isinstance(f, (int, float)) and not isinstance(f, bool) and f > 0 else 1.0


def openness_factor(open_to_all):
    """Weight campaigns open to an instant free join UP (the pipeline needs to start clipping
    immediately). 'yes' -> 1.1 (a plus), 'unclear' -> 1.0 (neutral — never guessed), 'no' ->
    0.5 (moot: application-gated is a hard disqualifier that already forces composite 0)."""
    return {"yes": 1.1, "no": 0.5}.get(open_to_all, 1.0)


# --- payout health (is the campaign ACTUALLY paying?) --------------------------
def payout_health(paid, total, submissions, days_active, *,
                  min_age_days=10.0, min_submissions=10,
                  zero_dollars=1.0, zero_fraction=0.005,
                  healthy_spent_fraction=0.02,
                  dead_penalty=0.2, healthy_boost=1.0):
    """Judge whether a campaign is a PAYING-DEAD trap: lots of activity but ~$0 ever paid out.

    Scout scores potential (budget/CPM/reach) but is otherwise blind to whether a campaign
    actually pays. The core judgment (the user's words): "given how long it's been open and how
    many submissions it has, is ~$0 payout suspicious? Old + many submissions + nothing paid =
    trap; new + nothing paid = fine."

    Data reality (STEP 0): `total`/`paid` are reliably scraped; `submissions` is the inline
    activity count Whop shows next to the budget (extract.parse_activity_count); TRUE launch
    date is NOT on the page, so `days_active` is Scout's own tracking age — a LOWER BOUND that
    is 0 on the run a campaign is first seen. So age can only EXONERATE (a positive-but-young
    tracking age proves Scout has watched it a short time), never condemn: when age is
    unmeasured (0/None, first sight) the SUBMISSION count is the evidence the campaign is
    established — that's what catches a just-discovered dead campaign like SomSleep.

    Returns a dict {status, factor, paid_out, spent_fraction, submissions, days_open, reason}:
      - status 'dead'    -> factor `dead_penalty` (heavy derank; NOT an exclude)
      - status 'healthy' -> factor `healthy_boost` (default 1.0 = untouched; >1.0 = small boost)
      - status 'new'/'ok'/'unknown' -> factor 1.0 (fail-open — never penalize on doubt)

    Fail-open everywhere: unknown budget or unknown submissions -> neutral 1.0. Pure/testable."""
    out = {"status": "unknown", "factor": 1.0, "paid_out": paid, "spent_fraction": None,
           "submissions": submissions, "days_open": days_active, "reason": ""}

    def num(x):
        return isinstance(x, (int, float)) and not isinstance(x, bool)

    if not num(paid) or not num(total) or total <= 0:
        out["reason"] = "budget unknown — not judged"
        return out
    spent_frac = paid / total
    out["spent_fraction"] = round(spent_frac, 6)
    near_zero = paid <= zero_dollars or spent_frac <= zero_fraction

    if not num(submissions):
        out["reason"] = "submission count unknown — not judged"
        return out
    if submissions < min_submissions:
        out["status"] = "new"
        out["reason"] = (f"only {int(submissions)} submissions (<{min_submissions}) — "
                         f"too little activity to judge")
        return out

    # age can only EXONERATE: a positive-but-short tracking age means Scout has genuinely
    # watched this a short time, so ~$0 is just newness. Age unmeasured (0/None, first sight)
    # does NOT exonerate — the submission count already established it's active.
    measured_young = num(days_active) and 0 < days_active < min_age_days

    if near_zero:
        if measured_young:
            out["status"] = "new"
            out["reason"] = (f"{int(submissions)} submissions but only ~{days_active:.0f}d "
                             f"tracked (<{min_age_days:.0f}d) — $0 payout not yet suspicious")
            return out
        out["status"] = "dead"
        out["factor"] = dead_penalty
        age_txt = (f"{days_active:.0f}d+ tracked" if num(days_active) and days_active > 0
                   else "since first seen")
        out["reason"] = (f"{int(submissions)} submissions / {age_txt} but ~$0 paid "
                         f"(${paid:,.2f} of ${total:,.0f}) — not paying out")
        return out

    if spent_frac >= healthy_spent_fraction:
        out["status"] = "healthy"
        out["factor"] = healthy_boost
        out["reason"] = (f"paying — ${paid:,.0f} ({spent_frac * 100:.0f}% of budget) across "
                         f"{int(submissions)} submissions")
        return out

    out["status"] = "ok"
    out["reason"] = f"some payout (${paid:,.0f}, {spent_frac * 100:.1f}% of budget)"
    return out


def payout_health_factor(payout_health_penalty_factor):
    """Payout-health derank. The resolved factor is computed upstream (enrich_active sets
    rec['payout_health_factor'] = cfg.payout_dead_penalty for a paying-dead trap, the healthy
    boost, or 1.0), so this just validates it: a positive number is used as-is, anything else ->
    neutral 1.0 (fail-open — an unknown/new/healthy campaign is left EXACTLY unchanged)."""
    f = payout_health_penalty_factor
    return f if isinstance(f, (int, float)) and not isinstance(f, bool) and f > 0 else 1.0


# --- max payout per video ------------------------------------------------------
def max_payout_factor(max_payout, uncapped):
    """Uncapped -> 1.15 (best). >=$300 -> 1.0. Below -> proportional penalty to 0.4.
    Unknown (None, not uncapped) -> neutral 1.0."""
    if uncapped:
        return 1.15
    if max_payout is None:
        return 1.0
    if max_payout >= MAX_PAYOUT_FLOOR:
        return 1.0
    return round(max(0.4, max_payout / MAX_PAYOUT_FLOOR), 4)


def min_view_threshold_factor(threshold):
    """Penalty for a minimum-VIEW payout gate (views a single video must reach before ANY
    payout). Scales hard with the gate — ~0.6x at 1K, ~0.12x (SEVERE) at 10K, ~0.04x at 50K+ —
    because a zero-audience start rarely clears it, so a typical clip earns $0. None/0 ->
    neutral 1.0 (unknown/no gate), never guessed."""
    if not threshold or threshold <= 0:
        return 1.0
    return round(_interp_log(threshold, MIN_VIEW_THRESHOLD_POINTS), 4)


# --- payout velocity -----------------------------------------------------------
# Prefer the OBSERVED budget drain (from snapshot history) over cumulative paid/tracking-age
# once we've watched a campaign for at least this long. Cumulative velocity =
# (total_paid/total)/tracking_age OVER-states an OLD campaign first seen recently: all its
# lifetime spend gets divided by a short tracking age and reads "fast" even though it's barely
# moving now. The observed drain-per-day across runs is the honest, AGE-AWARE spend rate.
VELOCITY_OBSERVED_MIN_SPAN_DAYS = 3.0


def effective_velocity(c):
    """(velocity, basis) — the payout velocity feeding velocity_band/velocity_factor.

    FIX 5 (age-aware): the cumulative velocity = (total_paid/total)/tracking_age OVERSTATES an
    OLD campaign first seen recently — all its lifetime spend divided by a short tracking age
    reads 'fast' even though it's barely moving now (Golden Circle: ~82% spent, but only ~$28/day
    across the window we've watched). The OBSERVED drain-per-day across snapshots is the honest,
    age-aware rate. But observed drain is only used to DOWNGRADE, and only when it's a CLEAN,
    reliable signal — a positive drain over a meaningful span that is LOWER than cumulative (i.e.
    it reveals cumulative overstated). A non-positive observed drain is discarded: it usually
    means a mid-window budget TOP-UP / reset (the budget fraction went UP — the Santa Cruz case),
    NOT a real stall, and must never flip a paying campaign to 'dead'. In every ambiguous case we
    fall back to the cumulative velocity, so a clean campaign is left EXACTLY unchanged. Pure."""
    cumulative = c.get("payout_velocity")
    drain = c.get("budget_drain") or {}
    dpd = drain.get("drain_per_day")
    span = drain.get("span_days")
    reliable = (isinstance(dpd, (int, float)) and not isinstance(dpd, bool) and dpd > 0
                and isinstance(span, (int, float)) and not isinstance(span, bool)
                and span >= VELOCITY_OBSERVED_MIN_SPAN_DAYS)
    # only act as a DOWNGRADE — never upgrade off a noisy recent burst, never bury on a reset
    if reliable and isinstance(cumulative, (int, float)) and dpd < cumulative:
        return dpd, "observed"
    return cumulative, "cumulative"


def velocity_band(velocity, days_active):
    # Calibrated so a healthy campaign clears its budget in ~20–40 days (~0.025–0.05/day);
    # the user's example — $200 of $2,000 after 10 days = 0.01/day — is "slow", a negative.
    if days_active is None or days_active < VELOCITY_MIN_AGE_DAYS:
        return "fresh"  # too new to judge — neutral, not penalized
    if velocity is None:
        return None
    if velocity < 0.005:
        return "dead"
    if velocity < 0.02:
        return "slow"
    if velocity < 0.05:
        return "healthy"
    return "strong"


def velocity_factor(velocity, days_active):
    """Old + creeping payout -> strong negative; fresh -> neutral 1.0.
    velocity = paid_fraction / day. $200/$2,000 over 10 days (0.01/day) -> ~0.6 penalty."""
    if days_active is None or days_active < VELOCITY_MIN_AGE_DAYS or velocity is None:
        return 1.0  # fresh or unknown -> never penalized
    if velocity < 0.003:
        return 0.35
    return round(_interp_log(velocity, [(0.003, 0.4), (0.005, 0.5), (0.01, 0.6),
                                        (0.02, 0.85), (0.04, 1.05), (0.08, 1.2)]), 4)


# --- competition per dollar ----------------------------------------------------
def competition_factor(participants_per_1k):
    """Crowded campaigns (many clippers per $1k budget) are penalized. Unknown -> 1.0."""
    if participants_per_1k is None:
        return 1.0
    if participants_per_1k <= 1:
        return 1.15
    return round(_interp_log(participants_per_1k,
                             [(1, 1.1), (10, 1.0), (30, 0.85), (100, 0.6), (300, 0.4)]), 4)


# --- footage substance (from intake.py: accessibility / type / volume / density) ----
def content_type_factor(content_type):
    """Standard clippable video (stream VOD / podcast) = 1.0. Non-standard is penalized
    HEAVILY (slideshow, music video, tiny-unusual videos like 'The Odyssey', face-UGC).
    UNKNOWN (type None) is neutral 1.0 — never a guessed penalty."""
    if not content_type or content_type.get("type") is None:
        return 1.0
    penalties = {"slideshow_photo": 0.2, "music_video": 0.3,
                 "short_form_only_unusual": 0.25, "ugc_requires_my_face": 0.2,
                 "other": 0.5}
    return penalties.get(content_type["type"], 1.0)


def footage_supply_factor(volume):
    """Recurring sources (an active channel with new VODs) are weighted higher than a
    one-time dump — sustainable footage matters for a daily op. Unknown -> neutral."""
    if not volume:
        return 1.0
    rec = volume.get("recurring")
    if rec == "recurring":
        return 1.2
    if rec == "one_time":
        return 0.85
    return 1.0


def action_density_factor(density):
    """Eventful footage boosted, logistics-heavy footage (the 'WTF Leagues' 80%-talk
    trap) penalized HARD. UNKNOWN -> neutral 1.0 (never guessed)."""
    if not density or density.get("score") is None:
        return 1.0
    return {"logistics_heavy": 0.4, "mixed": 1.0, "eventful": 1.15}.get(
        density.get("band"), 1.0)


def style_fit(c):
    """Style-fit for a CHAOTIC/high-energy clip channel, from signals already captured
    (category + footage content-type + action-density — no new scraping). Returns
    (factor, detail).

    `factor` multiplies the composite ALONGSIDE the money signals: chaos-clippable content
    (stream highlights, action, memes) is boosted, polished produced content (jewelry,
    corporate, music videos) is demoted. A neutral/unknown profile -> exactly 1.0 (never
    guessed into a penalty). Tune STYLE_FIT_WEIGHT (0 disables) or the STYLE_FIT_* affinity
    maps to re-profile. `detail` carries the raw fit, weight, and a human-readable basis."""
    category = c.get("category")
    base = STYLE_FIT_CATEGORY.get(category, 0.5)
    parts = [f"category={category or 'unknown'}({base:.2f})"]

    ctype = (c.get("content_type") or {}).get("type")
    if ctype in STYLE_FIT_CONTENT:
        base += STYLE_FIT_CONTENT[ctype]
        parts.append(f"content={ctype}({STYLE_FIT_CONTENT[ctype]:+.2f})")

    dens = c.get("action_density") or {}
    band = dens.get("band")
    if dens.get("score") is not None and band in STYLE_FIT_DENSITY:
        base += STYLE_FIT_DENSITY[band]
        parts.append(f"action={band}({STYLE_FIT_DENSITY[band]:+.2f})")

    fit = max(0.0, min(1.0, base))
    factor = max(STYLE_FIT_FLOOR, 1.0 + STYLE_FIT_WEIGHT * (2.0 * fit - 1.0))
    return round(factor, 4), {"fit": round(fit, 3), "weight": STYLE_FIT_WEIGHT,
                              "basis": " ".join(parts)}


def footage_access_factor(access):
    """Partial accessibility (some sources work, some don't) takes a penalty. Full
    inaccessibility is handled as a disqualifier (composite forced to 0), not here."""
    if not access:
        return 1.0
    return 0.8 if access.get("status") == "partial" else 1.0


# =============================================================================
# CROSS-RUN + STRATEGIC signals (built on the per-run snapshot history scout already
# accumulates — no new scraping). Every one degrades to UNKNOWN/neutral, never guessed.
# =============================================================================
def _timed_points(snaps, key):
    """[(datetime, value)] sorted by time for a numeric snapshot field. Skips snapshots
    missing the field or a parseable ts."""
    pts = []
    for s in snaps or []:
        if not s:
            continue
        ts, v = s.get("ts"), s.get(key)
        if ts and isinstance(v, (int, float)) and not isinstance(v, bool):
            try:
                pts.append((datetime.fromisoformat(ts), v))
            except Exception:
                continue
    pts.sort(key=lambda p: p[0])
    return pts


# --- 1. budget drain rate → projected lifespan --------------------------------
def project_budget_drain(history, current):
    """Projected days until the budget empties, from budget_remaining_fraction across
    runs. Distinct from payout velocity: this is about LIFESPAN. Bands: stalled (barely
    draining → clips not earning), too_fast (empties before I can exploit), sweet,
    slow. UNKNOWN until ≥2 runs of history exist — never guessed."""
    snaps = list(history or []) + ([current] if current else [])
    pts = _timed_points(snaps, "budget_remaining_fraction")
    out = {"days_until_empty": None, "drain_per_day": None, "band": "UNKNOWN",
           "span_days": None, "basis": "need ≥2 runs of history"}
    if len(pts) < 2:
        return out
    (t0, r0), (t1, r1) = pts[0], pts[-1]
    span = (t1 - t0).total_seconds() / 86400.0
    if span <= 0:
        out["basis"] = "no time span between snapshots"
        return out
    drained = r0 - r1                      # fraction of budget consumed over the span
    drain_per_day = drained / span
    out.update(span_days=round(span, 2), drain_per_day=round(drain_per_day, 6),
               basis=f"{drained * 100:.1f}% of budget drained over {span:.1f}d")
    if drain_per_day <= 0.0005:            # essentially not moving
        out.update(band="stalled", days_until_empty=None,
                   basis="budget barely moving across runs — clips aren't earning")
        return out
    dte = max(r1, 0.0) / drain_per_day
    out["days_until_empty"] = round(dte, 1)
    out["band"] = ("too_fast" if dte < 5 else "fast" if dte < 14
                   else "sweet" if dte <= 60 else "slow")
    return out


def budget_drain_factor(drain):
    """Sweet-spot lifespan boosted; stalled/too-fast/slow penalized. Unknown -> 1.0."""
    if not drain:
        return 1.0
    return {"stalled": 0.5, "too_fast": 0.6, "fast": 0.9, "sweet": 1.15,
            "slow": 0.7, "UNKNOWN": 1.0}.get(drain.get("band"), 1.0)


# --- 2. participant growth rate -----------------------------------------------
def participant_growth(history, current):
    """Participant-count growth across runs. Fast growth = the field is saturating (my
    clips compete harder); flat/slow = opportunity. UNKNOWN until ≥2 runs."""
    snaps = list(history or []) + ([current] if current else [])
    pts = _timed_points(snaps, "participants")
    out = {"per_day": None, "pct_per_day": None, "direction": "UNKNOWN",
           "band": "UNKNOWN", "span_days": None, "basis": "need ≥2 runs of history"}
    if len(pts) < 2:
        return out
    (t0, p0), (t1, p1) = pts[0], pts[-1]
    span = (t1 - t0).total_seconds() / 86400.0
    if span <= 0:
        out["basis"] = "no time span between snapshots"
        return out
    per_day = (p1 - p0) / span
    pct_per_day = per_day / max(p0, 1)
    if pct_per_day >= 0.05:
        band, direction = "growing_fast", "saturating"
    elif pct_per_day >= 0.01:
        band, direction = "growing", "growing"
    elif pct_per_day > -0.01:
        band, direction = "flat", "flat"
    else:
        band, direction = "declining", "declining"
    out.update(per_day=round(per_day, 3), pct_per_day=round(pct_per_day, 4),
               direction=direction, band=band, span_days=round(span, 2),
               basis=f"{p0}→{p1} clippers over {span:.1f}d")
    return out


def participant_growth_factor(growth):
    """Fast growth (saturating) penalized; flat (opportunity) bonused. Unknown -> 1.0."""
    if not growth:
        return 1.0
    return {"growing_fast": 0.8, "growing": 0.95, "flat": 1.1,
            "declining": 1.0, "UNKNOWN": 1.0}.get(growth.get("band"), 1.0)


# --- 3. source saturation (reuses the proven_clips clipper-search results) ------
def source_saturation_estimate(campaign):
    """How mined-out is this creator's footage? Derived from the clipper-account search
    proven_clips already ran ("<creator> clips") — many clipper accounts + existing
    clips = the good moments are taken. NO new scraping. UNKNOWN when that search
    didn't run / was inconclusive — never guessed."""
    rc = campaign.get("repeatable_clippability") or {}
    if rc.get("candidates_evaluated") is None:
        # the "<creator> clips" search never ran (no creator name / yt-dlp absent)
        return {"level": "UNKNOWN", "score": None,
                "basis": "clipper search not run / inconclusive"}
    # Saturation = how many existing clips of this creator's content already exist. Use
    # the ACTUAL clips found + dedicated clipper accounts (candidates_evaluated saturates
    # at the search cap, so it's a poor discriminator and is not used for the score).
    clips = rc.get("clip_count") or 0
    trusted = rc.get("trusted_clippers") or 0
    clip_sig = min(clips, 40) / 40.0
    trust_sig = min(trusted, 5) / 5.0
    score = round(0.7 * clip_sig + 0.3 * trust_sig, 3)
    level = "low" if score < 0.33 else "medium" if score < 0.66 else "high"
    return {"level": level, "score": score,
            "basis": f"{clips} existing clips across {trusted} dedicated clipper account(s)"}


def saturation_factor(saturation):
    """Fresh footage (low saturation) bonused; mined-out (high) penalized. Unknown 1.0."""
    if not saturation:
        return 1.0
    return {"low": 1.1, "medium": 1.0, "high": 0.7, "UNKNOWN": 1.0}.get(
        saturation.get("level"), 1.0)


# --- 4. recurring creator (factor; the count is computed in strategic.py) -------
def recurring_creator_factor(recurring):
    """A creator/brand that has run multiple campaigns over time is worth building an
    account for (it keeps earning after this one ends). Unknown -> neutral 1.0."""
    if not recurring or recurring.get("previous_count") is None:
        return 1.0
    p = recurring["previous_count"]
    return 1.2 if p >= 3 else 1.1 if p >= 1 else 1.0


# --- 5. account reusability ----------------------------------------------------
_NEW_ACCOUNT_RE = re.compile(
    r"\b(new\s+account|dedicated\s+account|fresh\s+account|brand[-\s]new\s+account|"
    r"separate\s+account|create\s+an?\s+account|make\s+an?\s+account|branded\s+account|"
    r"account\s+(?:must\s+be\s+)?named|named\s+account|specific\s+(?:username|handle|name)|"
    r"(?:username|handle)\s+must\s+be|account\s+with\s+the\s+name)\b", re.I)


def account_reusability(category, content_type, rules_text):
    """Can this feed an account I already run in this category, or does it demand a
    fresh dedicated/branded account (a real cost)? Reads the brief + category/type."""
    requires_new = bool(_NEW_ACCOUNT_RE.search(rules_text or ""))
    usable_category = category not in (None, "other")
    non_standard = content_type in ("slideshow_photo", "music_video",
                                    "short_form_only_unusual", "ugc_requires_my_face")
    reusable = usable_category and not requires_new and not non_standard
    if requires_new:
        reason = "requires a fresh dedicated/branded account (cost)"
    elif reusable:
        reason = f"can feed an existing {category} account (warmed — a plus)"
    elif not usable_category:
        reason = "category unknown — can't match to an existing account"
    else:
        reason = "non-standard content — likely needs a purpose-built account"
    return {"reusable": reusable, "requires_new_account": requires_new,
            "category": category, "reason": reason}


def reusability_factor(reuse):
    """Reusing a warmed category account is a plus; a required new account is a cost."""
    if not reuse:
        return 1.0
    if reuse.get("requires_new_account"):
        return 0.8
    return 1.1 if reuse.get("reusable") else 1.0


# --- 6. PERFORMANCE LEARNING HOOK (structure only — NOT wired to real logic yet) ----
def performance_factor(campaign, my_performance=None):
    """STUB for a future personalized-learning weight. Returns a NEUTRAL 1.0 today so it
    changes nothing — this is only the wiring point.

    FUTURE (deliberately not built): given `my_performance` (loaded from
    my_performance.json — my own recorded results: clips posted, views, earnings, and
    whether I'd repeat), this would boost campaigns/creators/categories where I actually
    earned and dampen ones I ran and regretted. It would match `campaign` to prior
    outcomes by creator/category/content_type. Until that logic + enough recorded
    results exist, it MUST stay 1.0 (never fabricate a personalized weight).
    """
    return 1.0


# --- cross-run trends ----------------------------------------------------------
def compute_trends(current, previous):
    """Diff a fresh snapshot against the previous run's. Returns a dict with per-metric
    deltas + an overall arrow/label (accelerating / steady / stalling / dead). Trend
    beats snapshot. `available=False` on the first data point — never fabricated."""
    if not previous:
        return {"available": False, "label": "first data point", "arrow": "·"}

    def delta(key):
        a, b = current.get(key), previous.get(key)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return a - b
        return None

    # Views/submissions are no longer scraped (that stats read was unreliable — see
    # scoring's docstring on dropping views-per-submission), so the trend runs off the two
    # volatile card-level metrics we DO read every run: budget paid out and participants.
    d = {k: delta(k) for k in ("budget_paid", "participants")}
    moved = [v for v in d.values() if v is not None]
    if not moved:
        return {"available": False, "label": "no comparable metrics", "arrow": "·", **d}

    budget_up = (d.get("budget_paid") or 0) > 0
    participants_up = (d.get("participants") or 0) > 0
    any_move = any((v or 0) > 0 for v in moved)

    if budget_up and participants_up:
        label, arrow = "accelerating", "↑"   # paying out AND drawing more clippers
    elif any_move:
        label, arrow = "steady", "→"
    else:
        # nothing moved since last run — budget not paying out and no new clippers
        label, arrow = ("dead" if d.get("budget_paid") == 0 else "stalling"), "↓"
    return {"available": True, "label": label, "arrow": arrow,
            "budget_paid_delta": d.get("budget_paid"),
            "participants_delta": d.get("participants")}


def repeatable_factor(c):
    """(factor, score_or_None, confidence) from a campaign's repeatable_clippability.

    Unknown clip data -> neutral 1.0 (marked UNKNOWN), never a fabricated boost or
    penalty. A known score maps linearly onto the heavy 0.5x..2.5x range.
    """
    rc = c.get("repeatable_clippability") or {}
    score = rc.get("score")
    conf = rc.get("confidence") or "UNKNOWN"
    if score is None:
        return 1.0, None, conf
    score = max(0.0, min(1.0, score))
    return round(REPEATABLE_MIN_FACTOR + REPEATABLE_SPAN * score, 4), score, conf


def pre_score(c):
    """The original crude hint. Unchanged. Not a recommendation."""
    pay = c.get("pay_per_1k") or 0.0
    rem = c.get("budget_remaining_fraction")
    rem = rem if rem is not None else 0.0
    participants = c.get("participants") or 0
    return round(pay * rem / max(participants, 1), 6)


def clippability(c):
    """Rough 0..1 proxy for how easy this campaign is to make clips for.

    Short-form platforms (tiktok/shorts/reels) are the easy-clip case; provided
    source footage (drive/youtube links) means you don't have to source raw material.
    With no signal at all we return a neutral 0.5 rather than penalising the unknown.
    """
    plats = set(c.get("platforms") or [])
    has_source = bool(c.get("source_links"))
    if not plats and not has_source:
        return 0.5
    score = 0.0
    if plats & {"tiktok", "shorts", "reels"}:
        score += 0.6
    if has_source:
        score += 0.4
    return round(min(score, 1.0), 4) if score else 0.3


def _reach_input(c):
    """Reach value feeding the score, preferring recent traction over raw followers.

    Returns (value_or_None, basis) where basis is 'recent_traction', 'followers', or
    'unknown'. A creator with big followings but weak recent views ranks on the weak
    recent views — recent traction beats raw follower count.
    """
    src = c.get("source") or {}
    traction = src.get("recent_avg_views")
    if traction:
        return traction, "recent_traction"
    reach = src.get("reach_estimate")
    if reach:
        return reach, "followers"
    return None, "unknown"


def composite_score(c):
    """Return (composite_score, breakdown_dict). Never raises."""
    pay = c.get("pay_per_1k") or 0.0
    pay_fac = pay_rate_factor(pay)  # pay is now a MODEST nudge, not the primary driver
    rem = c.get("budget_remaining_fraction")
    rem = rem if rem is not None else 0.0
    # Budget lever = ACTUAL DOLLARS REMAINING (total × fraction), not the bare fraction — "90%
    # left" is $9k on a $10k pool but $90 on a $100 pool, and those must NOT score the same.
    # The big-budget boost is tempered by the pay rate (real money AT a decent rate); pay rate
    # also keeps its own separate lever above.
    dollars_rem = budget_dollars_remaining(c)
    budget_fac = budget_factor(dollars_rem, pay)
    clip = clippability(c)

    reach_val, reach_basis = _reach_input(c)
    reach_fac = reach_factor(reach_val)  # PRIMARY view-driver, wide range (dominates pay)

    # expected earnings per clip — the number that actually matters (reach × rate). Heavy
    # lever when known; UNKNOWN -> neutral. Also feeds the strict minimum-payout gate.
    earn = expected_earnings(c)
    evpc = earn["expected_views_per_clip"]
    eepc = earn["expected_earnings_per_clip"]
    earn_fac = earnings_factor(eepc)

    conf = (c.get("source") or {}).get("confidence") or "UNKNOWN"
    confidence_factor = CONFIDENCE_FACTORS.get(conf, 0.5)

    high_min = bool(c.get("high_minimum"))
    minimum_penalty = HIGH_MINIMUM_PENALTY if high_min else 1.0

    # Strict minimum-payout gate: if a TYPICAL clip's expected views fall below the views
    # needed to reach the minimum payout, a typical clip earns $0 — heavy penalty. Only
    # fires when both the proven expected views and the min-views threshold are known.
    mvtp = c.get("min_views_to_payout")
    below_min = evpc is not None and mvtp is not None and evpc < mvtp
    below_min_penalty = EXPECTED_BELOW_MIN_PENALTY if below_min else 1.0

    # Minimum-VIEW payout gate (distinct from the dollar minimum): a hard view count a video
    # must reach before ANY payout — brutal for a new account. Penalty scales with the gate.
    mvt = c.get("min_view_threshold")
    mvt_fac = min_view_threshold_factor(mvt)

    rep_factor, rep_score, rep_conf = repeatable_factor(c)

    # supporting levers
    mp = c.get("max_payout_per_video")
    uncapped = bool(c.get("max_payout_uncapped"))
    mp_fac = max_payout_factor(mp, uncapped)

    # velocity — prefer OBSERVED drain across runs over cumulative paid/tracking-age (FIX 5),
    # so an old, mostly-spent, dead-slow campaign no longer reads 'fast' off its high %-spent.
    vel, vel_basis = effective_velocity(c)
    days = c.get("days_active")
    vel_fac = velocity_factor(vel, days)
    vel_band = velocity_band(vel, days)

    ppk = c.get("participants_per_1k_budget")
    comp_fac = competition_factor(ppk)

    # footage substance (intake.py): accessibility / content-type / volume / density
    content = c.get("content_type")
    ctype_fac = content_type_factor(content)
    supply_fac = footage_supply_factor(c.get("footage_volume"))
    density = c.get("action_density")
    density_fac = action_density_factor(density)
    access = c.get("footage_access")
    access_fac = footage_access_factor(access)

    # style fit — chaos-clip channel affinity from category + content-type + action-density.
    # A tunable multiplier ALONGSIDE the money signals (STYLE_FIT_WEIGHT is the knob).
    style_fac, style_detail = style_fit(c)

    # cross-run + strategic signals (filled by strategic.compute_strategic_signals)
    drain = c.get("budget_drain")
    drain_fac = budget_drain_factor(drain)
    growth = c.get("participant_growth")
    growth_fac = participant_growth_factor(growth)
    saturation = c.get("source_saturation")
    sat_fac = saturation_factor(saturation)
    recurring = c.get("recurring_creator")
    recur_fac = recurring_creator_factor(recurring)
    reuse = c.get("account_reusability")
    reuse_fac = reusability_factor(reuse)
    perf_fac = performance_factor(c)  # STUB — always 1.0 today (see performance_factor)

    # data confidence — discount campaigns ranking mostly on UNKNOWN neutrals (a gamble
    # shouldn't outrank a proven campaign just because every UNKNOWN scores a neutral 1.0).
    core_known, core_total = core_signals_known(c)
    dc_fac = data_confidence_factor(core_known)

    # openness — boost campaigns open to an instant join; application-gated ones are a hard DQ.
    open_to_all = c.get("open_to_all") or "unclear"
    open_fac = openness_factor(open_to_all)

    # non-English derank — heavy penalty for a clearly non-English campaign (English-only op).
    # The factor is resolved upstream in enrich_active from cfg.nonenglish_penalty; a missing/
    # English/ambiguous campaign carries 1.0, leaving English composites EXACTLY unchanged.
    lang = c.get("language") or {}
    lang_fac = c.get("language_penalty_factor")
    lang_fac = lang_fac if isinstance(lang_fac, (int, float)) and lang_fac > 0 else 1.0

    # footage link-liveness derank — heavy penalty when ALL footage links are dead/offline
    # (resolved upstream in liveness.probe_campaign from cfg.liveness_dead_penalty). Live/
    # unknown/unchecked -> 1.0 (fail-open), leaving those composites EXACTLY unchanged.
    live = c.get("liveness") or {}
    live_fac = liveness_factor(c.get("liveness_penalty_factor"))

    # no-public-footage derank — heavy penalty when a FULLY-SCRAPED campaign exposes ZERO
    # public footage links (footage member-gated or absent, so intake can't download anything).
    # SEPARATE from liveness (dead existing link). Resolved upstream in enrich_active from
    # cfg.no_footage_penalty; has-footage / undeterminable -> 1.0 (fail-open), unchanged.
    fpres = c.get("footage_presence") or {}
    fpres_fac = footage_presence_factor(c.get("footage_presence_factor"))

    # approval-rate derank — heavy penalty when a KNOWN approval rate is below the floor (most
    # submissions rejected unpaid). Resolved upstream in enrich_active from cfg.approval_low_
    # penalty; high-approval / UNKNOWN -> 1.0 (fail-open), leaving those composites unchanged.
    appr = c.get("approval_rate")
    appr_fac = approval_rate_factor(c.get("approval_rate_factor"))

    # self-sourced-footage derank — heavy penalty when the rules tell clippers to find their OWN
    # footage (campaign provides none — un-clippable by a footage-download pipeline). Resolved
    # upstream from cfg.self_sourced_penalty; provides-footage / ambiguous -> 1.0 (fail-open).
    ss_fac = self_sourced_factor(c.get("self_sourced_factor"))

    # dedicated-page/account derank — the rules demand a page used ONLY for this campaign's
    # content (burns a whole account slot). Resolved upstream from cfg.dedicated_page_penalty;
    # usable-from-a-general-account / unknown -> 1.0 (fail-open).
    dp_fac = dedicated_page_factor(c.get("dedicated_page_factor"))

    # payout-health derank — is the campaign ACTUALLY paying? A paying-dead trap (meaningful
    # submissions but ~$0 ever paid out) is heavy-deranked; a genuinely new campaign with $0
    # paid is left untouched (fail-open). Factor resolved upstream in enrich_active.
    payout = c.get("payout_health") or {}
    payout_fac = payout_health_factor(c.get("payout_health_factor"))

    disqualified = bool(c.get("disqualifiers"))

    # Base is driven by ABSOLUTE budget dollars (not bare %) and REACH (primary), only nudged
    # by the pay rate (modest) — views dominate rate. Expected-earnings-per-clip (reach × rate)
    # is a heavy top lever.
    base = budget_fac * clip * reach_fac * pay_fac
    composite = round(
        base * confidence_factor * minimum_penalty * below_min_penalty * mvt_fac
        * earn_fac * rep_factor * mp_fac * vel_fac * comp_fac
        * ctype_fac * supply_fac * density_fac * access_fac * style_fac
        * drain_fac * growth_fac * sat_fac * recur_fac * reuse_fac * perf_fac
        * dc_fac * open_fac * lang_fac * live_fac * fpres_fac * payout_fac
        * appr_fac * ss_fac * dp_fac, 6)
    if disqualified:
        composite = 0.0  # sinks to the bottom (still shown in the DISQUALIFIED section)

    rc = c.get("repeatable_clippability") or {}
    breakdown = {
        "pay_per_1k": pay,
        "pay_rate_factor": pay_fac,
        "budget_remaining_fraction": round(rem, 4),
        # budget as ABSOLUTE dollars remaining (total × fraction) — the real pool size, not a %
        "budget_total": c.get("budget_total"),
        "budget_dollars_remaining": round(dollars_rem, 2) if dollars_rem is not None else None,
        "budget_factor": budget_fac,
        "reach_value": reach_val,
        "reach_basis": reach_basis,
        "reach_factor": reach_fac,
        "clippability": clip,
        # expected earnings per clip — the headline number (reach × rate)
        "expected_views_per_clip": evpc,
        "expected_earnings_per_clip": eepc,
        "earnings_basis": earn["basis"],
        "earnings_factor": earn_fac,
        "confidence": conf,
        "confidence_factor": confidence_factor,
        "high_minimum": high_min,
        "minimum_penalty": minimum_penalty,
        "min_views_to_payout": c.get("min_views_to_payout"),
        # strict minimum-payout gate on PROVEN expected views (typical clip earns $0)
        "expected_below_minimum": below_min,
        "expected_below_min_penalty": below_min_penalty,
        # minimum-VIEW payout gate — hard view count a video must clear before ANY payout
        "min_view_threshold": mvt,
        "min_view_threshold_factor": mvt_fac,
        # repeatable-clippability — THE clip-quality lever (replaces views-per-submission)
        "repeatable_score": rep_score,
        "repeatable_confidence": rep_conf,
        "repeatable_factor": rep_factor,
        "repeatable_median_views": rc.get("median_views"),
        "repeatable_median_to_follower": rc.get("median_view_to_follower"),
        # supporting levers
        "max_payout_per_video": mp,
        "max_payout_uncapped": uncapped,
        "max_payout_factor": mp_fac,
        "payout_velocity": vel,
        "velocity_basis": vel_basis,   # 'observed' (drain across runs) vs 'cumulative' (age-honest)
        "velocity_band": vel_band,
        "velocity_factor": vel_fac,
        "days_active": days,
        "participants_per_1k_budget": ppk,
        "competition_factor": comp_fac,
        # footage substance (intake.py)
        "content_type": (content or {}).get("type"),
        "content_type_factor": ctype_fac,
        "footage_recurring": (c.get("footage_volume") or {}).get("recurring"),
        "footage_total_hours": (c.get("footage_volume") or {}).get("total_hours"),
        "footage_supply_factor": supply_fac,
        "action_density_band": (density or {}).get("band"),
        "action_density_score": (density or {}).get("score"),
        "action_density_factor": density_fac,
        "footage_access_status": (access or {}).get("status"),
        "footage_access_factor": access_fac,
        # style fit — chaos-clip channel affinity (category + content-type + action-density)
        "style_fit": style_detail["fit"],
        "style_fit_weight": style_detail["weight"],
        "style_fit_basis": style_detail["basis"],
        "style_fit_factor": style_fac,
        # cross-run + strategic signals
        "budget_days_until_empty": (drain or {}).get("days_until_empty"),
        "budget_drain_band": (drain or {}).get("band"),
        "budget_drain_factor": drain_fac,
        "participant_growth_band": (growth or {}).get("direction"),
        "participant_growth_factor": growth_fac,
        "source_saturation_level": (saturation or {}).get("level"),
        "saturation_factor": sat_fac,
        "recurring_previous_count": (recurring or {}).get("previous_count"),
        "recurring_creator_factor": recur_fac,
        "account_requires_new": (reuse or {}).get("requires_new_account"),
        "reusability_factor": reuse_fac,
        "performance_factor": perf_fac,
        # data confidence — how much of the ranking rests on real data vs neutral defaults
        "core_signals_known": core_known,
        "core_signals_total": core_total,
        "data_confidence_factor": dc_fac,
        # openness — open to an instant join (yes/no/unclear); gated is a hard DQ
        "open_to_all": open_to_all,
        "openness_factor": open_fac,
        # non-English derank (English-only op) — 1.0 for English/ambiguous (no change)
        "language": lang.get("language"),
        "language_nonenglish": bool(lang.get("nonenglish")),
        "language_confidence": lang.get("confidence"),
        "language_factor": lang_fac,
        # footage link-liveness — heavy derank when all footage is dead/offline (fail-open)
        "liveness_status": live.get("status"),
        "liveness_penalized": bool(live.get("penalized")),
        "liveness_factor": live_fac,
        # no-public-footage — heavy derank when zero public footage links (fail-open)
        "has_public_footage": fpres.get("has_public_footage"),
        "footage_link_count": fpres.get("footage_link_count"),
        "footage_presence_factor": fpres_fac,
        # approval rate — heavy derank when a KNOWN rate is below the floor (fail-open on UNKNOWN)
        "approval_rate": appr,
        "approval_rate_factor": appr_fac,
        # self-sourced footage — heavy derank when clippers must find their own footage (fail-open)
        "self_sourced": bool(c.get("self_sourced")),
        "self_sourced_phrase": c.get("self_sourced_phrase"),
        "self_sourced_factor": ss_fac,
        # dedicated-page required — derank when the rules demand an account used only for this
        # campaign (burns an account slot). Fail-open when usable from a general account.
        "dedicated_page_required": bool(c.get("dedicated_page_required")),
        "dedicated_page_phrase": c.get("dedicated_page_phrase"),
        "dedicated_page_factor": dp_fac,
        # member-gated rules FLAG (not a derank) — the full rules live behind joining, so the
        # captured rules are only PARTIAL and shouldn't be fully trusted.
        "rules_incomplete": bool(c.get("rules_incomplete")),
        "rules_incomplete_phrase": c.get("rules_incomplete_phrase"),
        # payout health — is the campaign actually paying? (dead/healthy/new/ok/unknown)
        "payout_status": payout.get("status"),
        "payout_submissions": payout.get("submissions"),
        "payout_days_open": payout.get("days_open"),
        "payout_paid_out": payout.get("paid_out"),
        "payout_spent_fraction": payout.get("spent_fraction"),
        "payout_factor": payout_fac,
        "disqualified": disqualified,
        "composite": composite,
    }
    return composite, breakdown


# =============================================================================
# CATEGORY-LEVEL RANKING — on top of per-campaign scoring. Each ACTIVE campaign
# contributes its composite to EVERY category it fits (multi-tag), and a category's
# score aggregates its members. Per-campaign ranking is unchanged and still primary.
# =============================================================================
# How a category's score is aggregated from its members' composites. Numeric modes take the
# top-N; "average" uses all members; "best" the single top. Config knob `category_agg`.
CATEGORY_AGG_MODES = {"top3": 3, "top5": 5, "top10": 10, "average": None, "best": 1}
CATEGORY_AGG_DEFAULT = "top5"
# A category needs at least this many member campaigns to rank as "full"; fewer -> "thin"
# (its score rests on a small sample, so it's flagged rather than trusted outright).
CATEGORY_FULL_MIN = 5


def _aggregate_category(scores_desc, agg):
    """Aggregate a category's member composites (already sorted high->low) per `agg`.
    Empty -> 0.0."""
    if not scores_desc:
        return 0.0
    if agg == "average":
        vals = scores_desc
    elif agg == "best":
        vals = scores_desc[:1]
    else:
        vals = scores_desc[:CATEGORY_AGG_MODES.get(agg, 5)]
    return round(sum(vals) / len(vals), 6)


def _is_rankable(c):
    """A campaign that COUNTS toward category scores: actually scored this run and not excluded
    by any hard gate (gambling/other DQ, rules_unreadable, or prohibited category). The
    view-floor penalty is NOT an exclusion — it lives in the composite, so a view-floored
    campaign still counts, just with its already-penalized (low) score."""
    return (c.get("status") in ("scraped", "refreshed")
            and not c.get("disqualified")
            and not c.get("rules_unreadable")
            and not c.get("excluded_prohibited"))


def rank_categories(campaigns, agg=CATEGORY_AGG_DEFAULT, *, full_min=CATEGORY_FULL_MIN):
    """Rank categories by an aggregate of their member campaigns' composites (highest first).

    Each rankable campaign counts toward exactly ONE category — its PRIMARY (`category`) — so
    secondary tags never inflate the counts. Excluded campaigns never count (`_is_rankable`).
    `agg` picks the aggregation (top5|top3|top10|average|best); an unknown value falls back to
    the default. Categories with < `full_min` members are flagged `thin`. Returns
    [{category, score, count, thin, agg, top_campaigns[]}...], score-descending."""
    if agg not in CATEGORY_AGG_MODES:
        agg = CATEGORY_AGG_DEFAULT
    buckets = {}
    for c in campaigns:
        if not _is_rankable(c):
            continue
        comp = c.get("composite_score") or 0
        # PRIMARY category only (guard #6): a campaign counts toward exactly ONE category so
        # secondaries can't inflate counts. `categories`[0] == the primary; fall back cleanly.
        cat = c.get("category") or (c.get("categories") or ["other"])[0] or "other"
        member = {"id": c.get("id"), "name": c.get("name"), "composite_score": comp}
        buckets.setdefault(cat, []).append(member)
    ranking = []
    for cat, members in buckets.items():
        members.sort(key=lambda m: m["composite_score"], reverse=True)
        scores = [m["composite_score"] for m in members]
        ranking.append({
            "category": cat,
            "score": _aggregate_category(scores, agg),
            "count": len(members),
            "thin": len(members) < full_min,
            "agg": agg,
            "top_campaigns": members[:5],
        })
    ranking.sort(key=lambda r: r["score"], reverse=True)
    return ranking
