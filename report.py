"""Outputs: campaigns.json, campaigns_summary.md, and the terminal final report."""
import json
from datetime import datetime, timezone

import scoring

_CATEGORY_LABELS = {
    "streamer_irl": "Streamer/IRL", "gaming": "Gaming", "sports": "Sports",
    "podcast_talking": "Podcast/Talking", "brand_product": "Brand/Product",
    "music": "Music", "meme": "Meme", "news": "News", "movie_tv": "Movie/TV",
    "other": "Other",
}
_CATEGORY_ORDER = list(_CATEGORY_LABELS.keys())


def _composite_of(c):
    return c.get("composite_score") or 0


def _cat_label(cat):
    return _CATEGORY_LABELS.get(cat, (cat or "other").replace("_", " ").title())


def _category_breakdown_md(cs):
    """Markdown for the Groq recategorization breakdown: count per category, low-confidence
    total, and how far 'other' shrank vs the legacy keyword tagger."""
    if not cs:
        return []
    lines = ["## Categorization (Groq)", ""]
    srcs = cs.get("sources") or {}
    lines.append(f"{cs.get('total', 0)} campaigns categorized "
                 f"(groq {srcs.get('groq', 0)} · cache {srcs.get('cache', 0)} · "
                 f"keyword-fallback {srcs.get('keyword_fallback', 0)}). "
                 f"{cs.get('low_confidence', 0)} low-confidence.")
    now_other = cs.get("other_now", 0)
    base = cs.get("other_keyword_baseline")
    if base is not None:
        delta = base - now_other
        lines.append(f"**\"Other\" is now {now_other}** (keyword tagger would put {base} here "
                     f"on this set — a {delta:+d} change).")
    lines.append("")
    for cat, n in (cs.get("by_category") or {}).items():
        lines.append(f"- {_cat_label(cat)}: {n}")
    lines.append("")
    return lines


def _category_ranking_md(category_ranking):
    """Markdown for the category-level ranking (highest first). Thin categories (fewer than
    the full-min members) are flagged so a score resting on a small sample is obvious."""
    lines = ["## Category ranking", ""]
    if not category_ranking:
        lines.extend(["(none — no rankable campaigns)", ""])
        return lines
    agg = category_ranking[0].get("agg", "top5")
    lines.append(f"Each category scored as the **{agg}** of its member campaigns' composites "
                 "(a campaign counts toward every category it fits; excluded campaigns don't "
                 "count). **THIN** = fewer than 5 campaigns, so the score rests on a small "
                 "sample.")
    lines.append("")
    for i, r in enumerate(category_ranking, 1):
        thin = " **[THIN]**" if r.get("thin") else ""
        top = ", ".join(m.get("name") or "(unnamed)" for m in (r.get("top_campaigns") or [])[:3])
        lines.append(f"{i}. **{_cat_label(r.get('category'))}** — score {r.get('score'):.4f} "
                     f"· {r.get('count')} campaign(s){thin}"
                     + (f" · top: {top}" if top else ""))
    lines.append("")
    return lines


def write_json(path, campaigns, category_ranking=None, category_summary=None):
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(campaigns),
        # Groq categorization breakdown (counts per category, low-confidence, 'other' shrink).
        "category_summary": category_summary,
        # Category-level ranking (highest first) on top of per-campaign scoring. None until
        # computed; [] when there's nothing rankable. Per-campaign records stay in "campaigns".
        "category_ranking": category_ranking,
        "campaigns": campaigns,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def _fmt_category(c):
    return _CATEGORY_LABELS.get(c.get("category"), c.get("category") or "Other")


def _disqualifier_reasons(c):
    return [d.get("reason") for d in (c.get("disqualifiers") or [])]


def _core_known(c):
    """core-signals-known count for tie-breaking / display (from the composite breakdown)."""
    return (c.get("composite_breakdown") or {}).get("core_signals_known") or 0


def _fmt_clip_short(c):
    rc = c.get("repeatable_clippability") or {}
    s = rc.get("score")
    return "UNKNOWN" if s is None else f"{s:.2f} ({rc.get('confidence') or 'UNKNOWN'})"


def _fmt_earn_short(c):
    eepc = (c.get("composite_breakdown") or {}).get("expected_earnings_per_clip")
    return "UNKNOWN" if eepc is None else f"${eepc:,.2f}"


