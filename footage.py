"""Footage probe via yt-dlp metadata only. NO downloads, ever.

For the top-N campaigns by pre-score, any YouTube/Twitch/Kick source channel is
inspected with `yt-dlp --dump-json --flat-playlist --skip-download` to collect a
rough video count, total duration, and recent upload dates. Everything is
best-effort: missing yt-dlp or a failed probe yields None, never a crash.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys

from extract import CHANNEL_HOSTS


# yt-dlp resolution — robust across launch methods (see intake._resolve_ytdlp_cmd). shutil.which
# ONLY searches PATH, so launching Scout's venv python directly (not activated) reported "not
# found" and skipped the footage probe though the venv had yt-dlp. Prefer the importable module.
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


def _is_channel(url):
    return bool(url) and any(h in url for h in CHANNEL_HOSTS)


def probe_channel(url, timeout=90):
    """Metadata for one channel URL, or None on any problem."""
    if not yt_dlp_available():
        return None
    try:
        proc = subprocess.run(
            [*_YTDLP_CMD, "--dump-json", "--flat-playlist", "--skip-download", url],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None

    entries = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except Exception:
            continue
    if not entries:
        return None

    durations = [e.get("duration") for e in entries if isinstance(e.get("duration"), (int, float))]
    dates = sorted({e.get("upload_date") for e in entries if e.get("upload_date")}, reverse=True)
    return {
        "url": url,
        "video_count": len(entries),
        "total_duration_sec": sum(durations) if durations else None,
        "recent_upload_dates": dates[:5] if dates else [],
    }


def probe_campaigns(campaigns, top_n, pacer):
    """Run footage probes for the highest pre-scored campaigns, in place."""
    if not yt_dlp_available():
        print("  yt-dlp not found on PATH — skipping footage probe.")
        return
    ranked = sorted(
        [c for c in campaigns if c.get("status") in ("scraped", "refreshed")],
        key=lambda c: c.get("pre_score", 0),
        reverse=True,
    )[:top_n]

    for c in ranked:
        channels = [u for u in (c.get("source_links") or []) if _is_channel(u)]
        if not channels:
            continue
        stats = []
        for url in channels:
            info = probe_channel(url)
            if info:
                stats.append(info)
            pacer.page_delay()  # same human respect between probes
        if stats:
            c["footage_stats"] = stats
