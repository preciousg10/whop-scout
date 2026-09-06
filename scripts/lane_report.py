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
import re
import sys
from pathlib import Path

# UTF-8 console (Windows defaults to cp1252 and crashes on emoji/₹ campaign names)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

CAMPAIGNS_JSON = Path(r"C:\whop\scout\campaigns.json")

# Lane -> keyword phrases. Matched as whole-word/phrase substrings against a
# campaign's name + category + rules text. A campaign is assigned to EVERY lane
# whose keywords appear (multi-assign).
LANES = {
    "HEALTH": [
        "sleep", "supplement", "nutrition", "diet", "wellness", "vitamin",
        "medical", "doctor", "hormone", "peptide", "longevity", "skin",
        "derma", "mental health", "therapy", "meditation",
    ],
    "FITNESS": [
        "gym", "workout", "fitness", "muscle", "bodybuilding", "weight loss",
        "protein", "training", "calisthenics", "athletic performance",
    ],
    "MONEY": [
        "crypto", "bitcoin", "trading", "investing", "finance", "business",
        "entrepreneur", "hustle", "sales", "ecommerce", "wealth", "startup",
    ],
    "MOTIVATION_MINDSET": [
        "motivation", "mindset", "discipline", "masculinity",
        "self improvement", "self-improvement", "success", "stoic",
        "confidence", "dating", "red pill", "redpill",
    ],
    "GAMING": [
        "game", "gaming", "fortnite", "minecraft", "cod", "warzone",
        "valorant", "roblox", "esports", "gameplay",
    ],
    "ENTERTAINMENT_STREAMER": [
        "streamer", "reaction", "drama", "celebrity", "kai cenat",
        "ishowspeed", "twitch", "irl", "viral moments", "creator", "podcast",
    ],
    "SPORTS": [
        "nba", "nfl", "soccer", "football", "ufc", "mma", "boxing", "f1",
        "sports", "athlete", "highlights", "basketball",
    ],
    "MUSIC": [
        "song", "edit", "lyric", "rave", "edm", "rapper", "artist", "album",
        "remix",
    ],
    "COMEDY_MEMES": ["meme", "funny", "comedy", "humor", "skit"],
    "NEWS_POLITICS": [
        "news", "politics", "election", "current events", "commentary",
    ],
    "FAITH": [
        "god", "faith", "christian", "islam", "bible", "spiritual", "religion",
    ],
}


def campaign_text(rec):
    """Blob of name + category + rules text, lowercased, for keyword matching."""
    parts = [
        rec.get("name") or "",
        rec.get("category") or "",
        " ".join(rec.get("categories") or []),
        rec.get("rules_text") or "",
        rec.get("modal_rules_text") or "",
        rec.get("modal_requirements_text") or "",
    ]
    return " ".join(parts).lower()


def match_lanes(rec):
    """Return the sorted list of lanes this campaign fits (may be empty)."""
    text = campaign_text(rec)
    hits = []
    for lane, keywords in LANES.items():
        for kw in keywords:
            # whole-word / phrase boundary match so "cod" doesn't hit "code"
            if re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", text):
                hits.append(lane)
                break
    return hits


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
