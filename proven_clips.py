"""Proven-clips analysis — is THIS creator's content REPEATABLY clippable?

The question that matters for a zero-audience start is NOT "does this creator post
well on their own channel" — it's "when third parties clip this creator, do those
clips reliably go viral?" A creator whose dedicated clippers consistently pull big
numbers is fundamentally clippable; that's the signal a new clipper can ride.

So this module answers clippability through DEDICATED CLIPPER-ACCOUNT IDENTITY,
fully automatically, no human confirmation anywhere:

  1. DISCOVER clipper accounts — search YouTube (yt-dlp `ytsearch`) for accounts
     clipping this creator ("<creator> clips / shorts / clipper / daily"). TikTok/IG
     are best-effort bonus only (yt-dlp can't reliably search them; their absence
     never lowers confidence — a strong YouTube signal alone scores HIGH).
  2. AUTO-SCORE each candidate's legitimacy as a dedicated clipper of THIS creator:
     creator name in the handle (+strong), a high share of 15–90s short clips (+),
     repeatedly posting this creator (+). General edit/AMV/montage/compilation/tribute
     accounts are dropped outright. Above threshold → auto-trusted. No manual step.
  3. NOISE-FILTER clips inside trusted accounts: keep only 15–90s clips, drop
     edit/montage/amv/compilation/AI/best-of/top-10/tribute captions. Filtered counts
     are reported.
  4. AGGREGATE across trusted clippers — median views, consistency (many 10k–50k beats
     a lone 1M outlier), how many active clippers, and median views RELATIVE TO THE
     CLIPPERS' OWN follower counts (small clippers pulling big views = the content
     carries the clip). THIS aggregate is the primary clippability score.
  5. The creator's own channel is a SEPARATE, LOW-WEIGHT reference only.
  6. No trusted clipper accounts found → mark UNKNOWN and fail loud. Never fabricate.
  7. We deliberately DON'T try to match clips back to a specific content stream — a
     creator whose clippers consistently go viral is clippable regardless of source.

Cardinal rule, same as social.py / footage.py: NEVER invent a number. A blocked
platform, timeout, or parse miss leaves the field None. yt-dlp metadata ONLY — NO
video downloads. `analyze_clip_content` is a deliberate stub for a future vision pass.

Two layers, mirroring extract.py: PURE analysis functions (scoring/consistency/
template/candidate-legitimacy/noise-filter — unit-testable with no network) feeding
off a yt-dlp harvest+discovery layer that never raises.
"""
import copy
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from pathlib import Path

from extract import SOCIAL_HOSTS
from social import parse_count  # reuse the honest '1.2M' -> int parser  # noqa: F401

# Platforms we can harvest clips from. YouTube is PRIMARY (yt-dlp handles it
# reliably); tiktok/instagram are best-effort bonus.
CLIP_PLATFORMS = ("youtube", "tiktok", "instagram")

# What a "short clip" is. Enforced on the counted signal.
SHORT_MIN = 15.0
SHORT_MAX = 90.0

# Discovery / trust defaults (scout passes Config overrides).
DEFAULT_SEARCH_N = 20            # ytsearch depth per query
DEFAULT_MAX_ACCOUNTS = 8         # candidate channels to actually harvest (cost cap)
DEFAULT_TRUST_THRESHOLD = 0.5    # legitimacy score to auto-trust a clipper account
DEFAULT_MIN_CLIPPERS_HIGH = 2    # >= this many trusted clippers -> eligible for HIGH
DEFAULT_STRONG_SINGLE = 10       # ...or a single clipper with >= this many clips

# Harvest / scoring defaults.
DEFAULT_PER_CREATOR = 40         # recent videos to pull per channel (flat)
DEFAULT_ENRICH_TOP = 12          # enrich this many pooled clips with full metadata
DEFAULT_RELATIVE_MULTIPLE = 5.0  # elite bar: clip views >= N x the clipper's followers
DEFAULT_TEMPLATE_CLIPS = 15      # top-N clips the template patterns draw from
DEFAULT_MIN_FOR_CONFIDENCE = 5   # creator-own reference: clips for a HIGH reference

# Desirable clipper-handle words (substring match on concatenated handle/name).
CLIP_KEYWORDS = ("clip", "clips", "clipper", "clipz", "shorts", "daily",
                 "cuts", "moments", "highlights")
# Hard-exclude account words (a general edit/montage/AMV/multi-creator account).
# Substring match, so "creatoredits" is excluded too.
EXCLUDE_ACCOUNT_KEYWORDS = ("edit", "montage", "amv", "compilation", "tribute", "mashup")
# Clip-level caption noise (word-boundary regex so it won't false-fire inside words).
_NOISE_RE = re.compile(
    r"\b(edits?|montages?|amv|compilations?|tributes?|mashups?|"
    r"best[\s-]?of|top[\s-]?\d+|ai)\b", re.I,
)


# yt-dlp resolution — robust across launch methods (see intake._resolve_ytdlp_cmd for the full
# rationale). shutil.which ONLY searches PATH, so launching Scout's venv python directly (without
# activating) reported "yt-dlp not available" and SKIPPED proven-clips analysis even though the
# venv had yt-dlp. We prefer the importable module so it always runs under Scout's own venv.
def _resolve_ytdlp_cmd():
    override = os.environ.get("SCOUT_YTDLP")
    if override and (os.path.isfile(override) or shutil.which(override)):
        return [override]
    if importlib.util.find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"]
    bindir = os.path.dirname(sys.executable)
    for name in ("yt-dlp.exe", "yt-dlp"):
        cand = os.path.join(bindir, name)
        if os.path.isfile(cand):
            return [cand]
    exe = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    return [exe] if exe else None


_YTDLP_CMD = _resolve_ytdlp_cmd()


def yt_dlp_available():
    return _YTDLP_CMD is not None


# =============================================================================
# PURE math / stats helpers (no network, no Playwright — freely testable)
# =============================================================================
def _nums(vals):
    return [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]


def median(vals):
    xs = sorted(_nums(vals))
    if not xs:
        return None
    n = len(xs)
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0


def percentile(vals, p):
    """Nearest-rank-ish percentile (0..100). None on empty input."""
    xs = sorted(_nums(vals))
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    idx = int(round((p / 100.0) * (len(xs) - 1)))
    idx = max(0, min(len(xs) - 1, idx))
    return xs[idx]


def _clamp01(x):
    return max(0.0, min(1.0, x))


def _mean(vals):
    xs = _nums(vals)
    return sum(xs) / len(xs) if xs else None


def _log_scale(x, lo, hi):
    """Map x into 0..1 on a log scale between lo and hi (x<=lo ->0, x>=hi ->1)."""
    if not x or x <= 0 or lo <= 0 or hi <= lo:
        return 0.0
    return _clamp01((math.log10(x) - math.log10(lo)) / (math.log10(hi) - math.log10(lo)))


