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


def openness_factor(open_to_all):
    """Weight campaigns open to an instant free join UP (the pipeline needs to start clipping
    immediately). 'yes' -> 1.1 (a plus), 'unclear' -> 1.0 (neutral — never guessed), 'no' ->
    0.5 (moot: application-gated is a hard disqualifier that already forces composite 0)."""
    return {"yes": 1.1, "no": 0.5}.get(open_to_all, 1.0)


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

    vel = c.get("payout_velocity")
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

    disqualified = bool(c.get("disqualifiers"))

    # Base is now driven by REACH (primary) and only nudged by the pay rate (modest) —
    # views dominate rate. Expected-earnings-per-clip (reach × rate) is a heavy top lever.
    base = rem * clip * reach_fac * pay_fac
    composite = round(
        base * confidence_factor * minimum_penalty * below_min_penalty * mvt_fac
        * earn_fac * rep_factor * mp_fac * vel_fac * comp_fac
        * ctype_fac * supply_fac * density_fac * access_fac * style_fac
        * drain_fac * growth_fac * sat_fac * recur_fac * reuse_fac * perf_fac
        * dc_fac * open_fac, 6)
    if disqualified:
        composite = 0.0  # sinks to the bottom (still shown in the DISQUALIFIED section)

    rc = c.get("repeatable_clippability") or {}
    breakdown = {
        "pay_per_1k": pay,
        "pay_rate_factor": pay_fac,
        "budget_remaining_fraction": round(rem, 4),
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

    Each rankable campaign contributes to EVERY category in its `categories` multi-tag (a
    sports-podcast lifts BOTH sports and podcast_talking). Excluded campaigns never count
    (`_is_rankable`). `agg` picks the aggregation (top5|top3|top10|average|best); an unknown
    value falls back to the default. Categories with < `full_min` members are flagged `thin`.
    Returns [{category, score, count, thin, agg, top_campaigns[]}...], score-descending."""
    if agg not in CATEGORY_AGG_MODES:
        agg = CATEGORY_AGG_DEFAULT
    buckets = {}
    for c in campaigns:
        if not _is_rankable(c):
            continue
        comp = c.get("composite_score") or 0
        cats = c.get("categories") or [c.get("category") or "other"]
        member = {"id": c.get("id"), "name": c.get("name"), "composite_score": comp}
        for cat in cats:
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
