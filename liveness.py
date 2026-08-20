"""Footage LINK-LIVENESS probe — a cheap, metadata-only "is this footage actually
still there?" check, distinct from intake's accessibility verdict.

The motivating failure: a campaign whose footage links are DEAD/OFFLINE (a removed
YouTube video, an empty/locked Drive folder, a stream channel with no VODs left) still
ranked high, wasting the clipper's time. intake.py's accessibility answers "did the URL
respond?" — but a Kick channel that is offline-live-only, or a YouTube channel with zero
uploads, RESPONDS while offering nothing to clip. This module answers the narrower
question "does the link resolve to actual, watchable content?" and HEAVILY DERANKS (not
excludes) a campaign whose footage is entirely dead.

HARD design rules (so this never gets Scout's IP throttled):
  - METADATA ONLY — playability markers, flat-playlist counts, HEAD requests. NEVER a
    download.
  - GENTLE + SPACED — a random sleep between FRESH network probes (cache hits don't sleep).
    This is an off-Whop analysis probe (hits YouTube/Kick, not Whop), like proven_clips.
  - CACHED per LINK across campaigns AND runs (liveness_cache.json, TTL) — a link probed
    once isn't re-probed until it goes stale, so a board that shares links (or a delta run)
    barely touches the network.
  - FAIL-OPEN — a probe that errors, times out, is rate-limited, or is inconclusive returns
    UNKNOWN, which NEVER penalizes. A failed probe is not a dead link. The heavy derank
    fires ONLY when we AFFIRMATIVELY found dead sources and found NOTHING alive.

Verdicts per source: "alive" | "dead" | "unknown". Campaign-level: a campaign is "dead"
(penalized) only when it has links, ZERO are alive, and at least one is dead.

Pure/testable (no network): `campaign_liveness`, `_normalize_channel_videos_url`,
`classify_link_verdict`. The network probes are best-effort and never raise.
"""
import json
import random
import re
import time
from datetime import datetime, timezone

from intake import (classify_source, yt_dlp_available, _http_get, _http_head,
                    _ytdlp_flat, _DRIVE_LOCKED)

# YouTube playabilityStatus → a liveness verdict. Only GENUINELY gone states are "dead";
# a sign-in/age wall is "unknown" (walled ≠ removed), so we never derank on a login wall.
_YT_LIVENESS = {
    "OK": "alive",
    "ERROR": "dead",            # private or deleted
    "UNPLAYABLE": "dead",       # removed / region-locked / members-only-gone
    "LIVE_STREAM_OFFLINE": "dead",
    "LOGIN_REQUIRED": "unknown",
    "AGE_CHECK_REQUIRED": "unknown",
}


# =============================================================================
# PURE helpers (no network — freely testable)
# =============================================================================
def _normalize_channel_videos_url(url):
    """Point a YouTube channel URL at its /videos tab so a flat-playlist lists real
    UPLOADS (a bare @handle lists the channel's tabs, not its videos — the artifact that
    made a 20-upload channel look like it had '2 videos'). Idempotent; leaves explicit
    /streams, /shorts, playlist, or already-/videos URLs alone."""
    low = url.lower()
    if any(s in low for s in ("/videos", "/streams", "/shorts", "/playlist", "list=")):
        return url
    return url.rstrip("/") + "/videos"


def classify_link_verdict(kind, *, accessible=None, content_count=None, locked=False,
                          http_status=None, yt_playability=None, ytdlp_ran=None):
    """Pure mapping of already-gathered probe facts to alive/dead/unknown. Kept separate
    from the network probes so the decision logic is testable with plain values.

      - locked / deleted / removed / 404 / 410 -> dead
      - a channel/folder that RESOLVED with a real content_count of 0 -> dead (empty)
      - content_count > 0, or an OK/reachable single asset -> alive
      - anything we could not determine (walls, 403 blocks, no yt-dlp, parse miss) -> unknown
    """
    if yt_playability is not None:
        return _YT_LIVENESS.get(yt_playability, "unknown")
    if locked:
        return "dead"
    if http_status in (404, 410):
        return "dead"
    if content_count is not None:
        return "dead" if content_count == 0 else "alive"
    if accessible is True:
        return "alive"
    if accessible is False:
        return "dead"
    return "unknown"


