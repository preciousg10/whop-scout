#!/usr/bin/env python3
"""One-time, READ-ONLY audience-lane analysis over campaigns.json.

Tags EVERY campaign with ALL audience-lanes it matches (multi-assign — a
campaign can belong to several lanes at once), then prints:
  1. a summary table (lane -> total, lane -> clippable), sorted by clippable
  2. the campaign names under each lane
  3. the campaigns that matched NO lane (OTHER)

A lane is a TOPIC/AUDIENCE someone follows (not a format like "podcast").
"Clippable" = English AND has at least one footage link.

Does NOT modify campaigns.json or any pipeline code — pure read + print.

    python scripts/lane_report.py
"""

import json
import sys
from pathlib import Path

# UTF-8 console (Windows defaults to cp1252 and crashes on emoji/₹ campaign names)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# The lane keywords + matcher live in scout's canonical `lanes` module (scout also
# persists `lanes` on each campaign from the SAME logic). Import them so this report and
# scout's stored tags never drift apart.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lanes import LANES, campaign_text, match_lanes  # noqa: E402

CAMPAIGNS_JSON = Path(r"C:\whop\scout\campaigns.json")


def is_english(rec):
    """Fail-open English check (matches pipeline: only CLEARLY non-English is False)."""
    lang = rec.get("language") or {}
    if lang.get("nonenglish") is True:
        return False
    if rec.get("non_english") is True:
        return False
    return True


def has_footage(rec):
    """At least one footage link (source_links is the footage-only field)."""
    return bool(rec.get("source_links"))


def is_clippable(rec):
    return is_english(rec) and has_footage(rec)


def main():
    if not CAMPAIGNS_JSON.exists():
        sys.exit(f"campaigns.json not found at {CAMPAIGNS_JSON}")

    data = json.loads(CAMPAIGNS_JSON.read_text(encoding="utf-8"))
    campaigns = data.get("campaigns", [])

    lane_members = {lane: [] for lane in LANES}
    lane_members["OTHER"] = []

    for rec in campaigns:
        lanes = match_lanes(rec)
        if not lanes:
            lane_members["OTHER"].append(rec)
        else:
            for lane in lanes:
                lane_members[lane].append(rec)

    # ---- 1. summary table -------------------------------------------------
    rows = []
    for lane, members in lane_members.items():
        total = len(members)
        clippable = sum(1 for r in members if is_clippable(r))
        rows.append((lane, total, clippable))
    # sort by clippable desc, then total desc; keep OTHER visible at its rank
    rows.sort(key=lambda r: (r[2], r[1]), reverse=True)

    print("=" * 60)
    print(f"AUDIENCE-LANE REPORT  ({len(campaigns)} campaigns, multi-assign)")
    print("Clippable = English AND has a footage link")
    print("Counts OVERLAP across lanes (a campaign can be in several)")
    print("=" * 60)
    print(f"{'LANE':<24}{'TOTAL':>8}{'CLIPPABLE':>12}")
    print("-" * 44)
    for lane, total, clippable in rows:
        print(f"{lane:<24}{total:>8}{clippable:>12}")
    print("-" * 44)

    # ---- 2. names under each lane ----------------------------------------
    for lane, total, clippable in rows:
        if lane == "OTHER":
            continue
        members = lane_members[lane]
        print()
        print(f"### {lane}  (total {total}, clippable {clippable})")
        for r in sorted(members, key=lambda x: (x.get("name") or "").lower()):
            tags = []
            if not is_english(r):
                tags.append("non-EN")
            if not has_footage(r):
                tags.append("no-footage")
            suffix = f"   [{', '.join(tags)}]" if tags else "   [clippable]"
            print(f"  - {r.get('name') or '(unnamed)'}{suffix}")

    # ---- 3. OTHER (uncovered) --------------------------------------------
    other = lane_members["OTHER"]
    print()
    print("=" * 60)
    print(f"OTHER — matched NO lane ({len(other)} campaigns, uncovered)")
    print("=" * 60)
    for r in sorted(other, key=lambda x: (x.get("name") or "").lower()):
        cat = r.get("category") or "?"
        print(f"  - {r.get('name') or '(unnamed)'}  (category={cat})")


if __name__ == "__main__":
    main()