def _fmt_language(c):
    """Detected language + whether the non-English derank fired (auditable, per campaign)."""
    lang = c.get("language") or {}
    code = lang.get("language") or "unknown"
    if lang.get("nonenglish"):
        fac = (c.get("composite_breakdown") or {}).get("language_factor")
        conf = lang.get("confidence") or "?"
        basis = lang.get("basis") or ""
        return f"{code} — NON-ENGLISH derank x{fac} ({conf}; {basis})"
    return f"{code} (English/ambiguous — no penalty)"


def _fmt_budget_short(c):
    rem = c.get("budget_remaining_fraction")
    total = c.get("budget_total")
    if rem is None:
        return "n/a"
    if total is not None:
        return f"${rem * total:,.0f} ({rem * 100:.0f}%)"
    return f"{rem * 100:.0f}%"


def _fmt_liveness(c):
    """Footage link-liveness summary line (status + the derank when it fired)."""
    live = c.get("liveness") or {}
    status = live.get("status")
    if not status or status == "no_links":
        return "n/a (no footage links)"
    reason = (live.get("reason") or "").strip()
    if live.get("penalized"):
        fac = (c.get("composite_breakdown") or {}).get("liveness_factor")
        fac_txt = f" — DEAD, composite x{fac}" if fac is not None else " — DEAD"
        return f"{status}{fac_txt} ({reason})"
    return f"{status} ({reason})" if reason else status


def _fmt_footage_presence(c):
    """Footage-presence summary line: does a PUBLIC downloadable footage link exist at all?
    'Footage: 0 public links -> NO-FOOTAGE x0.15' vs 'Footage: 2 public links'."""
    if c.get("self_sourced"):
        fac = (c.get("composite_breakdown") or {}).get("self_sourced_factor")
        fac_txt = f" → ×{fac}" if fac is not None else ""
        ph = c.get("self_sourced_phrase")
        ph_txt = f" [\"{ph}\"]" if ph else ""
        return f"SELF-SOURCED (no footage provided){fac_txt}{ph_txt}"
    fp = c.get("footage_presence") or {}
    has = fp.get("has_public_footage")
    n = fp.get("footage_link_count")
    if has is None:
        return "presence undeterminable (detail not loaded — fail-open, no penalty)"
    if has is False:
        fac = (c.get("composite_breakdown") or {}).get("footage_presence_factor")
        fac_txt = f" → NO-FOOTAGE x{fac}" if fac is not None else " → NO-FOOTAGE"
        return f"0 public links{fac_txt} ({fp.get('reason')})"
    return f"{n} public link{'s' if n != 1 else ''}"


def _fmt_payout(c):
    """Payout-health summary line: submissions / tracked-age / paid-out + the verdict. Shows
    the numbers the judgment rests on ('52 subs / 20d / $0 paid -> DEAD-PAYOUT x0.2')."""
    ph = c.get("payout_health") or {}
    status = ph.get("status")
    if not status or status in ("unknown", "disabled"):
        return f"n/a ({ph.get('reason') or 'not judged'})"
    subs = ph.get("submissions")
    days = ph.get("days_open")
    paid = ph.get("paid_out")
    subs_txt = f"{int(subs)} subs" if isinstance(subs, (int, float)) else "? subs"
    days_txt = f"{days:.0f}d" if isinstance(days, (int, float)) else "?d"
    paid_txt = f"${paid:,.0f} paid" if isinstance(paid, (int, float)) else "$? paid"
    head = f"{subs_txt} / {days_txt} / {paid_txt}"
    if status == "dead":
        fac = (c.get("composite_breakdown") or {}).get("payout_factor")
        return f"{head} → DEAD-PAYOUT x{fac} ({ph.get('reason')})"
    if status == "healthy":
        return f"{head} → healthy ({ph.get('reason')})"
    return f"{head} → {status} ({ph.get('reason')})"


def _fmt_approval(c):
    """Approval-rate line — ALWAYS shown. The % of submissions a campaign approves/pays:
    'approval: 88%' / 'approval: 22% → LOW x0.2' / 'approval: UNKNOWN'."""
    ar = c.get("approval_rate")
    if not isinstance(ar, (int, float)) or isinstance(ar, bool):
        return "UNKNOWN (not shown/parsed — fail-open, no penalty)"
    fac = (c.get("composite_breakdown") or {}).get("approval_rate_factor")
    if fac is not None and fac < 1.0:
        return f"{ar:.0f}% → LOW x{fac}"
    return f"{ar:.0f}%"