def campaign_liveness(verdicts, has_links):
    """Roll per-source verdicts into a campaign verdict. Penalized ONLY when there are
    links, zero sources are alive, and at least one is affirmatively dead — so a campaign
    with any live footage, or with only-inconclusive probes, is never deranked (fail-open).

    Returns {status, penalized, reason, alive[], dead[], unknown[]} where status is one of
    alive | partial | dead | unknown | no_links."""
    if not has_links:
        return {"status": "no_links", "penalized": False, "alive": [], "dead": [],
                "unknown": [], "reason": "no footage links to check"}

    def slim(vs):
        return [{"url": v["url"], "kind": v["kind"], "reason": v.get("reason")} for v in vs]

    alive = [v for v in verdicts if v.get("verdict") == "alive"]
    dead = [v for v in verdicts if v.get("verdict") == "dead"]
    unknown = [v for v in verdicts if v.get("verdict") == "unknown"]

    if alive:
        status = "alive" if not dead else "partial"
        penalized = False
        reason = (f"{len(alive)} live source(s)"
                  + (f", {len(dead)} dead (live footage still available)" if dead else ""))
    elif dead:
        status = "dead"
        penalized = True
        reason = ("all footage sources dead/offline: "
                  + "; ".join(f"{v['kind']} {v.get('reason')}" for v in dead))
    else:
        status = "unknown"
        penalized = False
        reason = "liveness undetermined (probe inconclusive/blocked) — not penalized (fail-open)"

    return {"status": status, "penalized": penalized, "reason": reason,
            "alive": slim(alive), "dead": slim(dead), "unknown": slim(unknown)}


# =============================================================================
# PER-SOURCE network probes (best-effort; never raise; verdict alive/dead/unknown)
# =============================================================================
def _live_youtube_video(url):
    status, body, err = _http_get(url)
    if body is None:
        return {"url": url, "kind": "youtube_video", "verdict": "unknown",
                "reason": f"{err or f'HTTP {status}'} (fail-open)"}
    m = re.search(r'"playabilityStatus":\{"status":"([A-Z_]+)"', body)
    play = m.group(1) if m else None
    verdict = classify_link_verdict("youtube_video", yt_playability=play,
                                    accessible=True if play is None else None)
    reason = (f"playability {play}" if play else "reachable (no playability marker)")
    return {"url": url, "kind": "youtube_video", "verdict": verdict, "reason": reason}


def _live_youtube_channel(url):
    if not yt_dlp_available():
        return {"url": url, "kind": "youtube_channel", "verdict": "unknown",
                "reason": "channel upload count needs yt-dlp (absent) — fail-open"}
    data = _ytdlp_flat(_normalize_channel_videos_url(url), limit=10)
    if data is None:
        return {"url": url, "kind": "youtube_channel", "verdict": "unknown",
                "reason": "yt-dlp could not list channel uploads (fail-open)"}
    n = len(data.get("entries") or [])
    verdict = classify_link_verdict("youtube_channel", content_count=n)
    return {"url": url, "kind": "youtube_channel", "verdict": verdict,
            "reason": f"{n} upload(s) listed" if n else "channel has 0 uploads (empty/offline)"}


