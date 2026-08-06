"""Source-popularity lookup — best-effort and HONEST.

For the handles a campaign brief names (via extract.extract_handles), try to
retrieve follower/subscriber counts and recent-post view averages, so ranking can
favour campaigns whose source creator actually has reach — and, more importantly,
recent traction (a 1M-follower channel averaging 5k views is a trap).

Two retrieval paths, both metadata-only, both no-download:
  - yt-dlp for anything it understands (YouTube, TikTok, sometimes Kick): pulls
    `channel_follower_count` and the view_count of the last ~10 posts.
  - a single lightweight public-profile HTTP fetch as a fallback (Instagram / X /
    anything yt-dlp declines), scraping a "N Followers" figure from the page's
    meta description.

The cardinal rule: NEVER invent a number. Any failure leaves the field None and
the campaign's confidence drops accordingly. Missing yt-dlp, a timeout, a blocked
profile, a parse miss — all yield None, never a crash and never a guess.

Confidence per campaign:
  HIGH    — at least one real follower or recent-traction number was retrieved.
  LOW     — a creator/handle is identified but no count could be retrieved.
  UNKNOWN — no identifiable source at all.
"""
import json
import re
import shutil
import subprocess
import urllib.request

# yt-dlp handles these natively; the rest fall through to the HTTP fetch.
_YTDLP_PLATFORMS = ("youtube", "tiktok", "kick")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_COUNT_RE = re.compile(r"([0-9][0-9.,]*)\s*([KMB])?\b", re.I)
# "1,234 Followers", "12.3K Followers", "1.2M followers" in meta/description text.
_FOLLOWERS_RE = re.compile(r"([0-9][0-9.,]*\s*[KMB]?)\s+followers", re.I)


def yt_dlp_available():
    return shutil.which("yt-dlp") is not None


def parse_count(text):
    """'1.2M' / '12.3K' / '1,234' -> int, or None. Never guesses."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return int(text)
    m = _COUNT_RE.search(text.strip())
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    mult = {"k": 1e3, "m": 1e6, "b": 1e9}.get((m.group(2) or "").lower(), 1)
    return int(num * mult)


def _ytdlp_json(url, timeout):
    """Single-JSON metadata dump for a profile/channel, or None on any problem."""
    try:
        proc = subprocess.run(
            ["yt-dlp", "--dump-single-json", "--flat-playlist",
             "--playlist-end", "10", "--skip-download", url],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    out = proc.stdout.strip()
    try:
        return json.loads(out)
    except Exception:
        # Some extractors emit NDJSON; take the first parseable object.
        for line in out.splitlines():
            line = line.strip()
            if line:
                try:
                    return json.loads(line)
                except Exception:
                    continue
    return None


def lookup_ytdlp(url, timeout=90):
    """Follower count + recent-post average views via yt-dlp, or None."""
    data = _ytdlp_json(url, timeout)
    if not data:
        return None
    followers = data.get("channel_follower_count")
    if followers is None:
        followers = data.get("subscriber_count")
    entries = data.get("entries") or []
    views = [e.get("view_count") for e in entries
             if isinstance(e.get("view_count"), (int, float))]
    recent = round(sum(views) / len(views)) if views else None
    if followers is None and recent is None:
        return None
    return {"followers": int(followers) if followers is not None else None,
            "recent_avg_views": recent}


def lookup_http(url, timeout=20):
    """Fallback: fetch the public profile and scrape a 'N Followers' figure.

    Fragile by nature (many sites block or render client-side) — on anything less
    than a clean match we return None so the field stays honestly unknown.
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                                    "Accept-Language": "en-US,en;q=0.9"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read(600_000).decode("utf-8", "ignore")
    except Exception:
        return None
    m = _FOLLOWERS_RE.search(html)
    if not m:
        return None
    n = parse_count(m.group(1))
    if n is None:
        return None
    return {"followers": n, "recent_avg_views": None}


def lookup_handle(handle, pacer=None):
    """Retrieve counts for one handle dict (mutates it in place). Returns True if a
    real number was obtained. yt-dlp first for platforms it knows, HTTP otherwise;
    HTTP is also tried as a last resort when yt-dlp comes back empty."""
    url = handle.get("url")
    if not url:
        return False
    info = None
    if handle.get("platform") in _YTDLP_PLATFORMS and yt_dlp_available():
        info = lookup_ytdlp(url)
    if info is None:
        info = lookup_http(url)
    if not info:
        return False
    handle["followers"] = info.get("followers")
    handle["recent_avg_views"] = info.get("recent_avg_views")
    return bool(handle.get("followers") or handle.get("recent_avg_views"))


def probe_source(campaign, pacer):
    """Fill a campaign's `source` block with retrieved counts + confidence, in place."""
    src = campaign.get("source") or {}
    handles = src.get("handles") or []
    followers, recent = [], []
    got_real_number = False
    for h in handles:
        try:
            if lookup_handle(h, pacer):
                got_real_number = True
        except Exception:
            pass  # honest failure: leave this handle's counts as None
        if h.get("followers"):
            followers.append(h["followers"])
        if h.get("recent_avg_views"):
            recent.append(h["recent_avg_views"])
        if pacer is not None:
            pacer.page_delay()  # same human respect between external hits

    src["reach_estimate"] = sum(followers) if followers else None
    src["recent_avg_views"] = round(sum(recent) / len(recent)) if recent else None
    if got_real_number:
        src["confidence"] = "HIGH"
    elif src.get("name") or handles:
        src["confidence"] = "LOW"
    else:
        src["confidence"] = "UNKNOWN"
    campaign["source"] = src


def _has_resolvable_handle(campaign):
    """True if the campaign has at least one handle with a URL we can actually look up.
    A name-only source can NEVER yield a real reach number (there's nothing to fetch), so
    it must not consume a probe slot."""
    for h in (campaign.get("source") or {}).get("handles") or []:
        if h.get("url"):
            return True
    return False


def probe_sources(campaigns, top_n, pacer):
    """Look up source popularity for the highest pre-scored campaigns THAT HAVE A
    RESOLVABLE HANDLE, in place. Returns the number probed.

    Ordered by the crude pre_score (composite needs this data, so it can't order the
    selection yet). Two hard filters before spending a probe slot:
      - skip HIGH confidence — on delta runs those counts carry forward from
        campaigns.json, so we don't re-hit external sites;
      - skip campaigns with NO resolvable handle URL — a name-only source can only ever
        come back LOW (nothing to fetch), so probing it wastes the budget. This is the
        fix for the 0/N reach regression: previously the top pre_score campaigns were all
        name-only, so the whole top_n budget was consumed on unlookupable campaigns while
        the few handle-having ones (ranked below the cutoff) were never probed.
    """
    active = [c for c in campaigns if c.get("status") in ("scraped", "refreshed")]
    probable = [c for c in active
                if (c.get("source") or {}).get("confidence") != "HIGH"
                and _has_resolvable_handle(c)]
    probable.sort(key=lambda c: c.get("pre_score", 0), reverse=True)

    probed = 0
    for c in probable:
        if probed >= top_n:
            break
        probe_source(c, pacer)
        probed += 1
    return probed