def _warning_flags(c):
    """Only real warnings — nothing neutral. Disqualifiers live in their own section."""
    b = c.get("composite_breakdown") or {}
    flags = []
    lang = c.get("language") or {}
    if lang.get("nonenglish"):
        fac = b.get("language_factor")
        fac_txt = f" (composite x{fac})" if fac is not None else ""
        flags.append(f"NON-ENGLISH [{lang.get('language')}]{fac_txt}")
    if b.get("expected_below_minimum"):
        flags.append("BELOW-MIN-PAYOUT (typical clip earns $0)")
    if c.get("high_minimum"):
        flags.append("HIGH_MINIMUM")
    mvt = c.get("min_view_threshold")
    if mvt:
        flags.append(f"MIN-VIEW-THRESHOLD (needs {mvt:,} views before ANY payout)")
    if c.get("capture_suspect"):
        reason = c.get("capture_suspect_reason") or "doc/resource"
        flags.append(f"CAPTURE-SUSPECT (modal references '{reason}' but no resource link "
                     f"captured — a doc may have been missed)")
    ct = c.get("content_type") or {}
    if ct.get("type") and ct.get("standard") is False:
        flags.append(f"non-standard footage ({ct['type']})")
    if (c.get("footage_access") or {}).get("status") == "partial":
        flags.append("footage partially inaccessible")
    if (c.get("liveness") or {}).get("penalized"):
        fac = b.get("liveness_factor")
        fac_txt = f" (composite x{fac})" if fac is not None else ""
        flags.append(f"DEAD-FOOTAGE{fac_txt}")
    if (c.get("footage_presence") or {}).get("has_public_footage") is False:
        fac = b.get("footage_presence_factor")
        fac_txt = f" (composite x{fac})" if fac is not None else ""
        flags.append(f"NO-FOOTAGE (no public footage link){fac_txt}")
    if (c.get("payout_health") or {}).get("status") == "dead":
        ph = c.get("payout_health") or {}
        fac = b.get("payout_factor")
        fac_txt = f" (composite x{fac})" if fac is not None else ""
        subs = ph.get("submissions")
        subs_txt = f"{int(subs)} subs, " if isinstance(subs, (int, float)) else ""
        flags.append(f"DEAD-PAYOUT ({subs_txt}~$0 paid){fac_txt}")
    ar = c.get("approval_rate")
    if isinstance(ar, (int, float)) and not isinstance(ar, bool) \
            and (b.get("approval_rate_factor") or 1.0) < 1.0:
        fac = b.get("approval_rate_factor")
        fac_txt = f" (composite x{fac})" if fac is not None else ""
        flags.append(f"LOW-APPROVAL ({ar:.0f}% approved){fac_txt}")
    if c.get("self_sourced"):
        fac = b.get("self_sourced_factor")
        fac_txt = f" (composite x{fac})" if fac is not None else ""
        ph = c.get("self_sourced_phrase")
        ph_txt = f": \"{ph}\"" if ph else ""
        flags.append(f"SELF-SOURCED (no footage provided){fac_txt}{ph_txt}")
    if c.get("dedicated_page_required"):
        fac = b.get("dedicated_page_factor")
        fac_txt = f" (composite x{fac})" if fac is not None else ""
        ph = c.get("dedicated_page_phrase")
        ph_txt = f": \"{ph}\"" if ph else ""
        flags.append(f"DEDICATED-PAGE required{fac_txt}{ph_txt}")
    if c.get("rules_incomplete"):
        ph = c.get("rules_incomplete_phrase")
        ph_txt = f": \"{ph}\"" if ph else ""
        flags.append(f"RULES-INCOMPLETE (full rules gated behind joining — captured rules "
                     f"are partial){ph_txt}")
    return flags


def _short_rules(c, limit=180):
    t = " ".join((c.get("rules_text") or "").split())
    if not t:
        return "(none captured)"
    return t if len(t) <= limit else t[:limit].rstrip() + "…"


