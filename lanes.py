"""AUDIENCE-LANE tagging — the canonical lane definitions + matcher.

A LANE is a TOPIC/AUDIENCE someone follows (health, gaming, sports, …) — the real
grouping for POSTING, as opposed to a scout `category` which is a FORMAT (podcast,
brand_product, …). A single campaign can belong to SEVERAL lanes at once (multi-assign).

This module is the ONE place the lane keywords + matching live. Both
`scripts/lane_report.py` (the read-only analysis report this logic was first written in)
and scout's `enrich_active` (which persists `lanes` on every campaign record) import from
here, so the tags scout stores are IDENTICAL to what the report has always shown.

Pure / offline — no network, no Playwright, no Groq. Testable directly:
    import lanes
    lanes.match_lanes(rec)      # -> ["GAMING", "ENTERTAINMENT_STREAMER"]  (may be empty)
    lanes.campaign_lanes(rec)   # -> same, but [] collapses to ["OTHER"]
"""

import re

# Lane -> keyword phrases. Matched as whole-word/phrase substrings against a campaign's
# name + category + categories + rules text. A campaign is assigned to EVERY lane whose
# keywords appear (multi-assign). Edit here to re-profile every consumer at once.
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

# The sentinel lane for a campaign that matched none of the above.
OTHER = "OTHER"

# Every lane name a caller may filter on (the real lanes + OTHER).
LANE_NAMES = tuple(LANES.keys()) + (OTHER,)


def campaign_text(rec):
    """Blob of name + category + categories + rules text, lowercased, for keyword matching."""
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
    """Return the list of lanes this campaign fits (may be EMPTY — no OTHER added here).

    Order follows LANES declaration order so the result is deterministic."""
    text = campaign_text(rec)
    hits = []
    for lane, keywords in LANES.items():
        for kw in keywords:
            # whole-word / phrase boundary match so "cod" doesn't hit "code"
            if re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", text):
                hits.append(lane)
                break
    return hits


def campaign_lanes(rec):
    """Lanes for a record, with the OTHER fallback applied — the value scout PERSISTS as
    `rec["lanes"]`. Always non-empty: a campaign matching no lane returns ["OTHER"]."""
    return match_lanes(rec) or [OTHER]
