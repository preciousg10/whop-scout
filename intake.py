"""Campaign SUBSTANCE intake — ported/adapted from the clipper's intake.py + analyze.py
+ download.py (C:\\whop\\clipper\\scripts). Judges whether a campaign is actually
clippable, not just whether its stats look good. Four dimensions, metadata-only (NEVER
downloads full videos):

  1. FOOTAGE ACCESSIBILITY (runs first — it's a hard disqualifier). Is each source link
     actually reachable? Detects login walls, HTTP 403 (Kick), private/permission-locked
     Drive, dead links. No usable source -> DISQUALIFY. Some work / some don't -> penalty.
  2. CONTENT TYPE — what would I actually be clipping? standard_stream_vod /
     podcast_interview / slideshow_photo / music_video / short_form_only_unusual /
     ugc_requires_my_face / other. Non-standard is flagged + penalized heavily. (The
     "Odyssey" trap: two 2-minute movie trailers, not a normal stream.)
  3. FOOTAGE VOLUME + REFRESH — total hours available, and whether the source is a
     ONE-TIME dump or a RECURRING channel (weighted much higher — sustainable footage
     matters for a daily op), plus recency.
  4. ACTION / ENTERTAINMENT DENSITY — what fraction is real payoff vs logistics/dead
     talk, estimated from subtitles (cheapest) when available, else UNKNOWN.

Design rules (hard requirements):
  - HTTP-first: YouTube playability/duration/captions and Drive folder access are read
    over plain HTTP, so accessibility + content-type + volume work even WITHOUT yt-dlp.
    yt-dlp (when present) enhances: channel volume/cadence, Kick cookies, subtitle text
    for density. When a value truly can't be read it is UNKNOWN — never guessed, and
    UNKNOWN is NEUTRAL in scoring, never a silent zero.
  - Every probe is wrapped so one campaign's failure never aborts the run; the specific
    reason is recorded on the record and logged.
  - Results are cached per campaign keyed by the source-link set, so unchanged campaigns
    are not re-probed on the next run.
"""
import html
import json
import re
import shutil
import subprocess
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Non-standard content types flagged + penalized. Standard (clippable) = the first two.
STANDARD_CONTENT_TYPES = ("standard_stream_vod", "podcast_interview")
CONTENT_TYPES = STANDARD_CONTENT_TYPES + (
    "slideshow_photo", "music_video", "short_form_only_unusual",
    "ugc_requires_my_face", "other")

SHORT_VIDEO_SEC = 180.0   # a source whose videos are all shorter than this is "unusual"


def yt_dlp_available():
    return shutil.which("yt-dlp") is not None


# =============================================================================
# HTTP helpers (never raise; return (status, text|None, error))
# =============================================================================
def _http_get(url, timeout=25, max_bytes=1_500_000):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                                   "Accept-Language": "en-US,en;q=0.9"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(max_bytes).decode("utf-8", "ignore"), None
    except urllib.error.HTTPError as e:
        return e.code, None, f"HTTP {e.code}"
    except Exception as e:
        return None, None, f"{type(e).__name__}: {e}"


def _http_head(url, timeout=15):
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), None
    except urllib.error.HTTPError as e:
        return e.code, dict(getattr(e, "headers", {}) or {}), f"HTTP {e.code}"
    except Exception as e:
        return None, {}, f"{type(e).__name__}: {e}"


# =============================================================================
# PURE classifiers (no network — freely testable)
# =============================================================================
def classify_source(url):
    """Route a source URL to a probe kind. Never None."""
    low = (url or "").lower()
    if "drive.google.com" in low:
        return "drive_folder" if ("/folders/" in low or "folderview" in low) else "drive_file"
    if "docs.google.com" in low:
        return "gdoc"
    if "youtube.com" in low or "youtu.be" in low:
        if any(s in low for s in ("/@", "/channel/", "/c/", "/user/", "/playlist",
                                  "/videos", "/streams")) and "watch?v=" not in low:
            return "youtube_channel"
        return "youtube_video"
    if "twitch.tv" in low:
        return "twitch"
    if "kick.com" in low:
        return "kick"
    if re.search(r"\.(mp4|mov|mkv|webm|m4v|avi|ts|flv)(\?|#|$)", low):
        return "direct_file"
    return "other"


