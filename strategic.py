"""Cross-run + strategic signals, computed on the run history scout already accumulates
(per-run snapshots in campaigns.json) — NO new scraping. Fills five signals per active
campaign and one cross-campaign aggregate:

  1. budget_drain        — projected days-until-empty (scoring.project_budget_drain)
  2. participant_growth  — growth rate + direction  (scoring.participant_growth)
  3. source_saturation   — reuses the proven_clips clipper search already run
  4. recurring_creator   — how many campaigns this creator/brand has run over time,
                           counted across ALL records in campaigns.json (cross-run)
  5. account_reusability — can this feed an existing category account, or need a new one

Everything degrades to UNKNOWN/neutral when the history is too thin — never fabricated.
The 6th signal (personalized performance learning) is a documented stub in scoring.py
(`performance_factor`) + the my_performance.json schema; its logic is intentionally not
built yet.
"""
import re
from collections import defaultdict

import scoring

_STRIP = re.compile(r"[^a-z0-9]+")


def _norm_creator(campaign):
    """Normalized creator/brand key for name-matching across runs, or None."""
    src = campaign.get("source") or {}
    raw = campaign.get("creator") or src.get("name")
    if not raw:
        return None
    key = _STRIP.sub(" ", raw.lower()).strip()
    return key or None


def _recurring_map(records):
    """creator-key -> set of distinct campaign ids seen across ALL records (this run's
    plus every campaign carried forward in campaigns.json = the run history)."""
    by_creator = defaultdict(set)
    for c in records:
        key = _norm_creator(c)
        cid = c.get("id")
        if key and cid:
            by_creator[key].add(cid)
    return by_creator


def compute_strategic_signals(records):
    """Fill the strategic signal fields on every active record, in place. `records` must
    be the FULL assembled list (active + carried) so recurring-creator counts span the
    whole run history. Returns the number of active records processed."""
    by_creator = _recurring_map(records)
    active = [c for c in records if c.get("status") in ("scraped", "refreshed")]
    for c in active:
        key = _norm_creator(c)
        if key:
            ids = by_creator.get(key, set())
            cnt = len(ids)
            c["recurring_creator"] = {
                "creator": c.get("creator") or (c.get("source") or {}).get("name"),
                "campaign_count": cnt, "previous_count": max(cnt - 1, 0),
                "basis": "distinct campaigns by this creator across run history",
            }
        else:
            c["recurring_creator"] = {"creator": None, "campaign_count": None,
                                      "previous_count": None,
                                      "basis": "no creator/brand name to match on"}

        c["budget_drain"] = scoring.project_budget_drain(
            c.get("snapshot_history"), c.get("snapshot"))
        c["participant_growth"] = scoring.participant_growth(
            c.get("snapshot_history"), c.get("snapshot"))
        c["source_saturation"] = scoring.source_saturation_estimate(c)
        c["account_reusability"] = scoring.account_reusability(
            c.get("category"), (c.get("content_type") or {}).get("type"),
            c.get("rules_text"))
    return len(active)