def _campaign_block(c, rank):
    """Clean, scannable per-campaign block. Only the facts that matter for a click decision;
    the full factor breakdown lives in campaigns.json, not here."""
    name = c.get("name") or "(unnamed campaign)"
    pay = c.get("pay_per_1k")
    pay_txt = f"${pay:.2f}/1k" if pay is not None else "n/a"

    lines = [f"## {rank}. {name} — {_fmt_category(c)}"]
    lines.append(c.get("url") or "URL not captured")
    lines.append(f"Pay: {pay_txt} · Budget remaining: {_fmt_budget_short(c)}")
    lines.append(f"Expected earnings/clip: {_fmt_earn_short(c)}")
    lines.append(f"Clippability: {_fmt_clip_short(c)}")
    lines.append(f"Footage: {_fmt_footage_presence(c)}")
    lines.append(f"Footage liveness: {_fmt_liveness(c)}")
    lines.append(f"Payout: {_fmt_payout(c)}")
    lines.append(f"Approval: {_fmt_approval(c)}")
    lines.append(f"Data confidence: {_core_known(c)}/5 core signals known")
    lines.append(f"Language: {_fmt_language(c)}")

    b = c.get("composite_breakdown") or {}
    sf = b.get("style_fit")
    if sf is not None:
        lines.append(f"Style fit: {sf:.2f} (x{b.get('style_fit_factor')}) "
                     f"[{b.get('style_fit_basis')}]")

    open_to_all = c.get("open_to_all") or "unclear"
    if open_to_all == "yes":
        lines.append("Open to all: yes (instant join)")
    else:
        lines.append("Open to all: unclear (couldn't confirm instant join)")

    rec = c.get("recurring_creator") or {}
    if (rec.get("previous_count") or 0) > 0:
        lines.append(f"Recurring creator: {rec['previous_count']} prior campaign(s)")

    flags = _warning_flags(c)
    if flags:
        lines.append(f"Flags: {', '.join(flags)}")

    lines.append(f"Rules: {_short_rules(c)}")
    lines.append(f"id: {c.get('id') or 'n/a'}")
    lines.append("")
    return lines


def _campaign_oneliner(c, rank):
    """Compact one-line entry for the per-category top-5 lists."""
    name = c.get("name") or "(unnamed)"
    earn = _fmt_earn_short(c)
    earn_txt = "$/clip UNKNOWN" if earn == "UNKNOWN" else f"{earn}/clip"
    return (f"{rank}. {name} — {earn_txt} · clip {_fmt_clip_short(c)} · "
            f"{_core_known(c)}/5 known")


# --- data coverage -------------------------------------------------------------
# Each ranking signal has THREE predicates:
#   known      — did we measure a REAL value (vs falling back to a neutral default)?
#   achievable — is the prerequisite even PRESENT for this campaign? (a resolvable handle
#                for reach, a source link for footage, a creator name to search for clips)
# Coverage is measured as known / ACHIEVABLE, not known / total — because several signals
# are structurally unavailable pre-join (the brief just doesn't expose the data). Comparing
# against 100% would make a run that's working as well as the data allows look broken. The
# `ceiling` (achievable / total) is shown alongside so the structural limit is explicit.
def _known_reach(c):
    src = c.get("source") or {}
    return src.get("recent_avg_views") is not None or src.get("reach_estimate") is not None

def _has_resolvable_handle(c):
    return any(h.get("url") for h in (c.get("source") or {}).get("handles") or [])

def _has_links(c):
    return bool(c.get("source_links"))

def _has_creator_name(c):
    return bool((c.get("source") or {}).get("name") or c.get("creator"))

def _has_clipper_median(c):
    return (c.get("repeatable_clippability") or {}).get("median_views") is not None

# (label, known_pred, achievable_pred, ceiling_note)
_COVERAGE_SIGNALS = [
    ("Creator reach", _known_reach, _has_resolvable_handle,
     "needs a resolvable creator handle in the brief"),
    ("Footage accessibility",
     lambda c: (c.get("footage_access") or {}).get("status") in ("ok", "partial", "none"),
     _has_links, "needs a source/footage link"),
    ("Footage substance (content type)",
     lambda c: (c.get("content_type") or {}).get("type") is not None,
     _has_links, "needs a source/footage link"),
    ("Footage volume / refresh",
     lambda c: (c.get("footage_volume") or {}).get("recurring") is not None,
     _has_links, "needs a source/footage link"),
    ("Action/entertainment density",
     lambda c: (c.get("action_density") or {}).get("score") is not None,
     _has_links, "needs a source link WITH readable subtitles"),
    ("Repeatable clippability",
     lambda c: (c.get("repeatable_clippability") or {}).get("score") is not None,
     _has_creator_name, "needs a creator name to search clipper accounts"),
    ("Expected earnings/clip",
     lambda c: (c.get("composite_breakdown") or {}).get("expected_earnings_per_clip") is not None,
     _has_clipper_median, "derived from proven clipper median × rate"),
]