# Source kinds that are a PUBLIC, downloadable footage source (Drive folder/file, a VOD or
# video/channel URL, a live-stream channel, or a direct media file). gdoc/other are NOT
# footage — a rules doc or an unclassifiable link is not something you can clip.
FOOTAGE_KINDS = frozenset({"drive_folder", "drive_file", "youtube_video", "youtube_channel",
                           "twitch", "kick", "direct_file"})


def footage_presence(rec):
    """Does this campaign expose a PUBLIC, downloadable footage link AT ALL?

    DISTINCT from liveness (is an EXISTING link alive/dead) and accessibility (did the link
    respond) — this asks only whether a public footage link EXISTS. It catches the top-of-board
    trap where the footage is member-gated behind joining the campaign (Jesser x ClipFarm,
    SomSleep): intake can't download anything, so the auto-run wastes its walk on them.

    Returns {has_public_footage: True|False|None, footage_link_count, total_link_count,
    determinable, reason}. FAILS OPEN: if the detail section was never loaded (an unscraped
    card-only stub, no `scraped_at`), footage presence is UNDETERMINABLE -> None (no penalty —
    assume it might have footage rather than wrongly bury it). Only a fully-scraped campaign
    with ZERO public footage links is False (the derank case)."""
    links = [u for u in (rec.get("source_links") or []) if u]
    footage = [u for u in links if classify_source(u) in FOOTAGE_KINDS]
    n = len(footage)
    if n > 0:
        return {"has_public_footage": True, "footage_link_count": n,
                "total_link_count": len(links), "determinable": True,
                "reason": f"{n} public footage link(s)"}
    if not rec.get("scraped_at"):
        return {"has_public_footage": None, "footage_link_count": 0,
                "total_link_count": len(links), "determinable": False,
                "reason": "detail not loaded — footage presence undeterminable (fail-open)"}
    return {"has_public_footage": False, "footage_link_count": 0,
            "total_link_count": len(links), "determinable": True,
            "reason": "no public footage link — footage member-gated or absent"}


# --- content type from the brief text ------------------------------------------
_CT_UGC = re.compile(
    r"\b(face\s+on\s+camera|on[-\s]camera|show\s+your\s+face|film\s+yourself|"
    r"record\s+yourself|talking[-\s]head|ugc|user[-\s]generated|be\s+on\s+camera)\b", re.I)
_CT_SLIDESHOW = re.compile(
    r"\b(slideshow|image\s+post|photo\s+post|carousel|static\s+image|picture\s+only|"
    r"photo\s+dump|image\s+carousel)\b", re.I)
_CT_MUSIC = re.compile(
    r"\b(music\s+video|official\s+audio|lyric\s+video|song\s+promo|album|single\b|"
    r"streaming\s+numbers|spotify\s+stream)\b", re.I)
_CT_PODCAST = re.compile(
    r"\b(podcast|episode|interview|sit[-\s]down|guest|the\s+show|conversation|"
    r"long[-\s]form\s+talk)\b", re.I)
_CT_STREAM = re.compile(
    r"\b(stream|vod|twitch|kick|gameplay|gaming|just\s+chatting|livestream|"
    r"live\s+stream|highlights?\s+of\s+(?:the\s+)?stream|irl)\b", re.I)


def content_type_from_brief(text):
    """Best content-type guess from the brief text alone, or None (undetermined).
    Order matters: the disqualifying/unusual signals win over generic 'stream'."""
    if not text:
        return None
    if _CT_UGC.search(text):
        return "ugc_requires_my_face"
    if _CT_SLIDESHOW.search(text):
        return "slideshow_photo"
    if _CT_MUSIC.search(text):
        return "music_video"
    if _CT_PODCAST.search(text):
        return "podcast_interview"
    if _CT_STREAM.search(text):
        return "standard_stream_vod"
    return None