def _live_stream_channel(url, kind):
    """Kick/Twitch channel: does it still have VODs, or is it offline-live-only? Needs
    yt-dlp; a 403/block/absence is UNKNOWN (fail-open), never 'dead' — Kick 403s automated
    access even for live channels."""
    if not yt_dlp_available():
        return {"url": url, "kind": kind, "verdict": "unknown",
                "reason": f"{kind} VOD listing needs yt-dlp (absent) — fail-open"}
    base = url.rstrip("/")
    listing = base if base.endswith("/videos") else base + "/videos"
    data = _ytdlp_flat(listing, limit=10)
    if data is None:
        return {"url": url, "kind": kind, "verdict": "unknown",
                "reason": f"{kind} VOD listing blocked/failed (e.g. 403) — fail-open"}
    n = len(data.get("entries") or [])
    verdict = classify_link_verdict(kind, content_count=n)
    return {"url": url, "kind": kind, "verdict": verdict,
            "reason": f"{n} VOD(s) available" if n else "no VODs (offline-live-only)"}


def _live_drive_folder(url):
    status, body, err = _http_get(url)
    if body is None:
        return {"url": url, "kind": "drive_folder", "verdict": "unknown",
                "reason": f"{err or f'HTTP {status}'} (fail-open)"}
    if _DRIVE_LOCKED.search(body):
        return {"url": url, "kind": "drive_folder", "verdict": "dead",
                "reason": "private / permission-locked Drive folder"}
    hits = len(re.findall(r"video/mp4|\.mp4|\.mov|\.mkv|\.webm", body, re.I))
    if hits:
        return {"url": url, "kind": "drive_folder", "verdict": "alive",
                "reason": f"{hits} video entr{'y' if hits == 1 else 'ies'} in folder"}
    # Reachable but no video entries parsed: could be empty OR a parse miss -> UNKNOWN
    # (fail-open; we do not call a reachable folder 'dead' on an ambiguous parse).
    return {"url": url, "kind": "drive_folder", "verdict": "unknown",
            "reason": "folder reachable but item count unclear — fail-open"}


def _live_direct(url, kind):
    status, headers, err = _http_head(url)
    if status is None:
        return {"url": url, "kind": kind, "verdict": "unknown", "reason": f"{err} (fail-open)"}
    if status in (404, 410):
        return {"url": url, "kind": kind, "verdict": "dead", "reason": f"HTTP {status} (dead link)"}
    if kind == "drive_file" and status in (401, 403):
        return {"url": url, "kind": kind, "verdict": "unknown",
                "reason": f"HTTP {status} (walled) — fail-open"}
    if 200 <= status < 400:
        return {"url": url, "kind": kind, "verdict": "alive", "reason": f"reachable (HTTP {status})"}
    return {"url": url, "kind": kind, "verdict": "unknown", "reason": f"HTTP {status} (fail-open)"}


def probe_link(url):
    """Dispatch one footage URL to its liveness probe. Never raises (fail-open)."""
    kind = classify_source(url)
    try:
        if kind == "youtube_video":
            return _live_youtube_video(url)
        if kind == "youtube_channel":
            return _live_youtube_channel(url)
        if kind in ("kick", "twitch"):
            return _live_stream_channel(url, kind)
        if kind == "drive_folder":
            return _live_drive_folder(url)
        if kind in ("drive_file", "direct_file"):
            return _live_direct(url, kind)
        # gdoc / other: not a footage source to judge for liveness
        return {"url": url, "kind": kind, "verdict": "unknown",
                "reason": f"not a footage source ({kind}) — not judged"}
    except Exception as e:
        return {"url": url, "kind": kind, "verdict": "unknown",
                "reason": f"probe error: {type(e).__name__} (fail-open)"}


# =============================================================================
# Per-LINK cache (dedupe across campaigns AND runs within a TTL)
# =============================================================================
def _load_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("links", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_cache(path, links):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"updated_at": datetime.now(timezone.utc).isoformat(), "links": links},
                      f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _fresh(entry, max_age_days):
    """True if a cached link verdict is still within its TTL. UNKNOWN verdicts are NOT
    cached long — they should be retried — so treat them as always stale (re-probe)."""
    if not entry or entry.get("verdict") == "unknown":
        return False
    ts = entry.get("checked_at")
    if not ts:
        return False
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
    except Exception:
        return False
    return age <= max_age_days * 86400.0