def _coverage_rows(active):
    """[(label, known, achievable, total, note)] per ranking signal over ranked campaigns."""
    total = len(active)
    rows = []
    for label, known_pred, ach_pred, note in _COVERAGE_SIGNALS:
        known = ach = 0
        for c in active:
            try:
                a = bool(ach_pred(c))
            except Exception:
                a = False
            if a:
                ach += 1
            try:
                if a and known_pred(c):
                    known += 1
            except Exception:
                pass
        rows.append((label, known, ach, total, note))
    return rows


def _coverage_achieved(rows):
    """(known_total, achievable_total, fraction) — how much of the ACHIEVABLE data we got.
    fraction is None when nothing is achievable (no denominator)."""
    kt = sum(k for _, k, _, _, _ in rows)
    at = sum(a for _, _, a, _, _ in rows)
    return kt, at, (kt / at if at else None)


def _coverage_md(active):
    """A prominent coverage block for the top of the summary — measured against ACHIEVABLE
    data so it reflects whether scout is working as well as the briefs allow, not vs 100%."""
    total = len(active)
    if total == 0:
        return ["## Data coverage", "", "_No ranked campaigns this run._", ""]
    rows = _coverage_rows(active)
    kt, at, achieved = _coverage_achieved(rows)
    # A genuine problem is when we FAIL to capture data that IS available: either <50% of
    # achievable overall, or a signal that captured 0 of a non-trivial available pool (a
    # working feature that stopped — e.g. the reach lookup regression). Low absolute
    # coverage with high achieved% is a data ceiling, not a bug.
    stalled = [label for label, k, a, _t, _n in rows if a >= 3 and k == 0]
    problem = (achieved is not None and achieved < 0.5) or bool(stalled)
    warn = "⚠️ " if problem else ""
    pct = lambda k, d: (f"{100 * k / d:.0f}%" if d else "n/a")

    def bar(k, d):
        if not d:
            return "─" * 10
        filled = round(10 * k / d)
        return "█" * filled + "·" * (10 - filled)

    achieved_txt = f"{achieved * 100:.0f}% of achievable" if achieved is not None else "n/a"
    out = [f"## {warn}Data coverage — {achieved_txt} captured ({total} ranked campaign(s))", ""]
    if stalled:
        out.append("> **⚠️ STALLED SIGNAL(S): " + ", ".join(stalled) + "** — captured 0 of a "
                   "pool that DOES expose the data. A working feature likely broke; investigate.")
        out.append("")
    elif achieved is not None and achieved < 0.5:
        out.append("> **⚠️ Capturing under half of the data that IS available — likely a bug, "
                   "not a ceiling.** Investigate before trusting these ranks.")
        out.append("")
    out.append("Coverage is **known / achievable** — measured against how many campaigns even "
               "expose each signal, because several are structurally unavailable pre-join (the "
               "brief simply doesn't include the link/handle). The **ceiling** column is how "
               "many briefs expose the prerequisite at all; a low ceiling is a data limit, not "
               "a scout failure. A high 'of achievable' means scout is working as well as the "
               "data allows.")
    out += ["", "| Signal | Known / achievable | Of achievable | Ceiling (has data) |",
            "| --- | ---: | :--- | ---: |"]
    for label, known, ach, tot, note in rows:
        out.append(f"| {label} | {known}/{ach} | `{bar(known, ach)}` {pct(known, ach)} | "
                   f"{ach}/{tot} ({pct(ach, tot)}) _{note}_ |")
    out.append("")
    return out