def classify_content_type(brief_text, probes):
    """Combine the brief guess with what the probed source content actually is.
    Probe evidence (real durations) overrides the brief. Returns {type, standard,
    evidence}. type=None means UNKNOWN (neutral in scoring — never guessed)."""
    brief_type = content_type_from_brief(brief_text)
    evidence = []
    if brief_type:
        evidence.append(f"brief text → {brief_type}")

    vids = [p for p in probes if p.get("accessible") and p.get("kind") in
            ("youtube_video", "twitch", "kick", "direct_file")]
    durs = [p["duration_sec"] for p in vids if isinstance(p.get("duration_sec"), (int, float))]
    has_channel = any(p.get("kind") == "youtube_channel" and p.get("accessible") for p in probes)

    ctype = brief_type
    # Real durations override: a fixed set of short videos is the "unusual" trap.
    if durs and not has_channel and max(durs) < SHORT_VIDEO_SEC and len(durs) <= 6:
        ctype = "short_form_only_unusual"
        evidence.append(f"{len(durs)} source video(s), all < {int(SHORT_VIDEO_SEC)}s "
                        f"(max {max(durs):.0f}s) → not a normal stream to clip")
    elif durs and not brief_type:
        # Long-form videos with no other signal → treat as a standard VOD.
        if max(durs) >= 600:
            ctype = "standard_stream_vod"
            evidence.append(f"long-form source video(s) up to {max(durs) / 60:.0f} min")

    standard = ctype in STANDARD_CONTENT_TYPES if ctype else None
    return {"type": ctype, "standard": standard, "evidence": evidence}


# --- action / entertainment density (pure transcript analysis) -----------------
_LOGISTICS = (
    "let me", "one sec", "give me a sec", "give me a second", "hold on", "hold up",
    "be right back", "brb", "starting soon", "technical difficult", "loading",
    "my mic", "can you hear", "is the stream", "let me check", "give me a minute",
    "so yeah", "anyway", "let me pull up", "setting up", "real quick", "bear with me",
    "stand by", "in a second", "let me just", "waiting for", "load in", "lobby",
    "subscribe", "follow me", "link in", "promo code", "discord.gg", "sponsor",
    "let's see", "where is", "gimme a", "back in a")
_EVENTFUL = (
    "no way", "oh my god", "let's go", "lets go", "insane", "clutch", "what the",
    "are you kidding", "unbelievable", "holy", "that's crazy", "thats crazy", "won",
    "world record", "last second", "can't believe", "cant believe", "no shot",
    "get in", "poggers", "wtf", "omg", "huge", "massive", "biggest", "haha", "lmao",
    "screaming", "crazy", "actually", "clutched", "insane", "let's gooo", "no chance")


def analyze_transcript_density(text, *, min_words=200):
    """Estimate the eventful vs logistics/dead-talk mix of a transcript. Returns
    {score, band, method, n_words, eventful_hits, logistics_hits}. UNKNOWN (score None)
    if there's too little text — we never guess density."""
    if not text:
        return {"score": None, "band": "UNKNOWN", "method": "no transcript",
                "n_words": 0, "eventful_hits": 0, "logistics_hits": 0}
    low = text.lower()
    n_words = len(low.split())
    if n_words < min_words:
        return {"score": None, "band": "UNKNOWN", "method": "transcript too short",
                "n_words": n_words, "eventful_hits": 0, "logistics_hits": 0}
    ev = sum(low.count(m) for m in _EVENTFUL)
    lo = sum(low.count(m) for m in _LOGISTICS)
    total = ev + lo
    if total < 3:
        return {"score": None, "band": "UNKNOWN",
                "method": "too few signal markers to judge", "n_words": n_words,
                "eventful_hits": ev, "logistics_hits": lo}
    score = round(ev / total, 3)
    band = ("logistics_heavy" if score < 0.25 else "mixed" if score < 0.5 else "eventful")
    return {"score": score, "band": band, "method": "subtitles", "n_words": n_words,
            "eventful_hits": ev, "logistics_hits": lo}


# =============================================================================
# PER-SOURCE PROBES (best-effort; never raise; accessible True/False/None)
# =============================================================================
_YT_PLAYABILITY = {
    "OK": (True, None),
    "LOGIN_REQUIRED": (False, "login-walled (sign-in required)"),
    "AGE_CHECK_REQUIRED": (False, "age-restricted (sign-in required)"),
    "UNPLAYABLE": (False, "unplayable (members-only / removed / region-locked)"),
    "ERROR": (False, "video unavailable (private or deleted)"),
    "LIVE_STREAM_OFFLINE": (False, "live stream offline / not yet a VOD"),
}


