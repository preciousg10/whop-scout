"""Outputs: campaigns.json, campaigns_summary.md, and the terminal final report."""
import json
from datetime import datetime, timezone

import scoring

_CATEGORY_LABELS = {
    "streamer_irl": "Streamer/IRL", "gaming": "Gaming", "sports": "Sports",
    "podcast_talking": "Podcast/Talking", "brand_product": "Brand/Product",
    "music": "Music", "meme": "Meme", "other": "Other",
}
_CATEGORY_ORDER = list(_CATEGORY_LABELS.keys())


def _composite_of(c):
    return c.get("composite_score") or 0


def write_json(path, campaigns):
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(campaigns),
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


def _fmt_budget_short(c):
    rem = c.get("budget_remaining_fraction")
    total = c.get("budget_total")
    if rem is None:
        return "n/a"
    if total is not None:
        return f"${rem * total:,.0f} ({rem * 100:.0f}%)"
    return f"{rem * 100:.0f}%"


def _warning_flags(c):
    """Only real warnings — nothing neutral. Disqualifiers live in their own section."""
    b = c.get("composite_breakdown") or {}
    flags = []
    if b.get("expected_below_minimum"):
        flags.append("BELOW-MIN-PAYOUT (typical clip earns $0)")
    if c.get("high_minimum"):
        flags.append("HIGH_MINIMUM")
    ct = c.get("content_type") or {}
    if ct.get("type") and ct.get("standard") is False:
        flags.append(f"non-standard footage ({ct['type']})")
    if (c.get("footage_access") or {}).get("status") == "partial":
        flags.append("footage partially inaccessible")
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
    lines.append(f"Data confidence: {_core_known(c)}/5 core signals known")

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


def write_summary_md(path, campaigns):
    scored = [c for c in campaigns if c.get("status") in ("scraped", "refreshed")]
    # rules_unreadable campaigns (rules only in an unreadable Notion source) are EXCLUDED from
    # the ranked/active set — the clipper must never receive a campaign whose banned-words are
    # unknown — but kept and shown in their own section with the reason.
    unreadable = [c for c in scored if c.get("rules_unreadable") and not c.get("disqualified")]
    active = [c for c in scored if not c.get("disqualified") and not c.get("rules_unreadable")]
    disqualified = [c for c in scored if c.get("disqualified")]
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
        f"{len(unreadable)} rules-unreadable · {len(skipped)} pre-filtered · "
        f"{len(completed)} clipper-done.")
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


def terminal_report(campaigns, *, db_total, new_count, failures):
    scored = [c for c in campaigns if c.get("status") in ("scraped", "refreshed")]
    unreadable = [c for c in scored if c.get("rules_unreadable") and not c.get("disqualified")]
    active = [c for c in scored if not c.get("disqualified") and not c.get("rules_unreadable")]
    disqualified = [c for c in scored if c.get("disqualified")]
    newly_scraped = sum(1 for c in scored if c.get("status") == "scraped")
    refreshed = sum(1 for c in scored if c.get("status") == "refreshed")
    completed = sum(1 for c in campaigns if c.get("status") == "completed")
    active.sort(key=lambda c: (_composite_of(c), _core_known(c), c.get("pre_score", 0)),
                reverse=True)
    high_min = sum(1 for c in active if c.get("high_minimum"))
    clip_unk = sum(1 for c in active
                   if (c.get("repeatable_clippability") or {}).get("score") is None)

    print("")
    print("=" * 72)
    print("SCOUT — run complete")
    print("=" * 72)
    print(f"  Rankable campaigns this run          : {len(active)}")
    print(f"  Disqualified (sunk, still shown)     : {len(disqualified)}")
    print(f"  Excluded — rules unreadable (Notion) : {len(unreadable)}")
    print(f"  Total campaigns in DB                : {db_total}")
    print(f"  Scraped this run (unreached)         : {newly_scraped} ({new_count} never-seen)")
    print(f"  Known, refreshed no re-scrape        : {refreshed}")
    print(f"  Clipper-done (DONE list, excluded)   : {completed}")
    print(f"  Flagged HIGH_MINIMUM                 : {high_min}")
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
    print("  Top 10 by composite (comp | $/clip | clip | data | category):")
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
        cat = (c.get("category") or "other")[:10]
        print(f"    {i:>2}. {_composite_of(c):7.3f}  {name:<26}  {earn_txt:>6}  "
              f"{rep:>5}  {data:>4}  {cat}")
    print("=" * 72)