def write_summary_md(path, campaigns, category_ranking=None, category_summary=None):
    scored = [c for c in campaigns if c.get("status") in ("scraped", "refreshed")]
    # rules_unreadable campaigns (rules only in an unreadable Notion source) are EXCLUDED from
    # the ranked/active set — the clipper must never receive a campaign whose banned-words are
    # unknown — but kept and shown in their own section with the reason. Prohibited/vice
    # campaigns (gambling/betting/alcohol/vape/…) are EXCLUDED the same way — never handed to
    # the clipper — and take precedence over the other buckets so they show once, with reason.
    prohibited = [c for c in scored if c.get("excluded_prohibited")]
    unreadable = [c for c in scored if c.get("rules_unreadable")
                  and not c.get("disqualified") and not c.get("excluded_prohibited")]
    active = [c for c in scored if not c.get("disqualified")
              and not c.get("rules_unreadable") and not c.get("excluded_prohibited")]
    disqualified = [c for c in scored if c.get("disqualified")
                    and not c.get("excluded_prohibited")]
    # Rank by composite; ties break toward the better-UNDERSTOOD campaign (more core
    # signals known), then the crude pre_score.
    active.sort(key=lambda c: (_composite_of(c), _core_known(c), c.get("pre_score", 0)),
                reverse=True)
    skipped = [c for c in campaigns if c.get("status") == "skipped_prefilter"]
    completed = [c for c in campaigns if c.get("status") == "completed"]

    lines = ["# Scout — Content Rewards summary", "",
             f"Generated {datetime.now(timezone.utc).isoformat()}", ""]
    lines.append(
        f"{len(active)} rankable · {len(disqualified)} disqualified · "
        f"{len(prohibited)} prohibited · {len(unreadable)} rules-unreadable · "
        f"{len(skipped)} pre-filtered · {len(completed)} clipper-done.")
    noneng = sum(1 for c in active if (c.get("language") or {}).get("nonenglish"))
    if noneng:
        lines.append(f"{noneng} of the rankable campaigns detected NON-ENGLISH and deranked "
                     f"(composite heavily penalized, not excluded — see the Language line / "
                     f"NON-ENGLISH flag per campaign).")
    nofoot = sum(1 for c in active
                 if (c.get("footage_presence") or {}).get("has_public_footage") is False)
    if nofoot:
        lines.append(f"{nofoot} of the rankable campaigns expose NO public footage link and "
                     f"were deranked (footage member-gated or absent — can't be clipped from "
                     f"the auto-run; heavily penalized, not excluded — see the Footage line / "
                     f"NO-FOOTAGE flag per campaign).")
    lines.append("")
    lines.append("Sorted by composite rank (reach x rate drives it; expected $/clip and "
                 "proven clippability are the heavy levers). Campaigns ranking mostly on "
                 "UNKNOWNs are discounted, so a proven campaign outranks a mystery one — the "
                 "\"X/5 core signals known\" line shows how much real data each rank rests on. "
                 "Full factor breakdown per campaign is in campaigns.json.")
    lines.append("")

    # Data-coverage report — how much real signal underpins this ranking, up top so a
    # low-coverage rank is obvious at a glance (never something to dig for).
    lines.extend(_coverage_md(active))

    # Groq categorization breakdown, then the category-level ranking (highest first) — both
    # sit above the per-campaign list.
    lines.extend(_category_breakdown_md(category_summary))
    lines.extend(_category_ranking_md(category_ranking))

    lines.append("## Ranked campaigns")
    lines.append("")
    if not active:
        lines.append("(none)")
        lines.append("")
    for i, c in enumerate(active, 1):
        lines.extend(_campaign_block(c, i))

    # --- per-category top-5 -----------------------------------------------------
    lines.append("## Best per category (top 5 each)")
    lines.append("")
    by_cat = {}
    for c in active:
        by_cat.setdefault(c.get("category") or "other", []).append(c)
    for cat in _CATEGORY_ORDER:
        group = by_cat.get(cat)
        if not group:
            continue
        lines.append(f"### {_CATEGORY_LABELS[cat]} ({len(group)})")
        for i, c in enumerate(group[:5], 1):
            lines.append(_campaign_oneliner(c, i))
        lines.append("")

    # --- disqualified -----------------------------------------------------------
    if disqualified:
        disqualified.sort(key=lambda c: c.get("name") or "")
        lines.append("## Disqualified (shown, not ranked)")
        lines.append("")
        for c in disqualified:
            name = c.get("name") or "(unnamed)"
            reasons = "; ".join(_disqualifier_reasons(c)) or "disqualified"
            lines.append(f"- {name} — {reasons}")
        lines.append("")

    # --- excluded: rules unreadable ---------------------------------------------
    if unreadable:
        unreadable.sort(key=lambda c: c.get("name") or "")
        lines.append("## Excluded — rules unreadable (NOT ranked, NOT handed to the clipper)")
        lines.append("")
        lines.append("Rules live ONLY in a source scout/intake can't read (a Notion page that "
                     "didn't fetch). Clipping without the known banned-word list is a compliance "
                     "risk, so these are held out of the ranking. Fix by adding an on-page / "
                     "Google-Doc rules source, or verify the page is public.")
        lines.append("")
        for c in unreadable:
            name = c.get("name") or "(unnamed)"
            lines.append(f"- {name} — {c.get('rules_unreadable_reason') or 'rules unreadable'}")
        lines.append("")

    # --- excluded: prohibited category ------------------------------------------
    if prohibited:
        # clear DQs first, borderline (ambiguous-keyword-only) last, then by name.
        prohibited.sort(key=lambda c: (bool(c.get("excluded_prohibited_borderline")),
                                       c.get("name") or ""))
        n_border = sum(1 for c in prohibited if c.get("excluded_prohibited_borderline"))
        lines.append("## Excluded — prohibited category (NOT ranked, NOT handed to the clipper)")
        lines.append("")
        lines.append(f"{len(prohibited)} auto-excluded ({n_border} borderline). Betting/gambling/"
                     "casino/sportsbook, alcohol/drinking, vape and similar vice categories are "
                     "held out of the ranking. **BORDERLINE** rows matched only an ambiguous "
                     "keyword (bet/stake/odds/drink…) — review them for a false positive.")
        lines.append("")
        for c in prohibited:
            name = c.get("name") or "(unnamed)"
            tag = " **[BORDERLINE — review]**" if c.get("excluded_prohibited_borderline") else ""
            reason = c.get("excluded_prohibited_reason") or "prohibited category"
            lines.append(f"- {name} — {reason}{tag}")
        lines.append("")

    # --- skipped by pre-filter --------------------------------------------------
    if skipped:
        lines.append("## Skipped by pre-filter")
        lines.append("")
        for c in skipped:
            name = c.get("name") or "(unnamed)"
            lines.append(f"- {name} — {c.get('skip_reason') or 'filtered'}")
        lines.append("")

    # --- clipper-done (DONE list) -----------------------------------------------
    if completed:
        completed.sort(key=lambda c: c.get("name") or "")
        lines.append("## Clipper-done (exhausted — not scraped or ranked)")
        lines.append("")
        lines.append("On the DONE list (completed_campaigns.json). "
                     "Restore with: python scout.py --unmark-done <id>")
        lines.append("")
        for c in completed:
            name = c.get("name") or "(unnamed)"
            lines.append(f"- {name} — {c.get('id') or 'n/a'}")
        lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def terminal_report(campaigns, *, db_total, new_count, failures, category_ranking=None,
                    category_summary=None):
    scored = [c for c in campaigns if c.get("status") in ("scraped", "refreshed")]
    prohibited = [c for c in scored if c.get("excluded_prohibited")]
    unreadable = [c for c in scored if c.get("rules_unreadable")
                  and not c.get("disqualified") and not c.get("excluded_prohibited")]
    active = [c for c in scored if not c.get("disqualified")
              and not c.get("rules_unreadable") and not c.get("excluded_prohibited")]
    disqualified = [c for c in scored if c.get("disqualified")
                    and not c.get("excluded_prohibited")]
    newly_scraped = sum(1 for c in scored if c.get("status") == "scraped")
    refreshed = sum(1 for c in scored if c.get("status") == "refreshed")
    completed = sum(1 for c in campaigns if c.get("status") == "completed")
    active.sort(key=lambda c: (_composite_of(c), _core_known(c), c.get("pre_score", 0)),
                reverse=True)
    high_min = sum(1 for c in active if c.get("high_minimum"))
    noneng = sum(1 for c in active if (c.get("language") or {}).get("nonenglish"))
    min_view_gated = sum(1 for c in active if c.get("min_view_threshold"))
    dead_payout = sum(1 for c in active if (c.get("payout_health") or {}).get("status") == "dead")
    low_approval = sum(1 for c in active
                       if ((c.get("composite_breakdown") or {}).get("approval_rate_factor")
                           or 1.0) < 1.0)
    self_sourced = sum(1 for c in active if c.get("self_sourced"))
    capture_suspect = sum(1 for c in active if c.get("capture_suspect"))
    clip_unk = sum(1 for c in active
                   if (c.get("repeatable_clippability") or {}).get("score") is None)

    print("")
    print("=" * 72)
    print("SCOUT — run complete")
    print("=" * 72)
    print(f"  Rankable campaigns this run          : {len(active)}")
    print(f"  Disqualified (sunk, still shown)     : {len(disqualified)}")
    print(f"  Excluded — rules unreadable (Notion) : {len(unreadable)}")
    n_border = sum(1 for c in prohibited if c.get("excluded_prohibited_borderline"))
    print(f"  Excluded — prohibited category       : {len(prohibited)} ({n_border} borderline)")
    print(f"  Total campaigns in DB                : {db_total}")
    print(f"  Scraped this run (unreached)         : {newly_scraped} ({new_count} never-seen)")
    print(f"  Known, refreshed no re-scrape        : {refreshed}")
    print(f"  Clipper-done (DONE list, excluded)   : {completed}")
    print(f"  Flagged HIGH_MINIMUM                 : {high_min}")
    print(f"  Non-English (deranked, not excluded) : {noneng}")
    print(f"  Min-VIEW payout gate (penalized)     : {min_view_gated}")
    print(f"  DEAD-PAYOUT (active but ~$0 paid)    : {dead_payout}")
    print(f"  LOW-APPROVAL (< floor, deranked)     : {low_approval}")
    print(f"  SELF-SOURCED footage (deranked)      : {self_sourced}")
    print(f"  Capture-suspect (doc maybe missed)   : {capture_suspect}")
    print(f"  Clippability UNKNOWN                 : {clip_unk}")
    print(f"  Failures (see errors.log)            : {failures}")
    print("")
    rows = _coverage_rows(active)
    kt, at, achieved = _coverage_achieved(rows)
    if active:
        stalled = [label for label, k, a, _t, _n in rows if a >= 3 and k == 0]
        low = achieved is not None and achieved < 0.5
        flag = ("  ⚠ STALLED: " + ", ".join(stalled) if stalled
                else "  ⚠ under half of AVAILABLE data — likely a bug" if low else "")
        ach_txt = f"{achieved * 100:.0f}% of achievable" if achieved is not None else "n/a"
        print(f"  --- data coverage: {ach_txt} captured ({len(active)} ranked){flag} ---")
        print(f"      {'signal':<34}  known/achiev  ceiling(has-data)")
        for label, known, ach, tot, _note in rows:
            ap = f"{100 * known / ach:.0f}%" if ach else "n/a"
            cp = f"{100 * ach / tot:.0f}%" if tot else "n/a"
            print(f"    {label:<34}: {known}/{ach} ({ap})   {ach}/{tot} ({cp})")
        print("")
    print("  Top 10 by composite (comp | $/clip | clip | data | appr | category):")
    if not active:
        print("    (none)")
    for i, c in enumerate(active[:10], 1):
        name = (c.get("name") or "(unnamed)")[:26]
        eepc = (c.get("composite_breakdown") or {}).get("expected_earnings_per_clip")
        earn_txt = f"${eepc:,.0f}" if eepc is not None else "UNK"
        rc = c.get("repeatable_clippability") or {}
        rcs = rc.get("score")
        rep = f"{rcs:.2f}" if rcs is not None else "UNK"
        data = f"{_core_known(c)}/5"
        ar = c.get("approval_rate")
        low = ((c.get("composite_breakdown") or {}).get("approval_rate_factor") or 1.0) < 1.0
        appr_txt = (f"{ar:.0f}%{'!' if low else ''}"
                    if isinstance(ar, (int, float)) and not isinstance(ar, bool) else "UNK")
        cat = (c.get("category") or "other")[:10]
        print(f"    {i:>2}. {_composite_of(c):7.3f}  {name:<26}  {earn_txt:>6}  "
              f"{rep:>5}  {data:>4}  {appr_txt:>5}  {cat}")
    if category_summary:
        cs = category_summary
        srcs = cs.get("sources") or {}
        base = cs.get("other_keyword_baseline")
        base_txt = (f" (keyword baseline {base})" if base is not None else "")
        print("")
        print(f"  Categorization (Groq): {cs.get('total', 0)} campaigns · "
              f"groq {srcs.get('groq', 0)}/cache {srcs.get('cache', 0)}/"
              f"kw {srcs.get('keyword_fallback', 0)} · {cs.get('low_confidence', 0)} low-conf · "
              f"'other'={cs.get('other_now', 0)}{base_txt}")
    if category_ranking:
        agg = category_ranking[0].get("agg", "top5")
        print("")
        print(f"  Category ranking ({agg} of members' composites; THIN = <5 campaigns):")
        for i, r in enumerate(category_ranking, 1):
            thin = " [THIN]" if r.get("thin") else ""
            print(f"    {i:>2}. {r.get('score'):7.3f}  {_cat_label(r.get('category')):<16}  "
                  f"{r.get('count')} campaign(s){thin}")
    print("=" * 72)