def _probe_youtube_video(url):
    out = {"url": url, "kind": "youtube_video", "accessible": None, "reason": None,
           "duration_sec": None, "captions_available": None, "title": None}
    status, body, err = _http_get(url)
    if body is None:
        out["accessible"] = None if status is None else False
        out["reason"] = err or f"HTTP {status}"
        return out
    m = re.search(r'"playabilityStatus":\{"status":"([A-Z_]+)"', body)
    if m:
        ok, reason = _YT_PLAYABILITY.get(m.group(1), (None, f"playability {m.group(1)}"))
        out["accessible"], out["reason"] = ok, reason
    else:
        out["accessible"], out["reason"] = True, "reachable (no playability marker)"
    dur = re.search(r'"lengthSeconds":"(\d+)"', body)
    if dur:
        out["duration_sec"] = int(dur.group(1))
    out["captions_available"] = '"captionTracks"' in body
    t = re.search(r'"title":\{"runs":\[\{"text":"([^"]{1,80})"', body) or \
        re.search(r'<title>([^<]{1,90})</title>', body)
    if t:
        out["title"] = html.unescape(t.group(1))
    return out


def _probe_youtube_channel(url):
    out = {"url": url, "kind": "youtube_channel", "accessible": None, "reason": None,
           "duration_sec": None, "video_count": None, "latest_upload": None}
    # yt-dlp gives real volume/cadence; HTTP only confirms the channel exists.
    if yt_dlp_available():
        data = _ytdlp_flat(url, limit=40)
        if data is not None:
            entries = data.get("entries") or []
            durs = [e.get("duration") for e in entries
                    if isinstance(e.get("duration"), (int, float))]
            dates = sorted({e.get("upload_date") for e in entries if e.get("upload_date")},
                           reverse=True)
            out.update(accessible=True, reason="channel listed via yt-dlp",
                       duration_sec=sum(durs) if durs else None,
                       video_count=len(entries),
                       latest_upload=dates[0] if dates else None)
            return out
    status, body, err = _http_get(url)
    if body is not None and status == 200:
        out.update(accessible=True, reason="channel page reachable (volume unknown "
                                           "without yt-dlp)")
    else:
        out.update(accessible=(None if status is None else False),
                   reason=err or f"HTTP {status}")
    return out


def _probe_kick(url):
    out = {"url": url, "kind": "kick", "accessible": None, "reason": None,
           "duration_sec": None}
    if yt_dlp_available():
        data = _ytdlp_json(url)
        if data:
            out.update(accessible=True, reason="reachable via yt-dlp",
                       duration_sec=data.get("duration"))
            return out
    status, _b, err = _http_head(url)
    if status == 403:
        out.update(accessible=False,
                   reason="HTTP 403 — Kick blocks automated access (needs browser cookies)")
    elif status and 200 <= status < 400:
        out.update(accessible=True, reason="reachable (HTTP), detail unknown without yt-dlp")
    else:
        out.update(accessible=(None if status is None else False), reason=err or f"HTTP {status}")
    return out


def _probe_twitch(url):
    out = {"url": url, "kind": "twitch", "accessible": None, "reason": None,
           "duration_sec": None}
    if yt_dlp_available():
        data = _ytdlp_json(url)
        if data:
            out.update(accessible=True, reason="reachable via yt-dlp",
                       duration_sec=data.get("duration"))
            return out
    status, _b, err = _http_head(url)
    out.update(accessible=(True if status and 200 <= status < 400 else
                           (None if status is None else False)),
               reason=(err or f"HTTP {status}") if not (status and 200 <= status < 400)
               else "reachable (HTTP), detail unknown without yt-dlp")
    return out


_DRIVE_LOCKED = re.compile(r"you need access|request access to this item|"
                           r"access denied|no preview available", re.I)


def _probe_drive_file(url):
    out = {"url": url, "kind": "drive_file", "accessible": None, "reason": None,
           "duration_sec": None}
    status, body, err = _http_get(url)
    if body is None:
        out.update(accessible=(None if status is None else False), reason=err or f"HTTP {status}")
        return out
    if _DRIVE_LOCKED.search(body):
        out.update(accessible=False, reason="private / permission-locked Drive file")
    else:
        out.update(accessible=True, reason="Drive file shared (reachable)")
    return out