# =============================================================================
# PURE consistency scoring — used by BOTH the clipper aggregate and the
# (low-weight) creator-own reference. Consistency > peak, on purpose.
# =============================================================================
def clip_performance(clips, followers, *, relative_multiple=DEFAULT_RELATIVE_MULTIPLE,
                     min_for_confidence=DEFAULT_MIN_FOR_CONFIDENCE):
    """Score repeatable clippability from clips' view counts against a single audience
    baseline. Used for the creator-own REFERENCE (one channel, one follower count).

    Rewards a high MEDIAN relative to followers and a tight top-to-median spread, so a
    lone outlier can't carry the score. `score` is None (UNKNOWN) with no view data —
    we mark it, never guess.
    """
    views = _nums([c.get("views") for c in clips])
    n = len(views)
    base = {
        "score": None, "confidence": "UNKNOWN", "clip_count": n,
        "followers": int(followers) if followers else None,
        "median_views": None, "mean_views": None, "max_views": None, "min_views": None,
        "median_view_to_follower": None, "top_to_median_ratio": None,
        "share_exceeding_1x": None, "share_exceeding_5x": None,
        "relative_multiple": relative_multiple, "reason": None,
    }
    if n == 0:
        base["reason"] = "no view counts on any clip"
        return base

    med, mx, mn, mean = median(views), max(views), min(views), sum(views) / n
    tightness = _clamp01(med / mx) if mx > 0 else 0.0
    base.update(median_views=int(med), mean_views=int(mean), max_views=int(mx),
                min_views=int(mn), top_to_median_ratio=round(mx / med, 2) if med else None)

    if followers and followers > 0:
        r = med / followers
        share_1x = sum(1 for v in views if v >= followers) / n
        share_5x = sum(1 for v in views if v >= relative_multiple * followers) / n
        score_rel = _log_scale(r, 0.1, relative_multiple)
        hit = _clamp01(share_1x * 0.6 + share_5x * 1.5)
        score = _clamp01(0.50 * score_rel + 0.35 * hit + 0.15 * tightness)
        base.update(score=round(score, 4),
                    confidence="HIGH" if n >= min_for_confidence else "LOW",
                    median_view_to_follower=round(r, 3),
                    share_exceeding_1x=round(share_1x, 3),
                    share_exceeding_5x=round(share_5x, 3))
    else:
        score_abs = _log_scale(med, 1000, 100000)
        score = _clamp01(0.5 * tightness + 0.5 * score_abs)
        base.update(score=round(score, 4), confidence="LOW",
                    reason="follower count unavailable — scored on consistency + "
                           "absolute median only")
    return base


# =============================================================================
# PURE candidate legitimacy scoring + clip noise filtering
# =============================================================================
def _creator_tokens(name):
    """Significant lowercase tokens of a creator name (drop tiny stopword-ish bits)."""
    toks = [t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if len(t) >= 3]
    return toks or [t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if t]


def _name_hit(text, tokens, full=None):
    """Does `text` reference the creator (full name or any significant token)?"""
    t = (text or "").lower()
    if not t:
        return False
    if full and full.lower() in t:
        return True
    return any(tok in t for tok in tokens)


def score_candidate(candidate, harvest, creator_name, *,
                    trust_threshold=DEFAULT_TRUST_THRESHOLD):
    """AUTO-score one discovered account's legitimacy as a dedicated clipper of THIS
    creator. No human step. Returns a dict incl. `trusted` (bool) and `excluded`.

    Signals (all derived, none fabricated):
      - creator name in the handle/channel name (+strong)
      - `short_share`  — fraction of the account's clips that are 15–90s
      - `repost_share` — fraction of clip titles that name the creator
      - `clip_kw`      — a clip/clipper/shorts/daily word in the handle
    Hard exclude (dropped regardless of score): an edit/montage/AMV/compilation/
    tribute account (by handle) or one whose clips are mostly such edits (by caption).
    """
    tokens = _creator_tokens(creator_name)
    handle_text = " ".join(filter(None, [
        candidate.get("name"), candidate.get("handle"), harvest.get("uploader"),
    ])).lower()
    clips = harvest.get("clips") or []
    titles = [c.get("title") or "" for c in clips]

    # Hard exclusion — general edit/montage account, or mostly edit captions.
    excl_handle = any(kw in handle_text for kw in EXCLUDE_ACCOUNT_KEYWORDS)
    excl_share = _mean([1.0 if _NOISE_RE.search(t) else 0.0 for t in titles]) or 0.0
    if excl_handle or excl_share >= 0.5:
        return {"trusted": False, "excluded": True, "legitimacy": 0.0,
                "creator_in_handle": _name_hit(handle_text, tokens, creator_name),
                "repost_share": round(excl_share, 3), "short_share": None,
                "clip_kw": any(kw in handle_text for kw in CLIP_KEYWORDS),
                "reason": "excluded: general edit/montage/AMV/compilation/tribute account"}

    creator_hit = _name_hit(handle_text, tokens, creator_name)
    repost_share = _mean([1.0 if _name_hit(t, tokens, creator_name) else 0.0
                          for t in titles]) or 0.0
    durs = _nums([c.get("duration") for c in clips])
    short_share = (_mean([1.0 if SHORT_MIN <= d <= SHORT_MAX else 0.0 for d in durs])
                   if durs else 0.0)
    clip_kw = any(kw in handle_text for kw in CLIP_KEYWORDS)

    legitimacy = (0.45 * (1.0 if creator_hit else 0.0)
                  + 0.20 * (1.0 if clip_kw else 0.0)
                  + 0.20 * short_share
                  + 0.15 * min(1.0, repost_share * 2.0))
    associated = creator_hit or repost_share >= 0.4
    trusted = associated and legitimacy >= trust_threshold
    return {
        "trusted": bool(trusted), "excluded": False,
        "legitimacy": round(legitimacy, 3),
        "creator_in_handle": creator_hit,
        "repost_share": round(repost_share, 3),
        "short_share": round(short_share, 3),
        "clip_kw": clip_kw,
        "reason": None if trusted else (
            "not associated with creator" if not associated
            else f"legitimacy {legitimacy:.2f} < {trust_threshold}"),
    }