# =============================================================================
# Per-campaign orchestration
# =============================================================================
def _resolve_link(url, cache, cfg, did_network):
    """Return a verdict dict for one link, using the cache when fresh else a fresh probe
    (spaced). `did_network` is a 1-element list flag so the caller can space only real
    network calls."""
    cached = cache.get(url)
    if _fresh(cached, getattr(cfg, "liveness_cache_max_age_days", 7)):
        v = dict(cached)
        v["url"] = url
        v["cached"] = True
        return v
    if did_network[0]:
        lo, hi = getattr(cfg, "liveness_probe_spacing", (1.0, 3.0))
        time.sleep(random.uniform(lo, hi))   # gentle spacing between FRESH probes only
    result = probe_link(url)
    did_network[0] = True
    cache[url] = {"kind": result["kind"], "verdict": result["verdict"],
                  "reason": result.get("reason"),
                  "checked_at": datetime.now(timezone.utc).isoformat()}
    result["cached"] = False
    return result


def probe_campaign(rec, cache, cfg):
    """Fill rec['liveness'] + rec['liveness_penalty_factor'] for one campaign. Uses the
    per-link cache; only FRESH probes touch the network (and are spaced). Never raises."""
    links = [u for u in (rec.get("source_links") or []) if u]
    if not links:
        rec["liveness"] = campaign_liveness([], has_links=False)
        rec["liveness_penalty_factor"] = 1.0
        return False
    did_network = [False]
    verdicts = [_resolve_link(u, cache, cfg, did_network) for u in links]
    live = campaign_liveness(verdicts, has_links=True)
    rec["liveness"] = live
    rec["liveness_penalty_factor"] = (
        getattr(cfg, "liveness_dead_penalty", 0.15) if live["penalized"] else 1.0)
    return did_network[0]


def probe_campaigns(campaigns, cfg):
    """Run the liveness check over every non-disqualified active campaign. Cached per link
    across campaigns + runs; fail-open; gentle. Returns (checked, penalized) counts."""
    if not getattr(cfg, "liveness_enabled", True):
        return (0, 0)
    active = [c for c in campaigns
              if c.get("status") in ("scraped", "refreshed") and not c.get("disqualified")]
    cache_path = getattr(cfg, "liveness_cache_path", "liveness_cache.json")
    cache = _load_cache(cache_path)
    print(f"  Link liveness: checking footage links for {len(active)} campaign(s) "
          f"(metadata-only, cached, fail-open)"
          + ("" if yt_dlp_available() else " — yt-dlp absent → channel/VOD counts UNKNOWN "
             "(fail-open, no penalty); HTTP link checks still run") + "...")
    penalized = 0
    for c in active:
        try:
            probe_campaign(c, cache, cfg)
            if (c.get("liveness") or {}).get("penalized"):
                penalized += 1
        except Exception as e:
            c["liveness"] = {"status": "unknown", "penalized": False,
                             "reason": f"liveness failed: {type(e).__name__}: {e} (fail-open)"}
            c["liveness_penalty_factor"] = 1.0
    _write_cache(cache_path, cache)
    if penalized:
        print(f"    {penalized} campaign(s) deranked for dead/offline footage "
              f"(x{getattr(cfg, 'liveness_dead_penalty', 0.15)} composite).")
    return (len(active), penalized)


# =============================================================================
# Standalone CLI — check footage links directly (no scout/browser).
#   python liveness.py <url> [<url> ...]
# =============================================================================
def _cli(argv):
    if not argv:
        print("usage: python liveness.py <source_url> [<source_url> ...]")
        return 2

    class _Cfg:
        liveness_cache_path = None
        liveness_cache_max_age_days = 7
        liveness_probe_spacing = (1.0, 2.0)
        liveness_dead_penalty = 0.15

    rec = {"status": "scraped", "source_links": list(argv)}
    probe_campaign(rec, {}, _Cfg())
    print(json.dumps({"liveness": rec["liveness"],
                      "liveness_penalty_factor": rec["liveness_penalty_factor"]},
                     indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