def _probe_drive_folder(url):
    out = {"url": url, "kind": "drive_folder", "accessible": None, "reason": None,
           "item_count": None}
    status, body, err = _http_get(url)
    if body is None:
        out.update(accessible=(None if status is None else False), reason=err or f"HTTP {status}")
        return out
    if _DRIVE_LOCKED.search(body):
        out.update(accessible=False, reason="private / permission-locked Drive folder")
        return out
    # Public folders embed file entries; count video-ish entries as a rough volume proxy.
    hits = len(re.findall(r"video/mp4|\.mp4|\.mov|\.mkv|\.webm", body, re.I))
    out.update(accessible=True, item_count=hits or None,
               reason=f"Drive folder shared ({hits} video entr{'y' if hits == 1 else 'ies'} seen)"
               if hits else "Drive folder shared (item count unclear)")
    return out


def _probe_direct_file(url):
    out = {"url": url, "kind": "direct_file", "accessible": None, "reason": None,
           "duration_sec": None, "size_bytes": None}
    status, headers, err = _http_head(url)
    if status is None:
        out.update(accessible=None, reason=err)
        return out
    if status in (401, 403):
        out.update(accessible=False, reason=f"HTTP {status} (login-walled / forbidden)")
    elif status == 404 or status == 410:
        out.update(accessible=False, reason=f"HTTP {status} (dead link)")
    elif 200 <= status < 400:
        ct = (headers.get("Content-Type") or "").lower()
        cl = headers.get("Content-Length")
        out.update(accessible=True, reason=f"reachable ({ct or 'unknown type'})",
                   size_bytes=int(cl) if cl and cl.isdigit() else None)
    else:
        out.update(accessible=False, reason=f"HTTP {status}")
    return out


def probe_source(url):
    """Dispatch one source URL to the right probe. Never raises."""
    kind = classify_source(url)
    try:
        if kind == "youtube_video":
            return _probe_youtube_video(url)
        if kind == "youtube_channel":
            return _probe_youtube_channel(url)
        if kind == "kick":
            return _probe_kick(url)
        if kind == "twitch":
            return _probe_twitch(url)
        if kind == "drive_file":
            return _probe_drive_file(url)
        if kind == "drive_folder":
            return _probe_drive_folder(url)
        if kind == "direct_file":
            return _probe_direct_file(url)
        return {"url": url, "kind": kind, "accessible": None,
                "reason": f"not a footage source ({kind})"}
    except Exception as e:
        return {"url": url, "kind": kind, "accessible": None,
                "reason": f"probe error: {type(e).__name__}: {e}"}