def filter_clips(clips, *, short_min=SHORT_MIN, short_max=SHORT_MAX):
    """Drop noise-caption clips and clips with a KNOWN duration outside 15–90s.
    Returns (kept, stats).

    Critical: yt-dlp's flat/search listings frequently OMIT per-clip duration, so a
    missing duration must NOT mean "excluded" (that would discard every clip and score
    everyone UNKNOWN). We keep unknown-duration clips — their view_count still drives
    the aggregate — and only drop a clip once we actually know its duration is out of
    range. The caller enriches the top clips' real durations first, so known long-form
    still gets excluded from the counted signal. Kept-but-unconfirmed clips are counted
    separately so the report stays honest.
    """
    kept, f_short, f_kw, kept_no_dur = [], 0, 0, 0
    for c in clips:
        text = f"{c.get('title') or ''} {c.get('description') or ''}"
        if _NOISE_RE.search(text):
            f_kw += 1
            continue
        d = c.get("duration")
        if isinstance(d, (int, float)):
            if not (short_min <= d <= short_max):
                f_short += 1
                continue
        else:
            kept_no_dur += 1  # unknown duration -> keep (honest), don't assume
        kept.append(c)
    return kept, {"kept": len(kept), "filtered_short": f_short,
                  "filtered_keyword": f_kw, "kept_no_duration": kept_no_dur}


# =============================================================================
# PURE aggregate — the PRIMARY clippability score, across trusted clippers
# =============================================================================
def aggregate_clippability(trusted, *, relative_multiple=DEFAULT_RELATIVE_MULTIPLE,
                           min_clippers_high=DEFAULT_MIN_CLIPPERS_HIGH,
                           strong_single=DEFAULT_STRONG_SINGLE):
    """Pool 15–90s clips across trusted clipper accounts and score the creator's
    REPEATABLE clippability. Each clip is judged relative to ITS OWN account's follower
    count, so a small clipper pulling big views (the content carrying the clip) reads
    as strong — exactly the signal a zero-audience start wants.

    Consistency beats peak: a high pooled median + tight top-to-median + many clips
    clearing their account's audience score high; a lone 1M outlier does not.

    UNKNOWN (score None) when no trusted clipper produced a countable clip — we mark
    it and fail loud, never fabricate.
    """
    pooled = []
    for a in trusted:
        for c in (a.get("clips") or []):
            cc = dict(c)
            cc["account_followers"] = a.get("followers")
            cc["account"] = a.get("handle") or a.get("name")
            pooled.append(cc)
    n_clippers = sum(1 for a in trusted if (a.get("clips")))
    views = _nums([c.get("views") for c in pooled])
    n = len(views)

    block = {
        "score": None, "confidence": "UNKNOWN", "primary_signal": "clipper_accounts",
        "trusted_clippers": n_clippers, "clip_count": n,
        "median_views": None, "mean_views": None, "max_views": None,
        "top_to_median_ratio": None,
        "median_view_to_follower": None, "share_exceeding_1x": None,
        "share_exceeding_5x": None, "relative_multiple": relative_multiple,
        "reason": None,
    }
    if n == 0:
        block["reason"] = ("no trusted clipper accounts with countable 15–90s clips"
                           if n_clippers == 0 else
                           "trusted clippers found but no clips survived the 15–90s "
                           "+ noise filters")
        return block

    med, mx, mean = median(views), max(views), sum(views) / n
    tightness = _clamp01(med / mx) if mx > 0 else 0.0
    block.update(median_views=int(med), mean_views=int(mean), max_views=int(mx),
                 top_to_median_ratio=round(mx / med, 2) if med else None)

    ratios = [c["views"] / c["account_followers"] for c in pooled
              if isinstance(c.get("views"), (int, float))
              and isinstance(c.get("account_followers"), (int, float))
              and c["account_followers"] > 0]
    if ratios:
        med_ratio = median(ratios)
        share_1x = sum(1 for r in ratios if r >= 1.0) / len(ratios)
        share_5x = sum(1 for r in ratios if r >= relative_multiple) / len(ratios)
        score_rel = _log_scale(med_ratio, 0.1, relative_multiple)
        hit = _clamp01(share_1x * 0.6 + share_5x * 1.5)
        perf = 0.50 * score_rel + 0.30 * hit + 0.20 * tightness
        block.update(median_view_to_follower=round(med_ratio, 3),
                     share_exceeding_1x=round(share_1x, 3),
                     share_exceeding_5x=round(share_5x, 3))
    else:
        # No clipper follower counts anywhere — fall back to absolute median.
        score_abs = _log_scale(med, 1000, 100000)
        perf = 0.5 * score_abs + 0.5 * tightness
        block["reason"] = ("clipper follower counts unavailable — scored on absolute "
                           "median + consistency")

    # More independent clippers on the same creator = stronger, saturating bonus.
    clipper_bonus = min(0.10, 0.03 * max(0, n_clippers - 1))
    block["score"] = round(_clamp01(perf + clipper_bonus), 4)

    if n_clippers >= min_clippers_high and n >= 5:
        block["confidence"] = "HIGH"
    elif n_clippers >= 1 and n >= strong_single:
        block["confidence"] = "HIGH"   # a strong single YouTube clipper is enough
    else:
        block["confidence"] = "LOW"
    return block


# =============================================================================
# PURE template extraction — winning-clip patterns (metadata level)
# =============================================================================
_QUESTION_STARTS = {
    "how", "why", "what", "when", "who", "which", "where", "whose", "whom",
    "can", "could", "did", "do", "does", "is", "are", "was", "were", "should",
    "would", "will", "have", "has", "am",
}
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\U00002190-\U000021FF]"
)
_HASHTAG_RE = re.compile(r"#\w+")


def _is_question(text):
    if not text:
        return False
    if "?" in text:
        return True
    first = text.strip().split()
    return bool(first) and first[0].lower().strip("#@.,!") in _QUESTION_STARTS


def _cap_style(text):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return None
    upper_share = sum(c.isupper() for c in letters) / len(letters)
    if upper_share > 0.7:
        return "ALLCAPS"
    words = re.findall(r"[A-Za-z][A-Za-z']*", text)
    if words and sum(w[0].isupper() for w in words) / len(words) > 0.6:
        return "Title"
    if upper_share < 0.15:
        return "lower"
    return "Mixed"


def analyze_text_patterns(texts):
    """Caption / title style from a list of strings. None if there's nothing to read."""
    texts = [t.strip() for t in texts if t and t.strip()]
    if not texts:
        return None
    char_lens = [len(t) for t in texts]
    word_counts = [len(t.split()) for t in texts]
    q_share = sum(_is_question(t) for t in texts) / len(texts)
    styles = Counter(s for s in (_cap_style(t) for t in texts) if s)
    hashtags = [len(_HASHTAG_RE.findall(t)) for t in texts]
    emoji_share = sum(bool(_EMOJI_RE.search(t)) for t in texts) / len(texts)
    if q_share >= 0.6:
        structure = "mostly questions"
    elif q_share <= 0.2:
        structure = "mostly statements"
    else:
        structure = "mixed questions & statements"
    return {
        "sample": len(texts), "structure": structure,
        "question_share": round(q_share, 2),
        "median_char_len": int(median(char_lens)),
        "avg_word_count": round(sum(word_counts) / len(word_counts), 1),
        "dominant_capitalization": styles.most_common(1)[0][0] if styles else None,
        "avg_hashtags": round(sum(hashtags) / len(hashtags), 2),
        "emoji_share": round(emoji_share, 2),
        "examples": texts[:3],
    }