# =============================================================================
# yt-dlp helpers (subprocess, metadata-only; None on any problem)
# =============================================================================
def _ytdlp_json(url, timeout=90):
    try:
        proc = subprocess.run(["yt-dlp", "--dump-json", "--no-warnings",
                               "--skip-download", url],
                              capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            try:
                return json.loads(line)
            except Exception:
                continue
    return None


def _ytdlp_flat(url, limit=40, timeout=120):
    try:
        proc = subprocess.run(["yt-dlp", "--dump-single-json", "--flat-playlist",
                               "--playlist-end", str(limit), "--no-warnings",
                               "--skip-download", url],
                              capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout.strip())
    except Exception:
        return None


def _parse_vtt(text):
    """Strip a WebVTT/SRT subtitle file down to spoken text (dedup consecutive lines)."""
    out, last = [], None
    for line in (text or "").splitlines():
        line = line.strip()
        if (not line or line == "WEBVTT" or "-->" in line or line.isdigit()
                or line.startswith(("Kind:", "Language:", "NOTE"))):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = html.unescape(line).strip()
        if line and line != last:
            out.append(line)
            last = line
    return " ".join(out)


def fetch_subtitles(url, cookies_from_browser=None, timeout=120):
    """Fetch English subtitles (manual, else auto) via yt-dlp — subtitles only, NO
    video download. Returns transcript text or None. yt-dlp absent / no subs -> None."""
    if not yt_dlp_available():
        return None
    tmp = Path(tempfile.mkdtemp(prefix="scout_subs_"))
    try:
        cmd = ["yt-dlp", "--skip-download", "--write-subs", "--write-auto-subs",
               "--sub-langs", "en.*", "--sub-format", "vtt/srt/best",
               "--no-warnings", "-o", str(tmp / "%(id)s.%(ext)s"), url]
        if cookies_from_browser:
            cmd[1:1] = ["--cookies-from-browser", cookies_from_browser]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except Exception:
            return None
        subs = list(tmp.glob("*.vtt")) + list(tmp.glob("*.srt"))
        if not subs:
            return None
        text = _parse_vtt(subs[0].read_text(encoding="utf-8", errors="ignore"))
        return text or None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =============================================================================
# AGGREGATION (pure)
# =============================================================================
def aggregate_access(probes, has_links):
    """Roll per-source accessibility into a campaign verdict. Statuses:
    ok / partial / none (=> disqualify) / unknown / no_links."""
    if not has_links:
        return {"status": "no_links", "usable": [], "failed": [], "unknown": [],
                "reason": "no source links on the brief — footage source not probeable"}
    usable = [p for p in probes if p.get("accessible") is True]
    failed = [p for p in probes if p.get("accessible") is False]
    unknown = [p for p in probes if p.get("accessible") is None]

    def slim(ps):
        return [{"url": p["url"], "kind": p["kind"], "reason": p.get("reason")} for p in ps]

    if usable:
        status = "ok" if not failed else "partial"
        reason = (f"{len(usable)} usable source(s)"
                  + (f", {len(failed)} unusable" if failed else ""))
    elif failed:
        status = "none"
        reason = "no usable footage source: " + "; ".join(
            f"{p['kind']} {p.get('reason')}" for p in failed)
    else:
        status = "unknown"
        reason = "could not determine accessibility of any source (probe inconclusive)"
    return {"status": status, "reason": reason,
            "usable": slim(usable), "failed": slim(failed), "unknown": slim(unknown)}


def footage_volume(probes):
    """Total hours (when durations are known), one-time vs recurring, and recency.
    Recurrence is inferred from source KINDS (a channel is recurring; a fixed VOD/Drive
    dump is one-time) — that's a real signal available even without durations. Hours are
    UNKNOWN (None) when no duration is readable — never fabricated."""
    usable = [p for p in probes if p.get("accessible") is True]
    if not usable:
        return {"total_hours": None, "recurring": "unknown", "latest_upload": None,
                "sources": 0, "note": "no accessible source to measure"}
    durs = [p["duration_sec"] for p in usable
            if isinstance(p.get("duration_sec"), (int, float)) and p["duration_sec"] > 0]
    total_hours = round(sum(durs) / 3600.0, 3) if durs else None
    kinds = {p["kind"] for p in usable}
    recurring = "recurring" if (kinds & {"youtube_channel", "twitch", "kick"}
                                and any(p["kind"] == "youtube_channel" for p in usable)) \
        else "one_time"
    # A bare twitch/kick CHANNEL (not a single VOD) is also recurring; single VODs aren't.
    dates = [p.get("latest_upload") for p in usable if p.get("latest_upload")]
    latest = max(dates) if dates else None
    return {"total_hours": total_hours, "recurring": recurring, "latest_upload": latest,
            "sources": len(usable),
            "note": "hours from readable durations only" if durs else
            "duration unreadable without yt-dlp — hours UNKNOWN"}


def action_density(probes, cookies_from_browser=None):
    """Estimate action/entertainment density from the primary usable source's subtitles.
    Prefers a YouTube video advertising captions. UNKNOWN (neutral) when no transcript is
    obtainable — never guessed. (Segment-sampling + transcription is a documented future
    path; it needs yt-dlp + a transcriber, so absent those we stay UNKNOWN.)"""
    usable = [p for p in probes if p.get("accessible") is True]
    # order: youtube video w/ captions, any youtube video, any vod-ish source
    def rank(p):
        if p["kind"] == "youtube_video" and p.get("captions_available"):
            return 0
        if p["kind"] == "youtube_video":
            return 1
        if p["kind"] in ("youtube_channel", "twitch", "kick"):
            return 2
        return 3
    for p in sorted(usable, key=rank):
        if p["kind"] not in ("youtube_video", "youtube_channel", "twitch", "kick"):
            continue
        transcript = fetch_subtitles(p["url"], cookies_from_browser=cookies_from_browser)
        if transcript:
            d = analyze_transcript_density(transcript)
            d["source"] = p["url"]
            return d
    reason = ("no subtitles obtainable" if yt_dlp_available()
              else "subtitle fetch needs yt-dlp (not installed) — density UNKNOWN")
    return {"score": None, "band": "UNKNOWN", "method": reason,
            "n_words": 0, "eventful_hits": 0, "logistics_hits": 0}


# =============================================================================
# Per-campaign orchestration + caching
# =============================================================================
def _source_key(links):
    return "|".join(sorted(u for u in (links or []) if u))


def probe_campaign(rec, cfg, pacer=None):
    """Probe all four substance dimensions for one campaign, in place. Cached by the
    source-link set (unchanged campaigns are not re-probed). Never raises; accessibility
    'none' adds the footage_inaccessible disqualifier so the campaign sinks."""
    links = rec.get("source_links") or []
    key = _source_key(links)
    cached = rec.get("footage_intake")
    if cached and cached.get("source_key") == key and cached.get("sources") is not None:
        return False  # unchanged — reuse cached probe

    cookies = getattr(cfg, "cookies_from_browser", None)
    probes = []
    for u in links:
        probes.append(probe_source(u))
        if pacer is not None:
            pacer.page_delay()

    access = aggregate_access(probes, has_links=bool(links))
    content = classify_content_type(rec.get("rules_text"), probes)
    volume = footage_volume(probes)
    density = action_density(probes, cookies_from_browser=cookies)

    rec["footage_intake"] = {"source_key": key,
                             "probed_at": datetime.now(timezone.utc).isoformat(),
                             "sources": probes}
    rec["footage_access"] = access
    rec["content_type"] = content
    rec["footage_volume"] = volume
    rec["action_density"] = density

    # Accessibility is a hard disqualifier — runs first, sinks the campaign if nothing
    # is downloadable (this is the "lost days to an undownloadable VOD" guard).
    if access["status"] == "none":
        dq = rec.get("disqualifiers") or []
        if not any(d.get("code") == "footage_inaccessible" for d in dq):
            dq.append({"code": "footage_inaccessible",
                       "reason": "footage not accessible — " + access["reason"]})
        rec["disqualifiers"] = dq
        rec["disqualified"] = True
    return True


def probe_campaigns(campaigns, cfg, pacer):
    """Run substance intake on every non-disqualified active campaign (runtime is
    unlimited — coverage is the point). Cached; each campaign's failure is isolated and
    logged. Returns the count actually probed."""
    active = [c for c in campaigns
              if c.get("status") in ("scraped", "refreshed") and not c.get("disqualified")]
    print(f"  Footage intake: probing accessibility / content-type / volume / density "
          f"for {len(active)} campaign(s)"
          + ("" if yt_dlp_available() else " (yt-dlp absent → durations for channels and "
             "density will be UNKNOWN; HTTP probes still run)") + "...")
    probed = 0
    newly_dq = 0
    for c in active:
        try:
            if probe_campaign(c, cfg, pacer):
                probed += 1
                if c.get("disqualified"):
                    newly_dq += 1
        except Exception as e:
            c["footage_access"] = {"status": "unknown",
                                   "reason": f"intake failed: {type(e).__name__}: {e}"}
    if newly_dq:
        print(f"    {newly_dq} campaign(s) disqualified for inaccessible footage.")
    return probed


# =============================================================================
# Standalone CLI — probe a campaign's source links directly (no scout/browser).
#   python intake.py <url> [<url> ...]
# =============================================================================
def _cli(argv):
    if not argv:
        print("usage: python intake.py <source_url> [<source_url> ...]")
        return 2
    rec = {"rules_text": "", "source_links": list(argv)}
    class _Cfg:
        cookies_from_browser = None
    probe_campaign(rec, _Cfg(), pacer=None)
    print(json.dumps({k: rec[k] for k in ("footage_access", "content_type",
                                          "footage_volume", "action_density")},
                     indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