def analyze_lengths(durations):
    ds = [d for d in _nums(durations) if d > 0]
    if not ds:
        return None
    med, p25, p75 = median(ds), percentile(ds, 25), percentile(ds, 75)
    return {"count": len(ds), "median_sec": round(med, 1),
            "p25_sec": round(p25, 1), "p75_sec": round(p75, 1),
            "typical_range": f"{int(round(p25))}s–{int(round(p75))}s"}


def _parse_upload_date(s):
    if not s:
        return None
    s = str(s)
    if not re.fullmatch(r"\d{8}", s):
        return None
    try:
        return datetime.strptime(s, "%Y%m%d").date()
    except ValueError:
        return None


def analyze_cadence(upload_dates):
    ds = sorted({d for d in (_parse_upload_date(x) for x in upload_dates) if d})
    if len(ds) < 2:
        return None
    gaps = [(ds[i + 1] - ds[i]).days for i in range(len(ds) - 1)]
    med_gap = median(gaps)
    return {
        "clips_with_dates": len(ds), "first": ds[0].isoformat(), "last": ds[-1].isoformat(),
        "span_days": (ds[-1] - ds[0]).days,
        "median_gap_days": round(med_gap, 1) if med_gap is not None else None,
        "posts_per_week": round(7.0 / med_gap, 2) if med_gap and med_gap > 0 else None,
    }


def _clip_caption(clip):
    return clip.get("description") or clip.get("title")


def extract_template(clips, *, top_n=DEFAULT_TEMPLATE_CLIPS):
    """Winning-clip patterns from the top-`top_n` clips (by views). Metadata-level."""
    ranked = sorted([c for c in clips if isinstance(c.get("views"), (int, float))],
                    key=lambda c: c.get("views") or 0, reverse=True)
    top = ranked[:top_n] if ranked else clips[:top_n]
    if not top:
        return None
    return {
        "sample_clip_count": len(top),
        "drawn_from": "top clips by views" if ranked else "available clips (no view data)",
        "length": analyze_lengths([c.get("duration") for c in top]),
        "captions": analyze_text_patterns([_clip_caption(c) for c in top]),
        "titles": analyze_text_patterns([c.get("title") for c in top]),
        "cadence": analyze_cadence([c.get("upload_date") for c in top]),
        "content_analysis": None,  # FUTURE hook — see analyze_clip_content()
    }


# =============================================================================
# FUTURE HOOK — download + vision analysis of the top clips. NOT IMPLEMENTED.
# =============================================================================
def analyze_clip_content(clip, *, download_dir=None):
    """STUB for a future content-analysis pass — deliberately does nothing today.

    Intended future behavior: download the top clip (yt-dlp, actual video this time),
    run vision over it, and return on-screen-text / hook-style / cut-cadence /
    face-presence features to enrich the template. That is a downloads-and-vision
    capability and is explicitly OUT OF SCOPE now — implementing it means revisiting
    the hard constraints in CLAUDE.md. Returns None so callers degrade cleanly.
    """
    return None


def enrich_template_with_content(template, top_clips, *, download_dir=None):
    """Bolt-on point for the future vision pass. Today leaves content_analysis None."""
    if not template:
        return template
    analyses = [a for a in (analyze_clip_content(c, download_dir=download_dir)
                            for c in top_clips) if a is not None]
    template["content_analysis"] = analyses or None
    return template


# =============================================================================
# HARVEST + DISCOVERY layer — yt-dlp, METADATA ONLY, no downloads. Never raises.
# =============================================================================
def _run_ytdlp(args, timeout):
    try:
        proc = subprocess.run([*_YTDLP_CMD, *args, "--skip-download"],
                              capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout


def _parse_single_json(out):
    if not out:
        return None
    out = out.strip()
    try:
        return json.loads(out)
    except Exception:
        for line in out.splitlines():
            line = line.strip()
            if line:
                try:
                    return json.loads(line)
                except Exception:
                    continue
    return None


def classify_platform(url):
    u = (url or "").lower()
    for platform, hosts in SOCIAL_HOSTS.items():
        if any(h in u for h in hosts):
            return platform
    return None


def _harvest_targets(platform, url):
    """Candidate URLs to try, in order. Prefer a YouTube channel's /shorts tab."""
    if platform == "youtube":
        base = url.rstrip("/")
        if re.search(r"youtube\.com/(@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)$", base):
            return [base + "/shorts", base]
    return [url]


def _clip_url(platform, entry):
    u = entry.get("webpage_url") or entry.get("url")
    if not u and entry.get("id") and platform == "youtube":
        return f"https://www.youtube.com/watch?v={entry['id']}"
    return u


def _clip_from_entry(platform, entry):
    dur = entry.get("duration")
    return {
        "id": entry.get("id"), "platform": platform, "url": _clip_url(platform, entry),
        "title": entry.get("title"), "description": entry.get("description"),
        "views": entry.get("view_count"), "likes": entry.get("like_count"),
        "duration": dur if isinstance(dur, (int, float)) else None,
        "upload_date": entry.get("upload_date"),
    }


def harvest_channel(url, *, per_creator=DEFAULT_PER_CREATOR, timeout=120):
    """One channel's recent clips + follower count via yt-dlp (metadata only).

    Returns {url, platform, followers, uploader, clips, ok, reason}. No duration
    filtering here — callers apply the 15–90s + noise filter. Any failure yields
    ok=False with a reason and no clips, never a crash.
    """
    platform = classify_platform(url)
    out = {"url": url, "platform": platform, "followers": None, "uploader": None,
           "clips": [], "ok": False, "reason": None}
    if platform not in CLIP_PLATFORMS:
        out["reason"] = f"unsupported platform ({platform or 'unknown host'})"
        return out
    if not yt_dlp_available():
        out["reason"] = "yt-dlp not available"
        return out

    data = None
    for target in _harvest_targets(platform, url):
        data = _parse_single_json(_run_ytdlp(
            ["--dump-single-json", "--flat-playlist", "--playlist-end",
             str(per_creator), target], timeout))
        if data and (data.get("entries") or data.get("channel_follower_count")):
            break
    if not data:
        out["reason"] = "yt-dlp returned nothing (blocked, private, or bad URL)"
        return out

    followers = data.get("channel_follower_count") or data.get("subscriber_count")
    out["followers"] = int(followers) if isinstance(followers, (int, float)) else None
    out["uploader"] = data.get("uploader") or data.get("channel") or data.get("title")
    out["clips"] = [_clip_from_entry(platform, e)
                    for e in (data.get("entries") or []) if isinstance(e, dict)]
    if not out["clips"]:
        out["reason"] = "no clips with usable metadata (flat listing may lack views)"
        return out
    out["ok"] = True
    return out


def enrich_clips(clips, *, enrich_top=DEFAULT_ENRICH_TOP, pacer=None, timeout=90):
    """Fill likes / upload_date / caption on the top clips with a full metadata dump
    (still no download). Best-effort; a failed dump leaves fields None, never guessed."""
    if enrich_top <= 0 or not yt_dlp_available():
        return clips
    ranked = sorted([c for c in clips if isinstance(c.get("views"), (int, float))],
                    key=lambda c: c.get("views") or 0, reverse=True)[:enrich_top]
    for c in ranked:
        url = c.get("url")
        if not url:
            continue
        data = _parse_single_json(_run_ytdlp(["--dump-json", url], timeout))
        if data:
            if c.get("likes") is None and isinstance(data.get("like_count"), (int, float)):
                c["likes"] = int(data["like_count"])
            if not c.get("upload_date") and data.get("upload_date"):
                c["upload_date"] = data.get("upload_date")
            if not c.get("description") and data.get("description"):
                c["description"] = data.get("description")
            if c.get("duration") is None and isinstance(data.get("duration"), (int, float)):
                c["duration"] = data.get("duration")
            if c.get("views") is None and isinstance(data.get("view_count"), (int, float)):
                c["views"] = int(data["view_count"])
        if pacer is not None:
            pacer.page_delay()
    return clips


# --- clipper-account DISCOVERY (YouTube primary) -------------------------------
def _ytdlp_search(query, n, timeout=90):
    """yt-dlp `ytsearchN:` -> list of video entries (flat). [] on any failure."""
    data = _parse_single_json(_run_ytdlp(
        ["--dump-single-json", "--flat-playlist", f"ytsearch{n}:{query}"], timeout))
    return (data.get("entries") or []) if data else []


def _entry_channel(entry):
    """(dedupe_key, account_dict) for the channel that posted a search-result video."""
    url = entry.get("channel_url") or entry.get("uploader_url")
    cid = entry.get("channel_id")
    if not url and cid:
        url = f"https://www.youtube.com/channel/{cid}"
    name = entry.get("channel") or entry.get("uploader")
    handle = entry.get("uploader_id") or cid
    key = (url or cid or name)
    if not key:
        return None, None
    return key, {"url": url, "name": name, "handle": handle, "platform": "youtube"}


def discover_youtube_clippers(creator_name, *, search_n=DEFAULT_SEARCH_N,
                              max_accounts=DEFAULT_MAX_ACCOUNTS, pacer=None):
    """Search YouTube for accounts clipping this creator; return candidate channels
    ranked by how many search hits they got (more hits = more relevant). Best-effort."""
    queries = [f"{creator_name} clips", f"{creator_name} shorts",
               f"{creator_name} clipper", f"{creator_name} daily clips"]
    tally = {}
    for q in queries:
        for e in _ytdlp_search(q, search_n):
            if not isinstance(e, dict):
                continue
            key, acc = _entry_channel(e)
            if not key or not acc.get("url"):
                continue
            t = tally.setdefault(key, {**acc, "hit_count": 0, "sample_titles": []})
            t["hit_count"] += 1
            if e.get("title") and len(t["sample_titles"]) < 5:
                t["sample_titles"].append(e["title"])
        if pacer is not None:
            pacer.page_delay()
    ranked = sorted(tally.values(), key=lambda a: a["hit_count"], reverse=True)
    return ranked[:max_accounts]


# =============================================================================
# Per-creator orchestration: discover -> trust -> aggregate (+ own reference)
# =============================================================================
def _slim_clip(c):
    return {k: c.get(k) for k in ("platform", "url", "title", "views", "likes",
                                  "duration", "upload_date", "account")}


def _shorts_only(clips, max_sec=SHORT_MAX):
    """Reference-side filter: keep short clips (known duration <= max). Loose — the
    creator-own channel is only a low-weight reference."""
    return [c for c in clips if isinstance(c.get("duration"), (int, float))
            and c["duration"] <= max_sec] or clips


def _debug_dump_clips(candidate, harvest, is_trusted, sample=8):
    """Print the raw yt-dlp fields per clip for one account, so what the harvest
    actually returns (duration / view_count / title) is visible vs what the filter
    expects. Diagnostic only."""
    who = candidate.get("name") or candidate.get("url")
    clips = harvest.get("clips") or []
    have_dur = sum(1 for c in clips if isinstance(c.get("duration"), (int, float)))
    have_views = sum(1 for c in clips if isinstance(c.get("views"), (int, float)))
    print(f"  [debug] {who} — trusted={is_trusted} · {len(clips)} clips · "
          f"{have_dur} with duration · {have_views} with view_count")
    for c in clips[:sample]:
        print(f"    [debug]   duration={c.get('duration')!r:>8}  "
              f"view_count={c.get('views')!r:>10}  title={(c.get('title') or '')[:60]!r}")


def analyze_creator_reference(own_channels, *, per_creator=DEFAULT_PER_CREATOR,
                              relative_multiple=DEFAULT_RELATIVE_MULTIPLE,
                              min_for_confidence=DEFAULT_MIN_FOR_CONFIDENCE, pacer=None):
    """Low-weight REFERENCE: the creator's own short-form performance. Not the primary
    signal. None if no own-channel clips are harvestable."""
    all_clips, followers_seen = [], []
    for ch in own_channels:
        url = ch.get("url") if isinstance(ch, dict) else ch
        res = harvest_channel(url, per_creator=per_creator)
        if res["ok"]:
            all_clips.extend(res["clips"])
            if res["followers"]:
                followers_seen.append(res["followers"])
        if pacer is not None:
            pacer.page_delay()
    if not all_clips:
        return None
    followers = max(followers_seen) if followers_seen else None
    ref = clip_performance(_shorts_only(all_clips), followers,
                           relative_multiple=relative_multiple,
                           min_for_confidence=min_for_confidence)
    ref["basis"] = "creator-own channel (low-weight reference)"
    return ref


def analyze_creator_clippers(creator_name, own_channels=None, *, seed_accounts=None,
                             search_n=DEFAULT_SEARCH_N, max_accounts=DEFAULT_MAX_ACCOUNTS,
                             per_creator=DEFAULT_PER_CREATOR, enrich_top=DEFAULT_ENRICH_TOP,
                             relative_multiple=DEFAULT_RELATIVE_MULTIPLE,
                             trust_threshold=DEFAULT_TRUST_THRESHOLD,
                             min_clippers_high=DEFAULT_MIN_CLIPPERS_HIGH,
                             strong_single=DEFAULT_STRONG_SINGLE,
                             template_clips=DEFAULT_TEMPLATE_CLIPS, pacer=None,
                             debug=False):
    """Fully-automatic clippability for one creator via dedicated clipper accounts.

    Discovers clipper channels (YouTube primary), auto-scores/trusts them, noise-filters
    their clips, and aggregates the signal. `own_channels` feed only the low-weight
    reference; `seed_accounts` (known clip-farm URLs) are evaluated as candidates too.
    Returns {clippability, template, trusted, notes}. UNKNOWN when no clipper is trusted.

    For each TRUSTED account we enrich the top clips' real durations (per-video dump)
    BEFORE filtering, because flat/search listings usually omit duration — otherwise
    every clip would look "unknown duration". `debug=True` prints the raw yt-dlp fields
    (duration / view_count / title) for trusted accounts so the pipeline is inspectable.
    """
    notes = ["YouTube is the primary discovery source; TikTok/Instagram clipper search "
             "is not reliably supported by yt-dlp and is treated as optional bonus — "
             "its absence does not lower confidence."]
    candidates = discover_youtube_clippers(
        creator_name, search_n=search_n, max_accounts=max_accounts, pacer=pacer)
    # Fold in any known clip-farm seeds (still auto-scored, never blindly trusted).
    for s in (seed_accounts or []):
        url = s.get("url") if isinstance(s, dict) else s
        if url and not any(c.get("url") == url for c in candidates):
            candidates.append({"url": url, "name": None, "handle": None,
                               "platform": classify_platform(url), "hit_count": 0,
                               "sample_titles": []})

    evaluated, trusted = [], []
    for cand in candidates:
        harvest = harvest_channel(cand["url"], per_creator=per_creator)
        if pacer is not None:
            pacer.page_delay()
        if not harvest["ok"]:
            evaluated.append({"url": cand["url"], "name": cand.get("name"),
                              "trusted": False, "excluded": False,
                              "reason": harvest["reason"]})
            continue
        sc = score_candidate(cand, harvest, creator_name, trust_threshold=trust_threshold)
        is_trusted = sc["trusted"] and not sc["excluded"]

        # For trusted accounts, fetch real durations (+likes/captions/dates) on the
        # top clips BEFORE filtering — flat listings omit duration, so without this the
        # 15–90s filter can't confirm anything and the account contributes nothing.
        if is_trusted:
            enrich_clips(harvest["clips"], enrich_top=enrich_top, pacer=pacer)
        if debug:
            _debug_dump_clips(cand, harvest, is_trusted)

        kept, stats = filter_clips(harvest["clips"])
        rec = {
            "url": cand["url"], "name": cand.get("name") or harvest.get("uploader"),
            "handle": cand.get("handle"), "platform": harvest["platform"],
            "followers": harvest["followers"], "raw_clip_count": len(harvest["clips"]),
            "legitimacy": sc["legitimacy"], "trusted": sc["trusted"],
            "excluded": sc["excluded"], "creator_in_handle": sc["creator_in_handle"],
            "repost_share": sc["repost_share"], "short_share": sc["short_share"],
            "filtered": stats, "reason": sc["reason"],
        }
        if is_trusted and kept:
            trusted.append({**rec, "clips": kept})
        elif is_trusted and not kept:
            rec["reason"] = "trusted but all clips filtered out (noise captions / "
            rec["reason"] += "known duration outside 15–90s)"
        evaluated.append(rec)

    pooled = [c for a in trusted for c in a["clips"]]

    block = aggregate_clippability(trusted, relative_multiple=relative_multiple,
                                   min_clippers_high=min_clippers_high,
                                   strong_single=strong_single)
    block["candidates_evaluated"] = len(candidates)
    block["clippers"] = evaluated
    block["clip_platforms"] = sorted({a["platform"] for a in trusted}) or ["youtube"]
    block["notes"] = notes

    # Low-weight creator-own reference (shown; barely nudges the score).
    reference = analyze_creator_reference(
        own_channels or [], per_creator=per_creator,
        relative_multiple=relative_multiple, pacer=pacer)
    block["creator_reference"] = reference
    if block["score"] is not None and reference and reference.get("score") is not None:
        block["score_clipper_only"] = block["score"]
        block["score"] = round(0.9 * block["score"] + 0.1 * reference["score"], 4)

    template = extract_template(pooled, top_n=template_clips) if pooled else None
    block["top_clips"] = [_slim_clip(c) for c in sorted(
        pooled, key=lambda c: c.get("views") or 0, reverse=True)[:template_clips]]
    return {"clippability": block, "template": template, "trusted": trusted, "notes": notes}


# =============================================================================
# Scout wiring
# =============================================================================
def _load_clip_farms(path):
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def gather_own_channels(campaign):
    """The creator's OWN social channels (reference only): source handles + source_links
    on a clip platform. De-duped."""
    urls = {}

    def add(u, platform=None):
        if not u:
            return
        platform = platform or classify_platform(u)
        if platform in CLIP_PLATFORMS and u not in urls:
            urls[u] = {"url": u, "platform": platform}

    src = campaign.get("source") or {}
    for h in src.get("handles") or []:
        add(h.get("url"), h.get("platform"))
    for link in campaign.get("source_links") or []:
        add(link)
    return list(urls.values())


def gather_clip_farm_seeds(campaign, farms):
    """Known clip-farm accounts to evaluate as candidate clippers (still auto-scored)."""
    seeds = []
    for u in (farms.get("by_campaign_id") or {}).get(campaign.get("id"), []):
        seeds.append(u)
    src = campaign.get("source") or {}
    creator = (campaign.get("creator") or src.get("name") or "").lower()
    for key, extra in (farms.get("by_creator") or {}).items():
        if key and creator and key.lower() in creator:
            seeds.extend(extra)
    return seeds


def _creator_name_for(campaign):
    """Best creator name to search clippers for: explicit creator, else source name,
    else a readable handle derived from the campaign's source links (e.g. a youtube/
    tiktok @handle). Returns None only when truly nothing is identifiable."""
    src = campaign.get("source") or {}
    name = campaign.get("creator") or src.get("name")
    if name:
        return name
    for h in src.get("handles") or []:
        handle = h.get("handle")
        if handle:
            return handle.lstrip("@").replace("_", " ").replace("-", " ").strip() or None
    for link in campaign.get("source_links") or []:
        if classify_platform(link) in CLIP_PLATFORMS:
            m = re.search(r"/@?([A-Za-z0-9_.-]{2,40})", link)
            if m:
                return m.group(1).replace("_", " ").replace("-", " ").strip() or None
    return None


def _unk_block(reason):
    return {"score": None, "confidence": "UNKNOWN", "primary_signal": "clipper_accounts",
            "trusted_clippers": 0, "clip_count": 0, "clippers": [], "reason": reason,
            "unk_reason": reason}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


_NORM_RE = re.compile(r"[^a-z0-9]+")


def _norm_creator(name):
    """Normalized creator key for caching + dedupe across campaigns/runs. None if empty."""
    key = _NORM_RE.sub(" ", (name or "").lower()).strip()
    return key or None


def _clip_viable(c, cfg):
    """Lightweight VIABILITY floor: is this survivor worth the expensive clipper discovery?
    Skip campaigns with no budget left or flagged below-minimum-payout (a normal clip earns
    nothing) — they keep clippability UNKNOWN/neutral and are still ranked, we just don't
    spend yt-dlp effort on them. UNKNOWN budget is kept (never skipped on missing data)."""
    if not getattr(cfg, "clips_viability_floor", True):
        return True
    rem = c.get("budget_remaining_fraction")
    floor = getattr(cfg, "prefilter_min_budget_remaining", 0.0)
    if rem is not None and rem <= floor:
        return False
    if c.get("high_minimum"):
        return False
    return True


# --- cross-run creator cache ---------------------------------------------------
def _load_cache(path):
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        entries = data.get("creators", data) if isinstance(data, dict) else {}
        return entries if isinstance(entries, dict) else {}
    except Exception:
        return {}


def _write_cache(path, cache):
    payload = {
        "generated_at": _now_iso(),
        "note": "Cross-run creator -> repeatable-clippability cache. Recurring creators are "
                "reused within their TTL instead of re-running yt-dlp discovery. Scored "
                "results live longer than UNKNOWN ones (which are retried sooner).",
        "count": len(cache), "creators": cache,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def _cache_age_days(entry):
    ts = (entry or {}).get("cached_at")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0


def _cache_fresh(entry, cfg):
    """A SCORED creator result is reused for clips_cache_max_age_days; an UNKNOWN/failed one
    only for the shorter clips_cache_unknown_age_days (so transient blocks get retried)."""
    if not entry or not entry.get("block"):
        return False
    age = _cache_age_days(entry)
    if age is None:
        return False
    scored = entry["block"].get("score") is not None
    max_age = (cfg.clips_cache_max_age_days if scored
               else cfg.clips_cache_unknown_age_days)
    return age <= max_age


def _seed_cache_from_campaigns(cache, campaigns):
    """Seed the run cache from clippability already carried on campaign records (prior runs),
    so a recurring creator is reused even when the cache file is absent. Only real (scored)
    blocks are seeded, dated by when the campaign was last scraped/refreshed so the TTL still
    applies. Never overwrites a fresher on-disk cache entry."""
    for c in campaigns:
        blk = c.get("repeatable_clippability")
        if not blk or blk.get("score") is None:
            continue
        key = _norm_creator(_creator_name_for(c))
        if not key:
            continue
        cached_at = (blk.get("cached_at") or c.get("refreshed_at")
                     or c.get("scraped_at") or _now_iso())
        existing = cache.get(key)
        if existing and (existing.get("cached_at") or "") >= cached_at:
            continue
        cache[key] = {"creator": _creator_name_for(c), "cached_at": cached_at,
                      "block": blk, "template": blk.get("template"),
                      "trusted": blk.get("trusted_clippers") or []}


def _finalize_block(result):
    block = result["clippability"]
    block["template"] = result["template"]
    if block.get("score") is None and not block.get("unk_reason"):
        block["unk_reason"] = block.get("reason") or "no trusted clipper accounts found"
    return block


def _analyze_creator_group(group, cfg):
    """Run the full clipper discovery/aggregate for ONE creator (shared by every campaign
    with that creator). Runs on a worker thread — NO pacer (off-Whop, parallel, uncapped)."""
    result = analyze_creator_clippers(
        group["name"],
        own_channels=group["own"] or [],
        seed_accounts=group["seeds"],
        search_n=cfg.clipper_search_n, max_accounts=cfg.clipper_max_accounts,
        per_creator=cfg.clips_per_creator, enrich_top=cfg.clips_enrich_top,
        relative_multiple=cfg.clips_relative_multiple,
        trust_threshold=cfg.clipper_trust_threshold,
        min_clippers_high=cfg.clipper_min_for_high,
        strong_single=cfg.clipper_strong_single_clips,
        template_clips=getattr(cfg, "clips_template_clips", DEFAULT_TEMPLATE_CLIPS),
        pacer=None,
    )
    block = _finalize_block(result)
    trusted = [{k: a.get(k) for k in ("url", "name", "handle", "followers", "legitimacy")}
               for a in result["trusted"]]
    return {"block": block, "template": result["template"], "trusted": trusted}


def probe_campaigns(campaigns, cfg, pacer):
    """Fill each campaign's `repeatable_clippability` from discovered clipper accounts and
    write winning-clip templates to `cfg.template_path`. In place.

    Analyzed set = VIABLE survivors only (scraped/refreshed, not disqualified, past the
    `_clip_viable` floor) — the expensive discovery isn't spent on marginal campaigns.
    Clippability is a CREATOR property, so campaigns are grouped by creator and each creator
    is discovered ONCE per run (dedupe) and cached across runs (recurring creators reused
    within their TTL — never re-fetched). Fetches run on a small thread pool (off-Whop yt-dlp,
    no pacing) with a per-creator wall-clock timeout so one stuck lookup can't hang the phase.
    Progress is logged as "analyzing X/Y". Every UNKNOWN records a specific `unk_reason`
    (no creator name / timed out / no clipper accounts / failed) — never a silent guess.
    """
    if not yt_dlp_available():
        print("  yt-dlp not found on PATH — skipping proven-clips analysis.")
        return {}

    farms = _load_clip_farms(getattr(cfg, "clip_farms_path", "clip_farms.json"))
    active = [c for c in campaigns
              if c.get("status") in ("scraped", "refreshed")
              and not c.get("disqualified") and _clip_viable(c, cfg)]
    ranked = sorted(active, key=lambda c: c.get("pre_score", 0), reverse=True)
    if not getattr(cfg, "clips_analyze_all", True):
        ranked = ranked[:cfg.clips_top_n]

    cache = _load_cache(cfg.clips_cache_path)
    _seed_cache_from_campaigns(cache, campaigns)
    templates = _load_templates(cfg.template_path)

    # Group viable survivors by creator. No-creator campaigns resolve to UNKNOWN immediately.
    by_creator, no_creator = {}, 0
    for c in ranked:
        name = _creator_name_for(c)
        key = _norm_creator(name)
        if not key:
            c["repeatable_clippability"] = _unk_block(
                "no creator name extractable from brief or source links")
            no_creator += 1
            continue
        g = by_creator.get(key)
        if g is None:
            g = by_creator[key] = {"name": name, "campaigns": [], "own": None, "seeds": []}
        g["campaigns"].append(c)
        if g["own"] is None:
            g["own"] = gather_own_channels(c)
        g["seeds"].extend(gather_clip_farm_seeds(c, farms))

    to_fetch = {k: g for k, g in by_creator.items() if not _cache_fresh(cache.get(k), cfg)}
    reused = len(by_creator) - len(to_fetch)
    workers = max(1, getattr(cfg, "clips_workers", 4))
    timeout_s = getattr(cfg, "clips_campaign_timeout_s", 300)
    print(f"  Proven-clips: {len(ranked)} viable survivor(s) "
          f"({no_creator} without a creator) across {len(by_creator)} distinct creator(s); "
          f"{reused} reused from cache, {len(to_fetch)} to fetch "
          f"(pool={workers}, timeout={timeout_s}s/creator).")

    # Parallel fetch of the creators not already cached fresh. Each future is bounded by a
    # per-creator timeout; a stall is recorded UNKNOWN and skipped (its thread is abandoned,
    # bounded by yt-dlp's own subprocess timeouts) so the phase can never hang on one lookup.
    if to_fetch:
        ex = ThreadPoolExecutor(max_workers=workers)
        futures = {ex.submit(_analyze_creator_group, g, cfg): (k, g)
                   for k, g in to_fetch.items()}
        done, total = 0, len(futures)
        for fut, (k, g) in futures.items():
            done += 1
            try:
                res = fut.result(timeout=timeout_s)
                block = res["block"]
                cache[k] = {"creator": g["name"], "cached_at": _now_iso(),
                            "block": block, "template": res["template"],
                            "trusted": res["trusted"]}
                sc = block.get("score")
                status = (f"score {sc:.3f} [{block.get('confidence')}]"
                          if sc is not None else f"UNKNOWN ({block.get('unk_reason')})")
            except FutureTimeout:
                cache[k] = {"creator": g["name"], "cached_at": _now_iso(),
                            "block": _unk_block(f"analysis timed out after {timeout_s}s"),
                            "template": None, "trusted": []}
                status = "TIMEOUT"
            except Exception as e:
                cache[k] = {"creator": g["name"], "cached_at": _now_iso(),
                            "block": _unk_block(f"analysis failed: {type(e).__name__}: {e}"),
                            "template": None, "trusted": []}
                status = f"ERROR {type(e).__name__}"
            print(f"    analyzing {done}/{total} — {g['name']}: {status}")
        ex.shutdown(wait=False, cancel_futures=True)

    # Assign each creator's result to every campaign sharing it (deep-copied so campaigns
    # never share a mutable block), and write per-campaign templates.
    for k, g in by_creator.items():
        entry = cache.get(k) or {}
        block = entry.get("block") or _unk_block("analysis produced no result")
        block["cached_at"] = entry.get("cached_at")
        tmpl = entry.get("template")
        trusted = entry.get("trusted") or []
        for c in g["campaigns"]:
            c["repeatable_clippability"] = copy.deepcopy(block)
            templates[c["id"]] = {
                "campaign_id": c.get("id"), "campaign_name": c.get("name"),
                "creator": g["name"], "clippability": block,
                "trusted_clippers": trusted, "template": tmpl,
                "top_clips": block.get("top_clips"), "updated_at": _now_iso(),
            }

    _write_cache(cfg.clips_cache_path, cache)
    _write_templates(cfg.template_path, templates)
    return templates


def _load_templates(path):
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("templates", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_templates(path, templates):
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": "Winning-clip patterns per campaign, from DEDICATED CLIPPER ACCOUNTS of "
                "each creator (not the creator's own channel). Metadata-level only (no "
                "vision yet). Numbers are real or omitted, never guessed.",
        "count": len(templates), "templates": templates,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


# =============================================================================
# Standalone CLI — analyze a creator's clippability directly.
#   python proven_clips.py "Creator Name" [own_channel_url ...] [--debug]
# =============================================================================
def format_report(result, *, creator="creator"):
    cl = result["clippability"]
    lines = [f"Proven-clips report — {creator}", "=" * 62]
    score = cl.get("score")
    lines.append(f"  Clippability (clipper aggregate): "
                 f"{'%.3f' % score if score is not None else 'UNKNOWN'}  "
                 f"[confidence {cl.get('confidence')}]")
    if cl.get("reason"):
        lines.append(f"    note: {cl['reason']}")
    lines.append(f"  Trusted clipper accounts : {cl.get('trusted_clippers')} "
                 f"(of {cl.get('candidates_evaluated', 0)} evaluated)")
    lines.append(f"  Pooled short clips       : {cl.get('clip_count')}")
    lines.append(f"  Median / max views       : {cl.get('median_views')} / {cl.get('max_views')}")
    lines.append(f"  Median vs clipper-follow : {cl.get('median_view_to_follower')} "
                 f"(share ≥1x {cl.get('share_exceeding_1x')} / ≥5x {cl.get('share_exceeding_5x')})")
    lines.append(f"  Top-to-median spread     : {cl.get('top_to_median_ratio')}")
    for a in cl.get("clippers", []):
        mark = "TRUST" if a.get("trusted") else ("EXCL " if a.get("excluded") else "  -  ")
        lines.append(f"    [{mark}] {a.get('name') or a.get('url')}  "
                     f"legit={a.get('legitimacy')}  kept={(a.get('filtered') or {}).get('kept')}  "
                     f"{a.get('reason') or ''}")
    ref = cl.get("creator_reference")
    if ref:
        lines.append(f"  Creator-own reference    : {ref.get('score')} [{ref.get('confidence')}] "
                     f"(low weight)")
    tmpl = result.get("template") or {}
    length = tmpl.get("length") or {}
    caps = tmpl.get("captions") or {}
    cad = tmpl.get("cadence") or {}
    lines.append("-" * 62)
    lines.append(f"  Template (top {tmpl.get('sample_clip_count', 0)} clips): "
                 f"length {length.get('typical_range', '?')}, "
                 f"captions {caps.get('structure', '?')}, "
                 f"{cad.get('posts_per_week', '?')} posts/week")
    return "\n".join(lines)


def _cli(argv):
    debug = "--debug" in argv
    argv = [a for a in argv if a != "--debug"]
    if not argv:
        print('usage: python proven_clips.py "Creator Name" [own_channel_url ...] [--debug]')
        return 2
    if not yt_dlp_available():
        print("yt-dlp not found on PATH — install it to harvest clip metadata.")
        return 1
    creator = argv[0]
    own = [{"url": u, "platform": classify_platform(u)} for u in argv[1:]]
    result = analyze_creator_clippers(creator, own_channels=own, debug=debug)
    print(format_report(result, creator=creator))
    out = Path("proven_clips_result.json")
    out.write_text(json.dumps(result["clippability"], indent=2, ensure_ascii=False),
                   encoding="utf-8")
    print(f"\nFull result written to {out}")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
