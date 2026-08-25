"""Scout — a personal Whop Content Rewards research tool.

Runs standalone from a terminal:  python scout.py
It drives *your own* logged-in, visible Chromium at human pace, once a day, and
degrades to "not today, browse manually" the moment anything looks like a block.
See README.md for the full flow. No Claude involvement at runtime.

Flags:
  --force     ignore the once-daily (20h) guard
  --refresh   full re-scrape of every campaign, not just new ones (delta is default)
  --probe     log in, screenshot + dump a card and a detail page to confirm selectors
"""
import argparse
import json
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Campaign names carry emoji ("🔥 $5K Budget"); a Windows console defaults to cp1252 and
# UnicodeEncodeErrors the moment we print one (a latent crash that killed runs on emoji
# campaigns). Force UTF-8 on stdout/stderr the same way the clipper's common.py does. All
# FILE I/O in this project already passes encoding="utf-8"; this closes the console gap.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import extract
import footage as footage_mod
import categorize as categorize_mod
import intake as intake_mod
import language
import liveness as liveness_mod
import proven_clips as clips_mod
import report
import selectors as S
import social as social_mod
import strategic as strategic_mod
import scoring
from browser import Session
from pacing import Pacer
from scoring import composite_score, pre_score
from state import State


# =============================================================================
# CONFIG — every tunable lives here. Edit these defaults to taste.
# (Selectors are their own concern; they live in selectors.py.)
# =============================================================================
@dataclass(frozen=True)
class Config:
    # session / browser
    profile_dir: str = "./whop_profile"       # persistent login profile
    viewport: tuple = (1366, 768)             # normal desktop window

    # once-daily + session caps
    min_hours_between_runs: float = 20.0      # refuse if last run < this (unless --force)
    max_campaigns: int = 500                  # per-run detail cap
    max_minutes: float = 300.0                 # per-run wall-clock cap

    # click / interaction resilience — Whop's cross-origin iframe UI is frequently SLOWER
    # than a single click timeout, and most "failures" are transient slowness, not real
    # blocks. So: generous timeouts, retry slow clicks, and a high consecutive-failure
    # tripwire that only trips on a genuine block (captcha/login wall raise StopRun directly).
    click_timeout_ms: int = 28000             # card + dialog click timeout (was 8000)
    dialog_wait_ms: int = 15000               # wait for the detail dialog to render (was 8000)
    scroll_into_view_ms: int = 8000           # scroll a card into view before clicking
    click_retries: int = 3                    # attempts per click before it counts as a failure
    click_retry_wait: tuple = (1.5, 3.5)      # short random wait between click retries
    max_consecutive_failures: int = 8         # run-ending tripwire (real block, not slowness)

    # human pacing (seconds unless noted)
    base_delay: tuple = (1.5, 6.0)            # normal per-page think time
    fast_delay: tuple = (0.3, 0.8)            # fast click-through
    fast_chance: float = 0.20                 # 20% of delays are fast
    afk_every: tuple = (6, 15)                # campaigns between AFK breaks
    afk_break: tuple = (45, 150)              # AFK break length
    long_afk_chance: float = 0.10             # 10% of breaks are long
    long_afk_break: tuple = (180, 300)        # 3–5 min long break
    revisit_chance: float = 0.05              # 5% double-back to previous
    scroll_step: tuple = (300, 800)           # px per wheel tick
    scroll_pause: tuple = (0.5, 1.5)          # s between wheel ticks
    list_stable_rounds: int = 3               # scroll rounds w/ no new cards = "end"

    # pre-filter — a CHEAP gate that only spares session/browser budget on UNREACHED
    # campaigns that are genuinely unusable. It must NOT do the ranking's job: the composite
    # weighs pay-rate-vs-reach-vs-earnings tradeoffs, so a low headline rate on a huge,
    # clippable creator is a KEEP, not a drop. Floors are deliberately near "absurd" (e.g.
    # $0.01/1k, empty budget), not "unattractive". Known campaigns bypass this entirely
    # (see prefilter()). Applied only to values we actually parsed — nothing invisible.
    prefilter_min_pay_per_1k: float = 0.25    # $/1k floor (catch absurd rates only); 0 disables
    prefilter_min_budget_remaining: float = 0.05   # must be strictly greater (near-empty only)
    prefilter_required_platforms: tuple = ("tiktok", "shorts", "reels")

    # scoring / footage
    footage_top_n: int = 30                   # probe this many top campaigns
    social_top_n: int = 30                    # source-popularity lookup on this many
    # category-level ranking (on top of per-campaign scoring). A category's score aggregates
    # its member campaigns' composites; how = this knob: top5 | top3 | top10 | average | best.
    category_agg: str = "top5"
    # Groq-based categorizer (categorize.py): reads name + modal text + footage TITLES +
    # creator and assigns a primary category from the fixed set. Batched + content-hash cached.
    category_cache_path: str = "category_cache.json"  # per-content cache (skip Groq if unchanged)
    category_batch_size: int = 20             # campaigns per Groq call (70B handles 20; a 413
    #                                           auto-splits the batch — set ~10 for a static safety)
    category_model: str = None                # None -> $GROQ_MODEL or llama-3.3-70b-versatile
    category_batch_pause: float = 2.0         # seconds between Groq batches (stay under RPM)
    category_batch_retries: int = 1           # whole-batch retries before fallback (daily budget: fail fast)
    # Per-run cap on NEW (uncached) campaigns sent to Groq — protects the free-tier DAILY token
    # budget on a first big fill. The rest keep keyword_fallback and are picked up next run;
    # Scout runs every few days, so the board fills in over a couple runs within the free tier.
    category_max_new_per_run: int = 120

    # proven-clips / repeatable-clippability (the heavy new ranking lever).
    # Clippability is measured from AUTO-DISCOVERED dedicated clipper accounts of each
    # creator (YouTube-primary), not the creator's own channel — see proven_clips.py.
    # footage substance intake (intake.py)
    cookies_from_browser: str = None          # e.g. "chrome" — lets yt-dlp reach gated VODs (Kick)
    max_snapshot_history: int = 20            # per-run snapshots kept for cross-run projections
    my_performance_path: str = "my_performance.json"  # my recorded results (learning hook)
    clips_analyze_all: bool = True            # analyze EVERY VIABLE survivor (not a top-N)
    clips_top_n: int = 40                     # cap when clips_analyze_all is False (top by pre_score)
    # Only analyze survivors that clear a lightweight VIABILITY floor — budget still
    # remaining AND not flagged below-minimum-payout — so the expensive clipper discovery
    # isn't spent on clearly-marginal campaigns (they keep clippability UNKNOWN/neutral and
    # are still ranked). Set False to restore full-coverage analysis of every non-DQ survivor.
    clips_viability_floor: bool = True
    # Analysis-phase performance. The off-Whop yt-dlp pass is uncapped and hits YouTube (not
    # Whop), so it is safe to parallelize and needs no human pacing. Creator results are
    # cached across runs (recurring creators are never re-fetched within the TTL).
    clips_workers: int = 4                    # parallel yt-dlp creator lookups (small pool)
    clips_campaign_timeout_s: int = 300       # per-creator wall-clock cap (one stall can't hang the phase)
    clips_cache_path: str = "proven_clips_cache.json"  # cross-run creator -> clippability cache
    clips_cache_max_age_days: int = 14        # reuse a SCORED creator result this long
    clips_cache_unknown_age_days: int = 3     # retry an UNKNOWN/failed creator sooner
    clipper_search_n: int = 20                # ytsearch depth per discovery query
    clipper_max_accounts: int = 8             # candidate clipper channels to harvest (cost cap)
    clipper_trust_threshold: float = 0.5      # legitimacy score to auto-trust a clipper
    clipper_min_for_high: int = 2             # >= this many trusted clippers -> eligible HIGH
    clipper_strong_single_clips: int = 10     # ...or one clipper with >= this many clips
    clips_per_creator: int = 40               # recent videos to pull per channel (flat)
    clips_enrich_top: int = 12                # enrich this many pooled clips w/ full metadata
    clips_relative_multiple: float = 5.0      # elite bar: clip views >= N× the clipper's followers
    clips_template_clips: int = 15            # top-N clips the template patterns draw from

    # minimum-payout viability: a campaign whose first payout needs more than this
    # many views (min_payout / pay_per_1k * 1000) is flagged HIGH_MINIMUM and
    # deprioritized hard — a normal ~1k-view clip would earn nothing.
    min_payout_max_views: float = 1000.0

    # Non-English derank (English-only operation). A campaign whose text (name + rules +
    # modal + creator handle/description) reads as CLEARLY non-English gets its composite
    # multiplied by this — a heavy derank (~85% off), NOT a hard exclude. Detection is a
    # cheap offline stopword heuristic (language.py, NO Groq); ambiguous/short text fails
    # OPEN (factor 1.0), so English composites are left EXACTLY unchanged.
    nonenglish_penalty: float = 0.15

    # PAYOUT-HEALTH derank (scoring.payout_health). Scout scores POTENTIAL (budget/CPM/reach)
    # but is otherwise blind to whether a campaign ACTUALLY pays. A paying-dead trap — open a
    # while, meaningful submissions, yet ~$0 ever paid out (the SomSleep case) — is heavy-
    # deranked (composite × payout_dead_penalty), NOT excluded. A genuinely NEW campaign with
    # $0 paid is left untouched (fail-open on newness). Inputs: budget_paid/total (reliably
    # scraped) + the inline submissions/participants count (extract.parse_activity_count) +
    # days_active. NOTE: true launch date is NOT on the Whop page, so days_active is Scout's own
    # tracking age (a LOWER BOUND, 0 on first sight) — it can only EXONERATE a young campaign;
    # when age is unmeasured the submission count is the evidence the campaign is established.
    payout_health_enabled: bool = True
    payout_dead_penalty: float = 0.2         # composite × this for a paying-dead trap (~85% off)
    payout_min_age_days: float = 10.0        # under this TRACKED age, $0 paid is just "new" (no penalty)
    payout_min_submissions: int = 10         # need at least this many submissions to judge as dead
    payout_zero_dollars: float = 1.0         # paid <= $this counts as ~$0 (near-zero payout)
    payout_zero_fraction: float = 0.005      # OR spent fraction <= this counts as ~$0
    payout_healthy_spent_fraction: float = 0.02   # >= this of budget paid (with activity) = healthy
    payout_healthy_boost: float = 1.0        # factor for a healthy paying campaign (1.0 = untouched; >1.0 = boost)

    # Footage LINK-LIVENESS derank (liveness.py). A campaign whose footage links are all
    # DEAD/OFFLINE (removed video, empty/locked Drive folder, stream channel with no VODs)
    # is a waste of the clipper's time, so its composite is multiplied by this — a HEAVY
    # derank (~85% off), NOT a hard exclude (a probe can false-negative on rate-limiting/
    # outage, so the campaign stays visible far down). The check is metadata-only, cached
    # per LINK across runs (TTL), spaced, and FAILS OPEN — an errored/blocked/inconclusive
    # probe is UNKNOWN and never penalizes. Fires ONLY when sources are affirmatively dead
    # and NONE are alive. This is an off-Whop analysis probe (hits YouTube/Kick, not Whop).
    liveness_enabled: bool = True
    liveness_dead_penalty: float = 0.15
    liveness_cache_path: str = "liveness_cache.json"   # per-link verdict cache (dedupe + TTL)
    liveness_cache_max_age_days: int = 7               # reuse a live/dead link verdict this long
    liveness_probe_spacing: tuple = (1.0, 3.0)         # random sleep between FRESH probes (gentle)

    # footage-PRESENCE derank — distinct from liveness (is an existing link alive) and
    # accessibility (did the link respond): this asks whether the campaign exposes a PUBLIC,
    # downloadable footage link AT ALL (Drive folder / VOD or video URL / direct file). A
    # campaign whose footage is member-gated or absent (ZERO public footage links) can't be
    # clipped from the auto-run, so it's HEAVY-deranked (composite × no_footage_penalty), never
    # hard-excluded (a missed link shouldn't permanently kill it). FAILS OPEN: if the detail
    # section was never loaded (unscraped stub), footage presence is undeterminable -> no penalty.
    no_footage_penalty: float = 0.15

    # APPROVAL-RATE derank — every Whop campaign header shows an approval rate (the % of
    # submissions that get approved/paid). A KNOWN rate BELOW approval_rate_floor means most
    # clips are rejected unpaid (wasted effort), so the campaign is HEAVY-deranked (composite ×
    # approval_low_penalty), NOT excluded. FAILS OPEN: an approval rate that isn't shown/parsed
    # is UNKNOWN and never penalized; a known rate at/above the floor is untouched.
    approval_derank_enabled: bool = True
    approval_rate_floor: float = 65.0        # known approval rate below this -> derank
    approval_low_penalty: float = 0.2        # composite × this when approval < floor (~80% off)

    # SELF-SOURCED footage derank — some campaigns PROVIDE no footage; their rules/docs tell
    # clippers to find their OWN ("find your own footage", "use any footage of X", "we don't
    # provide footage"). Un-clippable by a footage-download pipeline, so HEAVY-deranked (composite
    # × self_sourced_penalty), NOT excluded. The instruction usually lives in the rules DOC, so
    # (when self_sourced_fetch_docs) Scout fetches the Google-Doc rules text for footage-less
    # gdoc campaigns and re-checks. FAILS OPEN: detection is high-precision, so a normal campaign
    # is never flagged; unfetched/ambiguous -> not flagged.
    self_sourced_enabled: bool = True
    self_sourced_penalty: float = 0.2        # composite × this when self-sourced (~80% off)
    self_sourced_fetch_docs: bool = True     # fetch gdoc rules text for footage-less campaigns

    # output paths
    state_path: str = "state.json"
    campaigns_path: str = "campaigns.json"
    summary_path: str = "campaigns_summary.md"
    errors_path: str = "errors.log"
    template_path: str = "campaign_template.json"   # winning-clip patterns (for the clipper)
    clip_farms_path: str = "clip_farms.json"        # optional known clip-farm accounts
    # DONE list — campaigns the CLIPPER has actually processed/exhausted. The clipper writes
    # this (or use --mark-done); ONLY these are skipped from future scraping AND ranking.
    # "Already scraped" is NOT done — a scraped campaign stays a ranked candidate until here.
    completed_path: str = "completed_campaigns.json"


CONFIG = Config()


# =============================================================================
class StopRun(Exception):
    """Raised to abort the whole run immediately (challenge / repeated failures)."""


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def log_error(path, url, exc):
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{_now_iso()}\t{url}\t{type(exc).__name__}: {exc}\n")


def safe_goto(page, url, *, wait_until="domcontentloaded", timeout=30000):
    """Navigate without ever raising.

    Manual login triggers Whop's own OAuth redirect chain; a programmatic goto that
    collides with it raises "Navigation interrupted by another navigation" (or
    ERR_ABORTED). None of that should be able to kill a run. Returns True if the
    navigation settled, False otherwise.
    """
    try:
        page.goto(url, wait_until=wait_until, timeout=timeout)
        return True
    except Exception as e:
        first_line = (str(e).splitlines() or [type(e).__name__])[0]
        print(f"    (navigation to {url} didn't settle: {first_line})")
        return False


# --- frame + selector helpers --------------------------------------------------
# The cards live inside a cross-origin app iframe (apps.whop.com). "scope" below is
# whatever we run selectors against — the app Frame in practice, the page as a
# fallback. Frame and Page share the .locator/.content/.url interface.
def pick_card_selector(scope):
    """The CARD candidate that currently matches the most elements in `scope`."""
    best, best_n = None, 0
    for sel in S.CARD:
        try:
            n = scope.locator(sel).count()
        except Exception:
            n = 0
        if n > best_n:
            best, best_n = sel, n
    return best, best_n


def get_app_frame_locator(page):
    """A FrameLocator for the Content Rewards app iframe. Used for ALL list
    queries/scrolling/clicks: it re-resolves the frame lazily on every call, so it
    survives the app re-rendering (unlike a captured Frame object). Returns None if
    the iframe element isn't present yet."""
    for sel in S.APP_IFRAME:
        try:
            if page.locator(sel).first.count() > 0:
                return page.frame_locator(sel)
        except Exception:
            continue
    return None


def get_app_frame(page, timeout=20):
    """Wait for and return the app iframe's Frame OBJECT (apps.whop.com). Used only
    where we need `.content()` (the probe dump) or `.url` (detail navigation
    tracking) — things a FrameLocator can't give. For querying elements, prefer
    get_app_frame_locator."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for fr in page.frames:
            try:
                if fr is not page.main_frame and S.APP_FRAME_URL_HINT in (fr.url or ""):
                    return fr
            except Exception:
                continue
        time.sleep(1.0)
    return None


def _iframe_box(page):
    for sel in S.APP_IFRAME:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                box = loc.bounding_box()
                if box:
                    return box
        except Exception:
            continue
    return None


def scroll_list(page, pacer, steps=None):
    """Human wheel-scroll INSIDE the app iframe (cursor parked over it), so the
    frame's own feed scrolls rather than the top page. Real wheel events, no JS."""
    steps = steps if steps is not None else random.randint(2, 5)
    box = _iframe_box(page)
    for _ in range(steps):
        try:
            if box:
                page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.mouse.wheel(0, random.randint(*pacer.scroll_step))
        except Exception:
            pass
        time.sleep(random.uniform(*pacer.scroll_pause))


def wait_for_feed(page, pacer, timeout=40, min_anchors=30):
    """The list is a client-rendered app inside a cross-origin iframe. Wait for that
    iframe, then poll INSIDE it (via a FrameLocator) until its campaign feed renders.

    Returns the app FrameLocator (whether or not cards were detected, so callers can
    still query/dump), or None if the iframe element never appeared. Selector-
    agnostic readiness: a CARD candidate matching several elements, or the frame's
    anchor count growing past baseline and stabilizing.
    """
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    # Scroll the top page down so the app iframe mounts past the hero and is in view.
    pacer.human_scroll(page, steps=4)

    # Wait for the iframe element itself to exist, then build a FrameLocator.
    fl_deadline = time.monotonic() + 20
    fl = None
    while time.monotonic() < fl_deadline:
        fl = get_app_frame_locator(page)
        if fl is not None:
            break
        time.sleep(1.0)
    if fl is None:
        print("    app iframe (apps.whop.com) not found yet.")
        return None

    deadline = time.monotonic() + timeout
    last, stable = -1, 0
    while time.monotonic() < deadline:
        sel, n = pick_card_selector(fl)
        if sel and n >= 3:
            return fl
        try:
            anchors = fl.locator("a").count()
        except Exception:
            anchors = 0
        if anchors >= min_anchors and anchors == last:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        last = anchors
        scroll_list(page, pacer, steps=2)
        time.sleep(1.2)
    return fl


def _app_id():
    m = re.search(r"(app_[A-Za-z0-9]+)", S.LIST_URL)
    return m.group(1) if m else ""


def _on_content_rewards(page):
    app_id = _app_id().lower()
    return bool(app_id and app_id in (page.url or "").lower())


def _find_banner(page):
    for sel in S.DISCOVER_BANNER:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


def navigate_to_list(page, pacer):
    """Reach the Content Rewards app the way a user would: open /discover/, then
    CLICK the Clipping/Content Rewards banner. A direct goto to the app URL gets
    bounced back to /discover/ by Whop's client router, so clicking is the reliable
    path — the direct goto is only a fallback. Never raises. Returns True if we end
    up on the content-rewards app page.
    """
    if _on_content_rewards(page):
        return True

    # Preferred path: discover page, then click the banner.
    if safe_goto(page, S.DISCOVER_URL):
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        banner = _find_banner(page)
        if banner is not None:
            pacer.maybe_hover(banner)
            pacer.page_delay()
            try:
                banner.click(timeout=5000)
                page.wait_for_load_state("domcontentloaded", timeout=10000)
                time.sleep(1.0)
            except Exception as e:
                first = (str(e).splitlines() or ["?"])[0]
                print(f"    (banner click didn't take: {first})")
        else:
            print("    (couldn't find the Content Rewards banner on /discover/)")

    # Fallback: a direct goto (may be bounced, but worth one try).
    if not _on_content_rewards(page):
        print("    falling back to direct navigation to the app URL...")
        safe_goto(page, S.LIST_URL)
    return _on_content_rewards(page)


# --- login ---------------------------------------------------------------------
# Nothing in this phase may ever crash the run. We never navigate programmatically
# while the user is mid-login (that's what collides with the OAuth redirect); we
# only navigate *after* their Enter, and even then via safe_goto with retries.
def _manual_login_prompt():
    print("\n" + "-" * 60)
    print("Log in manually in the browser window (email / Google / whatever).")
    print("Take all the time you need — Scout will NOT navigate while you do.")
    print("When you're fully logged in, come back here and press Enter.")
    print("(Scout never touches your credentials or automates the login form.)")
    print("-" * 60)
    input("Press Enter once logged in... ")


def _page_is_live(page):
    """True if the Playwright page is still open (not closed/detached)."""
    try:
        return page is not None and not page.is_closed()
    except Exception:
        return False


def _reacquire_page(session):
    """Adopt the live Whop page as session.page after manual login.

    The OAuth redirect chain can close or replace the tab we opened before the
    prompt, so the original session.page may be dead by the time the user presses
    Enter. Find the best currently-open page (Whop over a Google-login popup) and
    make it the page every downstream phase drives. If every tab was closed, open a
    fresh one so the run can still continue instead of crashing on a closed target.
    """
    live = session.live_page()
    if live is not None:
        if live is not session.page:
            print(f"    (re-acquired the active page after login: {live.url})")
        session.page = live
        return live
    print("    (all tabs were closed during login; opening a fresh Whop tab)")
    session.page = session.context.new_page()
    safe_goto(session.page, "https://whop.com/")
    return session.page


def _reach_list_or_wait(page, pacer):
    """Get to the Content Rewards list after login by clicking through /discover/.
    Retry a few times; if it still isn't reachable, show what we see and wait for
    another Enter — never exit."""
    while True:
        for attempt in range(1, 4):
            navigate_to_list(page, pacer)
            time.sleep(1.0)
            if not extract.is_login_wall(page) and _on_content_rewards(page):
                return
            print(f"    list not reachable yet (attempt {attempt}/3)...")
            time.sleep(2)
        print("\nCouldn't reach the Content Rewards list after 3 tries.")
        print(f"  Current URL : {page.url}")
        print(f"  Login wall? : {extract.is_login_wall(page)}")
        print("Finish logging in or navigate there manually, then press Enter to retry.")
        input("Press Enter to retry (or Ctrl+C to quit)... ")


def ensure_logged_in(session, first_run, pacer, always_prompt=False):
    page = session.page

    # always_prompt (used by --probe): never trust the login-wall heuristic — the
    # whole point of a probe is to dump the *logged-in* DOM, and the persistent
    # profile makes first_run False after the very first launch even when we're not
    # actually logged in. So force the manual flow every probe.
    if first_run or always_prompt:
        # Open a benign page and then get out of the way. No goto to the list until
        # the user has finished logging in and pressed Enter.
        safe_goto(page, "https://whop.com/")
        _manual_login_prompt()
        # OAuth may have closed/replaced/duplicated the tab — re-acquire the live
        # Whop page before touching it, or _reach_list_or_wait drives a dead page.
        page = _reacquire_page(session)
        _reach_list_or_wait(page, pacer)
        return

    # Returning session: click through to the list once. If we're logged out, fall
    # back to the same manual flow — still never exiting.
    navigate_to_list(page, pacer)
    time.sleep(1.0)
    if not extract.is_login_wall(page) and _on_content_rewards(page):
        return
    _manual_login_prompt()
    page = _reacquire_page(session)
    _reach_list_or_wait(page, pacer)


# --- probe ---------------------------------------------------------------------
def run_probe(session, pacer):
    # The final "press Enter to close" pause must ALWAYS fire — including the
    # no-cards-matched early return and any mid-probe error — or a fast probe closes
    # the window instantly and can kill the logged-in session. Hence try/finally.
    try:
        _probe_body(session, pacer)
    finally:
        print("\nProbe done. probe_list.html / probe_detail.html + the PNGs are saved.")
        print("Send them to Claude to fix selectors.py, then run `python scout.py`.")
        input("\nPress Enter to close the browser... ")


def _probe_body(session, pacer):
    page = session.page
    print("\nProbe: opening Discover and clicking through to Content Rewards...")
    on_list = navigate_to_list(page, pacer)
    print(f"  On content-rewards app page: {on_list} ({page.url})")
    print("  Waiting for the app iframe + feed to render...")
    fl = wait_for_feed(page, pacer)              # FrameLocator for queries
    frame = get_app_frame(page)                  # Frame object for .content()/.url
    scope = fl or page
    print(f"  App frame: {'found — ' + (frame.url or '') if frame else 'NOT FOUND'}")

    # Dump the FRAME's document — that's where the cards live. page.content() misses
    # them entirely (cross-origin iframe).
    if frame is not None:
        try:
            Path("probe_list.html").write_text(frame.content(), encoding="utf-8")
            print("  Saved probe_list.html (app-frame document)")
        except Exception as e:
            print(f"  (frame content unavailable, dumping top page: {e})")
            Path("probe_list.html").write_text(page.content(), encoding="utf-8")
    else:
        Path("probe_list.html").write_text(page.content(), encoding="utf-8")
        print("  Saved probe_list.html (top page — frame not found)")

    sel, n = pick_card_selector(scope)
    print(f"  Best card selector: {sel!r} matched {n} element(s).")
    if not sel or not n:
        print("  No cards matched yet, but probe_list.html holds the app-frame DOM.")
        print("  Send it to Claude to fix selectors.CARD, then re-probe.")
        return

    card = scope.locator(sel).first
    try:
        card.screenshot(path="probe_card.png")
        print("  Saved probe_card.png")
    except Exception as e:
        print(f"  (could not screenshot card: {e})")
    print("  Sample card extraction:")
    for k, v in extract.extract_card(card).items():
        print(f"    {k}: {v}")

    _probe_detail(page, pacer, card, frame)


def _probe_detail(page, pacer, card, list_frame):
    """Click a card and figure out whether detail opens in the FRAME or the TOP page
    (handle both), then dump whichever document holds the detail and sample-extract."""
    pre_page = page.url
    pre_frame = list_frame.url if list_frame else None
    clicked = False
    try:
        pacer.maybe_hover(card)
        pacer.page_delay()
        try:
            card.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        card.click(timeout=8000)
        clicked = True
    except Exception as e:
        print(f"  (normal click failed: {(str(e).splitlines() or ['?'])[0]}; retrying with force)")
        try:
            card.click(timeout=5000, force=True)
            clicked = True
        except Exception as e2:
            print(f"  (force click failed too: {(str(e2).splitlines() or ['?'])[0]})")
    time.sleep(2.5)

    post_frame_obj = get_app_frame(page)
    post_page = page.url
    post_frame = post_frame_obj.url if post_frame_obj else None
    navigated = (post_page != pre_page) or (post_frame != pre_frame)
    print(f"  Clicked: {clicked}")
    print(f"  After click — page: {pre_page} -> {post_page}")
    print(f"               frame: {pre_frame} -> {post_frame}")

    # Dump the best available detail document REGARDLESS, so probe_detail.html always
    # exists for selector work. Warn loudly if nothing actually navigated.
    if post_frame_obj is not None:
        detail_doc, detail_scope = post_frame_obj, (get_app_frame_locator(page) or page)
    else:
        detail_doc, detail_scope = page, page
    try:
        Path("probe_detail.html").write_text(detail_doc.content(), encoding="utf-8")
        page.screenshot(path="probe_detail.png", full_page=True)
        tag = "looks like a detail page" if navigated else "WARNING: nothing navigated — may still be the list"
        print(f"  Saved probe_detail.html / probe_detail.png ({tag})")
        print("  Sample detail extraction:")
        for k, v in extract.extract_detail(detail_scope).items():
            print(f"    {k}: {v}")
    except Exception as e:
        print(f"  (detail dump skipped: {(str(e).splitlines() or ['?'])[0]})")


# --- phase 1: list pass --------------------------------------------------------
def collect_cards(page, pacer, cfg, deadline_ts):
    print("Phase 1 — scanning the Content Rewards list (human scroll)...")
    # Guard the login->scrape handoff: the page we're about to scroll/scrape must be
    # open and actually on Whop. If OAuth closed the tab, or we're still parked on a
    # Google login URL, fail loud here rather than crashing deep inside mouse.wheel
    # ("Target ... has been closed") in wait_for_feed.
    if not _page_is_live(page):
        raise StopRun("the browser page was closed during login — couldn't find a "
                      "logged-in Whop discover page. Are you fully logged in?")
    if not _on_content_rewards(page):
        # One more attempt to reach the list from wherever login left us.
        navigate_to_list(page, pacer)
        time.sleep(1.0)
    if not _page_is_live(page) or extract.is_login_wall(page) or not _on_content_rewards(page):
        raise StopRun("couldn't find a logged-in Whop Content Rewards page to scrape "
                      f"(at {page.url if _page_is_live(page) else 'a closed tab'}) — "
                      "are you fully logged in?")
    if extract.is_challenge(page):
        raise StopRun("challenge on the list page")
    # Cards render inside the app iframe; wait_for_feed returns that FrameLocator.
    fl = wait_for_feed(page, pacer)
    scope = fl or page
    sel, n = pick_card_selector(scope)
    if not sel:
        print("  No cards found with current selectors. Run `python scout.py --probe`.")
        return []

    cards_by_id = {}
    stable = 0
    while stable < cfg.list_stable_rounds:
        locs = scope.locator(sel)
        new_found = 0
        for i in range(locs.count()):
            try:
                data = extract.extract_card(locs.nth(i))
            except Exception:
                continue
            cid = data.get("id")
            if cid and cid not in cards_by_id:
                cards_by_id[cid] = data
                new_found += 1
        print(f"  {len(cards_by_id)} unique cards seen...", end="\r")

        stable = stable + 1 if new_found == 0 else 0
        if time.monotonic() > deadline_ts:
            print("\n  Time budget reached during the list scan.")
            break
        scroll_list(page, pacer)

    print(f"\n  Phase 1 done: {len(cards_by_id)} unique cards.")
    return list(cards_by_id.values())


# --- pre-filter ----------------------------------------------------------------
def prefilter(cards, cfg, known_ids=None):
    known_ids = set(known_ids or ())
    survivors, skipped = [], []
    required = set(cfg.prefilter_required_platforms)
    for c in cards:
        # Known campaigns (already scraped, cached in campaigns.json) bypass the pre-filter
        # entirely. The pre-filter exists ONLY to spare browser/session budget on UNREACHED
        # campaigns, and a known campaign costs none (free card-level refresh). Demoting a
        # previously-ranked campaign out of the rankings via a budget-saving gate is a
        # regression — only the DONE list removes a scraped campaign from ranking. It stays a
        # survivor here and is routed to the free-refresh path in run_detail_pass.
        if c.get("id") in known_ids:
            survivors.append(c)
            continue

        reasons = []

        pay = c.get("pay_per_1k")
        if cfg.prefilter_min_pay_per_1k and pay is not None and pay < cfg.prefilter_min_pay_per_1k:
            reasons.append(f"pay ${pay:.2f}/1k < ${cfg.prefilter_min_pay_per_1k:.2f}")

        rem = c.get("budget_remaining_fraction")
        if rem is not None and rem <= cfg.prefilter_min_budget_remaining:
            reasons.append(f"budget {rem*100:.0f}% <= {cfg.prefilter_min_budget_remaining*100:.0f}%")

        plats = set(c.get("platforms") or [])
        if required and plats and not (plats & required):
            reasons.append("no tiktok/shorts/reels")

        if reasons:
            skipped.append((c, "; ".join(reasons)))
        else:
            survivors.append(c)
    return survivors, skipped


# --- records -------------------------------------------------------------------
# Fields filled by enrich_active()/passes later; None means "honestly unknown".
_ANALYSIS_DEFAULTS = {
    "min_payout": None, "min_views_to_payout": None, "high_minimum": False,
    "min_view_threshold": None,   # views a video must reach before ANY payout (distinct gate)
    "max_payout_per_video": None, "max_payout_uncapped": False,
    "participants_per_1k_budget": None,
    "category": None,          # single best category (per-campaign display / style_fit)
    "categories": [],          # ALL categories it fits (multi-tag, for category ranking)
    "join_cta": None, "open_to_all": "unclear",
    "disqualifiers": [], "disqualified": False,
    "first_seen_at": None, "days_active": None, "payout_velocity": None,
    # PAYOUT HEALTH — is the campaign actually paying? submissions is the inline activity count
    # next to the budget; payout_health is the verdict (dead/healthy/new/ok/unknown) + the
    # resolved factor composite_score multiplies in. Fails open (unknown -> factor 1.0).
    "submissions": None, "payout_health": None, "payout_health_factor": 1.0,
    "snapshot": None, "snapshot_history": None, "trends": None,
    # cross-run + strategic signals (filled by strategic.compute_strategic_signals)
    "budget_drain": None, "participant_growth": None, "source_saturation": None,
    "recurring_creator": None, "account_reusability": None,
    "source": None,
    "repeatable_clippability": None,
    "footage_stats": None,
    # footage SUBSTANCE intake (intake.py) — accessibility / type / volume / density
    "footage_intake": None, "footage_access": None, "content_type": None,
    "footage_volume": None, "action_density": None,
    # campaign locator for the clipper handoff (Task A). campaign_id (the app-frame UUID),
    # url (built from it), frame_url (raw apps.whop.com route), and brief_url (the dialog's
    # 'Brief' Google-Doc link that feeds intake) are captured when the Radix detail modal
    # opens; locator_missing flags a campaign we couldn't locate so the clipper can fail loud
    # on it instead of silently proceeding. Never crashes on a miss.
    "campaign_id": None, "locator_missing": False, "brief_url": None, "frame_url": None,
    # Full on-modal capture (rules live in DIFFERENT places per campaign): the dialog's visible
    # requirements/guidelines text, ALL resource anchors (rules docs AND footage folders, each
    # labelled), and the on-modal stats. The clipper reads rules from BOTH the on-modal text
    # AND the linked docs, whichever a campaign uses.
    "modal_requirements_text": None, "modal_rules_text": None, "resource_links": [],
    "modal_stats": None,
    # resource-capture reliability cross-check: the modal TEXT references a linked
    # Doc/Drive/Notion/folder but resource_links came back EMPTY (a silently-missed doc). This
    # is DETECTION only — capture logic is unchanged — surfaced as a report warning so a missed
    # doc never passes as "no docs". See enrich_active + extract.references_resource_doc.
    "capture_suspect": False, "capture_suspect_reason": None,
    # Rules readability (Notion handling). rules_source: modal/gdoc/notion/unreadable/none;
    # rules_unreadable=True (rules ONLY in a source we can't read) EXCLUDES the campaign from
    # the ranked output the clipper reads. notion_rules_text holds a successfully-fetched
    # public Notion page's rules.
    "rules_readable": None, "rules_source": None, "rules_unreadable": False,
    "rules_unreadable_reason": None, "notion_rules_text": None,
    # SELF-SOURCED footage derank (campaign provides no footage; clipper must find their own).
    # rules_doc_text caches fetched Google-Doc rules text (persists across runs). self_sourced
    # (True/False/None) + the matched phrase + the resolved factor composite_score multiplies in.
    "rules_doc_text": None, "self_sourced": None, "self_sourced_phrase": None,
    "self_sourced_factor": 1.0,
    # Prohibited/vice category exclusion (gambling/betting/casino/alcohol/vape/…). Like
    # rules_unreadable, excluded_prohibited=True REMOVES the campaign from the ranked output
    # the clipper reads (kept in campaigns.json, segregated in the report with the reason);
    # _borderline flags an ambiguous-keyword-only match for the user to eyeball.
    "excluded_prohibited": False, "excluded_prohibited_reason": None,
    "excluded_prohibited_borderline": False,
}


def card_only_record(card, status, skip_reason=None):
    rec = {
        "id": card.get("id"),
        "url": card.get("url"),
        "status": status,
        "skip_reason": skip_reason,
        "scraped_at": None,
        "name": card.get("name"),
        "creator": None,
        "pay_value": card.get("pay_value"),
        "pay_unit": card.get("pay_unit"),
        "pay_per_1k": card.get("pay_per_1k"),
        "budget_paid": card.get("budget_paid"),
        "budget_total": card.get("budget_total"),
        "budget_remaining_fraction": card.get("budget_remaining_fraction"),
        "platforms": card.get("platforms") or [],
        "source_links": [],
        "rules_text": None,
        "participants": None,
        "deadline": None,
    }
    rec.update({k: (list(v) if isinstance(v, list) else v)
                for k, v in _ANALYSIS_DEFAULTS.items()})
    rec["source"] = {"name": None, "handles": [], "reach_estimate": None,
                     "recent_avg_views": None, "confidence": "UNKNOWN"}
    # Card-only stubs are never opened, so no locator was captured — mark it missing so a
    # downstream picker knows this record can't be located without a real scrape.
    rec["campaign_id"] = None
    rec["locator_missing"] = True
    return rec


def build_record(card, detail, status):
    def pick(a, b):
        return a if a is not None else b
    rec = {
        "id": card.get("id"),
        "url": detail.get("url") or card.get("url"),
        "status": status,
        "skip_reason": None,
        "scraped_at": _now_iso(),
        "name": detail.get("name") or card.get("name"),
        "creator": detail.get("creator"),
        "pay_value": pick(detail.get("pay_value"), card.get("pay_value")),
        "pay_unit": detail.get("pay_unit") or card.get("pay_unit"),
        "pay_per_1k": pick(detail.get("pay_per_1k"), card.get("pay_per_1k")),
        "budget_paid": pick(detail.get("budget_paid"), card.get("budget_paid")),
        "budget_total": pick(detail.get("budget_total"), card.get("budget_total")),
        "budget_remaining_fraction": pick(
            detail.get("budget_remaining_fraction"), card.get("budget_remaining_fraction")
        ),
        "platforms": detail.get("platforms") or card.get("platforms") or [],
        "source_links": detail.get("source_links") or [],
        "rules_text": detail.get("rules_text"),
        "participants": detail.get("participants"),
        "deadline": detail.get("deadline"),
        "approval_rate": detail.get("approval_rate"),   # header "NN% approval rate" (or None)
    }
    rec.update({k: (list(v) if isinstance(v, list) else v)
                for k, v in _ANALYSIS_DEFAULTS.items()})
    rec["join_cta"] = detail.get("join_cta")   # scraped from the page; keep over the default
    # LOCATOR (Task A): captured while the detail modal was open (see _capture_locator /
    # scrape_detail). campaign_id is the per-campaign UUID from the app-frame route; url is the
    # human-clickable whop.com URL built from it (frame_url keeps the raw apps.whop.com route);
    # brief_url is the dialog's 'Brief' link for intake. campaign_id falls back to the URL's
    # last segment for old/fallback urls. locator_missing is True only when we captured NEITHER
    # a URL nor an id — a genuinely unlocatable campaign the clipper must fail loud on.
    rec["url"] = detail.get("url") or card.get("url")
    rec["campaign_id"] = detail.get("campaign_id") or extract.campaign_id_from_url(rec["url"])
    rec["brief_url"] = detail.get("brief_url")
    rec["frame_url"] = detail.get("frame_url")
    rec["modal_requirements_text"] = detail.get("modal_requirements_text")
    # Full rules from the modal for the clipper (hashtags / on-screen format / requirements),
    # not just the pointer bullet that lands in rules_text. See extract.full_modal_rules.
    rec["modal_rules_text"] = detail.get("modal_rules_text")
    rec["resource_links"] = detail.get("resource_links") or []
    rec["modal_stats"] = detail.get("modal_stats")
    rec["locator_missing"] = not (rec["url"] or rec["campaign_id"])
    return rec


def _make_snapshot(rec, ts):
    """A per-run snapshot of the volatile metrics, for cross-run trend diffing."""
    return {
        "ts": ts,
        "budget_paid": rec.get("budget_paid"),
        "budget_remaining_fraction": rec.get("budget_remaining_fraction"),
        "participants": rec.get("participants"),
    }


def enrich_active(rec, cfg, prev_rec=None, now=None):
    """Derive every analysis dimension for one active record: minimum-payout viability,
    source scaffold, max payout, competition, category, hard disqualifiers, campaign
    age + payout velocity, and the cross-run snapshot/trend. Idempotent; reads only
    already-scraped fields (the follower + clipper lookups happen in later passes).
    `prev_rec` is the same campaign from the previous run (for trends + first_seen)."""
    now = now or datetime.now(timezone.utc)
    rules = rec.get("rules_text")
    pay = rec.get("pay_per_1k")

    # minimum-payout viability
    min_payout = extract.parse_min_payout(rules)
    mv = extract.min_views_to_payout(min_payout, pay)
    rec["min_payout"] = min_payout
    rec["min_views_to_payout"] = mv
    rec["high_minimum"] = mv is not None and mv > cfg.min_payout_max_views

    # minimum-VIEW payout gate (DISTINCT from the min-payout-DOLLAR above): some campaigns pay
    # $0 until a single video crosses a hard VIEW count ("VIDEO MUST REACH 10K FOR PAYOUT").
    # Brutal for a new/low-view account, so it feeds a strong composite penalty scaling with
    # the threshold. Reads the on-modal requirements text as well as the rules bullets.
    mv_text = " ".join(t for t in (rules, rec.get("modal_requirements_text")) if t)
    rec["min_view_threshold"] = extract.parse_min_view_threshold(mv_text)

    # resource-capture reliability: if the modal TEXT references a linked Doc/Drive/Notion/
    # folder but resource_links came back EMPTY, a doc was silently missed. Flag it (DETECTION
    # only — capture logic unchanged) so it surfaces in the report instead of passing as "no
    # docs". When links WERE captured, the reference is expected — not suspect.
    if rec.get("resource_links"):
        rec["capture_suspect"] = False
        rec["capture_suspect_reason"] = None
    else:
        ref = extract.references_resource_doc(mv_text)
        rec["capture_suspect"] = bool(ref)
        rec["capture_suspect_reason"] = ref or None

    # max payout per video
    mp = extract.parse_max_payout(rules)
    rec["max_payout_per_video"] = mp["max_payout_per_video"]
    rec["max_payout_uncapped"] = mp["uncapped"]

    # competition per dollar
    rec["participants_per_1k_budget"] = extract.participants_per_1k_budget(
        rec.get("participants"), rec.get("budget_total"))

    # category + open-to-instant-join classification + hard disqualifiers. Openness reads
    # the brief plus the page CTA (Apply vs Join); an application/selection gate is a hard DQ.
    rec["category"] = extract.classify_category(rec.get("name"), rules, rec.get("platforms"))
    rec["categories"] = extract.classify_categories(rec.get("name"), rules, rec.get("platforms"))
    rec["open_to_all"] = extract.classify_openness(rules, rec.get("join_cta"))
    rec["disqualifiers"] = extract.detect_disqualifiers(
        rules, rec.get("platforms"), rec.get("source_links"), rec.get("join_cta"))
    rec["disqualified"] = bool(rec["disqualifiers"])

    # prohibited/vice category exclusion — a SEPARATE, stronger gate than the gambling
    # disqualifier above (which only sinks to composite 0): like rules_unreadable it removes
    # the campaign from the ranked output entirely. Reads name + category + on-modal
    # requirements text (extract.PROHIBITED_* are the extensible keyword/brand lists).
    prohibited = extract.detect_prohibited_category(
        rec.get("name"), rec.get("category"), rec.get("modal_requirements_text"))
    rec["excluded_prohibited"] = bool(prohibited)
    rec["excluded_prohibited_reason"] = prohibited["reason"] if prohibited else None
    rec["excluded_prohibited_borderline"] = bool(prohibited and prohibited["borderline"])

    # LANGUAGE — Scout is an English-only operation, so a clearly non-English campaign
    # (Spanish/Portuguese/French, or a non-Latin script) is DERANKED (not excluded). Offline
    # stopword heuristic (NO Groq/network); fails OPEN — short/ambiguous text is assumed
    # English so we never wrongly derank. The resolved factor is stored so composite_score
    # just multiplies it (English/unknown -> 1.0, leaving English composites EXACTLY unchanged).
    lang = language.detect_language(language.language_text(rec))
    rec["language"] = lang
    rec["language_penalty_factor"] = cfg.nonenglish_penalty if lang.get("nonenglish") else 1.0

    # FOOTAGE PRESENCE — does the campaign expose a PUBLIC, downloadable footage link at all?
    # A fully-scraped campaign with ZERO public footage links (footage member-gated or absent,
    # e.g. Jesser x ClipFarm / SomSleep) can't be clipped from the auto-run, so it's DERANKED
    # (not excluded). DISTINCT from the liveness derank (an existing link gone dead). Fails OPEN:
    # an unscraped stub -> undeterminable -> no penalty. The resolved factor is stored so
    # composite_score just multiplies it (has-footage/undeterminable -> 1.0, unchanged).
    fpres = intake_mod.footage_presence(rec)
    rec["footage_presence"] = fpres
    rec["footage_presence_factor"] = (cfg.no_footage_penalty
                                      if fpres.get("has_public_footage") is False else 1.0)

    # APPROVAL RATE — the % of submissions a campaign approves/pays (shown in the header as
    # "NN% approval rate"). A KNOWN-and-LOW rate means most clips are rejected unpaid, so a
    # campaign below cfg.approval_rate_floor is DERANKED (not excluded). Prefer the value scraped
    # off the detail header; otherwise recover it from the modal/rules text (the header rate is
    # the first "NN% approval rate"). Fails OPEN: not shown/unparseable -> UNKNOWN -> no penalty;
    # a rate at/above the floor is untouched. The resolved factor is stored so composite_score
    # just multiplies it in.
    appr = rec.get("approval_rate")
    if not isinstance(appr, (int, float)) or isinstance(appr, bool):
        appr = extract.parse_approval_rate(
            " ".join(t for t in (rec.get("modal_requirements_text"), rec.get("rules_text")) if t))
        rec["approval_rate"] = appr
    known_low = (cfg.approval_derank_enabled and isinstance(appr, (int, float))
                 and not isinstance(appr, bool) and appr < cfg.approval_rate_floor)
    rec["approval_rate_factor"] = cfg.approval_low_penalty if known_low else 1.0

    # SELF-SOURCED footage — does the rules text tell clippers to find their OWN footage (campaign
    # provides none)? Un-clippable by a footage-download pipeline, so DERANKED (not excluded).
    # Resolved here over the text available now (modal + rules bullets + any Notion text); the
    # gdoc-doc pass (enrich_self_sourced_docs, after rules-readability) re-checks footage-less
    # campaigns with the fetched doc text. Fails OPEN — high-precision detector, so a normal
    # campaign is never flagged. The resolved factor is stored for composite_score to multiply in.
    resolve_self_sourced(rec, cfg)

    # campaign age (first_seen carried across runs) + payout velocity
    first_seen = (prev_rec or {}).get("first_seen_at") or rec.get("first_seen_at") \
        or now.isoformat()
    rec["first_seen_at"] = first_seen
    try:
        days = (now - datetime.fromisoformat(first_seen)).total_seconds() / 86400.0
        rec["days_active"] = round(max(days, 0.0), 3)
    except Exception:
        rec["days_active"] = None
    rec["payout_velocity"] = extract.payout_velocity(
        rec.get("budget_paid"), rec.get("budget_total"), rec.get("days_active"))

    # PAYOUT HEALTH — is the campaign ACTUALLY paying, or a paying-dead trap (meaningful
    # submissions but ~$0 ever paid out)? `submissions` is the inline activity count Whop shows
    # next to the budget (the live Views/Submissions chart is shadow-DOM and unscraped). days_
    # active is Scout's own tracking age (lower bound), so it only exonerates a young campaign;
    # when age is unmeasured the submission count establishes it. Fails open (unknown -> 1.0).
    rec["submissions"] = extract.parse_activity_count(rec.get("modal_requirements_text"))
    if cfg.payout_health_enabled:
        ph = scoring.payout_health(
            rec.get("budget_paid"), rec.get("budget_total"),
            rec.get("submissions"), rec.get("days_active"),
            min_age_days=cfg.payout_min_age_days,
            min_submissions=cfg.payout_min_submissions,
            zero_dollars=cfg.payout_zero_dollars,
            zero_fraction=cfg.payout_zero_fraction,
            healthy_spent_fraction=cfg.payout_healthy_spent_fraction,
            dead_penalty=cfg.payout_dead_penalty,
            healthy_boost=cfg.payout_healthy_boost,
        )
    else:
        ph = {"status": "disabled", "factor": 1.0, "submissions": rec.get("submissions"),
              "days_open": rec.get("days_active"), "paid_out": rec.get("budget_paid"),
              "spent_fraction": None, "reason": "payout-health check disabled"}
    rec["payout_health"] = ph
    rec["payout_health_factor"] = ph["factor"]

    # cross-run snapshot + accumulating history (built on, not replacing, prior runs) +
    # trend. The history is what the budget-drain / participant-growth projections use.
    carried_hist = list((prev_rec or {}).get("snapshot_history") or [])
    prev_snap = (prev_rec or {}).get("snapshot")
    if prev_snap:
        carried_hist.append(prev_snap)
    cap = getattr(cfg, "max_snapshot_history", 20)
    rec["snapshot_history"] = carried_hist[-cap:]
    rec["snapshot"] = _make_snapshot(rec, now.isoformat())
    rec["trends"] = scoring.compute_trends(rec["snapshot"], prev_snap)

    # source scaffold (preserve counts already retrieved on a prior run)
    existing = rec.get("source")
    if not existing or not existing.get("handles"):
        handles = extract.extract_handles(rules, rec.get("source_links"))
        name = rec.get("creator")
        rec["source"] = {
            "name": name,
            "handles": handles,
            "reach_estimate": (existing or {}).get("reach_estimate"),
            "recent_avg_views": (existing or {}).get("recent_avg_views"),
            "confidence": (existing or {}).get("confidence")
            or ("LOW" if (name or handles) else "UNKNOWN"),
        }


# --- phase 2: detail pass ------------------------------------------------------
# Clicking a card does NOT navigate — it opens an in-frame Radix dialog overlaid on
# the list (which stays mounted behind it). So we open by clicking the card, extract
# from the dialog, then press Escape to close and return to the exact same list. We
# find the card by its accessible name ("View <NAME> campaign"), so we never depend
# on scroll position surviving — the lookup re-resolves the card wherever it is.
def _click_with_retry(locator, cfg, pacer, *, hover=None):
    """Click with a generous timeout and RETRIES. Whop's iframe UI is often slower than one
    click timeout, so a lone timeout is usually transient slowness, not a real failure — we
    wait briefly and retry `cfg.click_retries` times. Only the last exception (all attempts
    exhausted) propagates, so a caller's failure counter ticks once per genuinely stuck
    element, not once per slow attempt. StopRun is never swallowed."""
    attempts = max(1, cfg.click_retries)
    last = None
    for i in range(attempts):
        try:
            if hover is not None:
                try:
                    pacer.maybe_hover(hover)
                except Exception:
                    pass
            locator.click(timeout=cfg.click_timeout_ms)
            return
        except StopRun:
            raise
        except Exception as e:                       # transient (timeout / stale) — retry
            last = e
            if i < attempts - 1:
                time.sleep(random.uniform(*cfg.click_retry_wait))
    raise last


def open_detail(page, fl, name, pacer, cfg):
    """Click the card for `name` and return its open dialog Locator. Retries slow clicks;
    raises a plain Exception on transient failure (caught + retried per-campaign) but StopRun
    ONLY on a real block (challenge / login wall) so the run-ending tripwire stays specific."""
    btn = fl.get_by_role("button", name=f"View {name} campaign", exact=True).first
    try:
        btn.scroll_into_view_if_needed(timeout=cfg.scroll_into_view_ms)
    except Exception:
        pass
    _click_with_retry(btn, cfg, pacer, hover=btn)
    if extract.is_challenge(page) or extract.is_login_wall(page):
        raise StopRun("challenge / login wall after opening a campaign")
    dialog = fl.locator(S.DETAIL_DIALOG[0]).first
    dialog.wait_for(state="visible", timeout=cfg.dialog_wait_ms)
    pacer.page_delay()
    return dialog


def close_detail(page, fl):
    """Escape closes the Radix dialog; the list is preserved behind it."""
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    try:
        fl.locator(S.DETAIL_DIALOG[0]).first.wait_for(state="detached", timeout=5000)
    except Exception:
        try:  # one more nudge if it didn't dismiss
            page.keyboard.press("Escape")
            time.sleep(0.5)
        except Exception:
            pass


def _scroll_dialog(page, pacer):
    """Gentle human scroll inside the centered detail dialog."""
    try:
        vp = page.viewport_size or {"width": 1366, "height": 768}
        page.mouse.move(vp["width"] / 2, vp["height"] / 2)
        for _ in range(random.randint(1, 3)):
            page.mouse.wheel(0, random.randint(*pacer.scroll_step))
            time.sleep(random.uniform(*pacer.scroll_pause))
    except Exception:
        pass


def _expand_dialog_text(dialog):
    """Best-effort: click any 'See more' / 'Read more' / 'Show more' toggle inside the detail
    dialog so collapsed rules text fully renders before we read it. Some campaigns truncate a
    long requirements block behind such a toggle, and the hidden portion isn't in innerText
    until expanded. Scoped to the dialog Locator, capped, and NEVER raises — a missing toggle
    or a slow click is a silent no-op (the read still proceeds on whatever is visible)."""
    for sel in S.DETAIL_RULES_EXPAND:
        try:
            locs = dialog.locator(sel)
            n = min(locs.count(), 4)
        except Exception:
            continue
        for i in range(n):
            try:
                loc = locs.nth(i)
                if loc.is_visible(timeout=500):
                    loc.click(timeout=1500)
            except Exception:
                continue


def _frame_url(page):
    fr = get_app_frame(page)
    return fr.url if fr else None


def _looks_campaign_url(url, list_page_url):
    """A campaign-specific whop.com URL, not the list/discover/root base."""
    if not url:
        return False
    u = url.rstrip("/")
    bases = {(list_page_url or "").rstrip("/"), S.DISCOVER_URL.rstrip("/"), "https://whop.com"}
    return "whop.com" in u and u not in bases


def _return_to_list(page, fl, list_page_url, pacer):
    """After a full-page expand, go back to the list and confirm the feed is back.
    Cards aren't virtualized, so once the feed re-renders every card is findable."""
    try:
        page.go_back(timeout=10000)
    except Exception:
        pass
    time.sleep(1.0)
    if wait_for_feed(page, pacer) is not None:
        return True
    navigate_to_list(page, pacer)  # last resort: re-enter through discover
    return wait_for_feed(page, pacer) is not None


def _recover_list(page, fl, list_page_url, pacer):
    """After a failed/slow scrape, dismiss any half-open dialog and confirm the list feed is
    healthy again BEFORE continuing — so one stuck card doesn't cascade into more failures
    (a partially-opened dialog left over the list breaks the next card's click). Best-effort,
    never raises; returns True if the feed looks healthy."""
    try:
        close_detail(page, fl)          # Escape any stray/half-open dialog
    except Exception:
        pass
    try:
        if wait_for_feed(page, pacer) is not None:
            return True
    except Exception:
        pass
    try:                                 # last resort: re-enter the list through discover
        navigate_to_list(page, pacer)
        return wait_for_feed(page, pacer) is not None
    except Exception:
        return False


def _abs_whop_url(href):
    """Absolutize a possibly-relative whop href. None if not usable."""
    if not href:
        return None
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return "https://whop.com" + href
    return None


def _href_from_dialog(dialog, list_page_url):
    """Read a clickable campaign URL straight from an anchor in the open dialog — NO
    navigation. Whop's 'Expand to full page' / share / title controls are frequently
    `<a href>`, and reading the href is far more reliable than clicking through and diffing
    `page.url` (and needs no fragile return-to-list). Returns a campaign URL or None."""
    selectors = (
        'a[href*="whop.com"][href*="/app"]',   # most specific: a campaign app link
        'a[href^="/"][href*="/app"]',          # relative campaign app link
        'a[href*="whop.com"]',                  # any whop.com anchor
        'a[href^="/"]',                         # any relative anchor (absolutized below)
    )
    for sel in selectors:
        try:
            locs = dialog.locator(sel)
            count = locs.count()
        except Exception:
            continue
        for i in range(min(count, 15)):
            try:
                url = _abs_whop_url(locs.nth(i).get_attribute("href"))
            except Exception:
                continue
            if _looks_campaign_url(url, list_page_url):
                return url
    return None


# A campaign locator UUID (e.g. dd9f7918-e51d-4935-9f23-5935c783774a).
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _campaign_uuid_from_frame(frame_url):
    """The per-campaign UUID from the app-frame's route, or None. CONFIRMED by --test-capture:
    when the detail modal opens, the app frame (apps.whop.com) client-routes from
    `.../discover` to `.../discover/<UUID>`, where <UUID> is the campaign locator. (The
    `app_...` id in the path is the SHARED Content-Rewards app id — same for every campaign —
    NOT the locator; don't confuse them.) The literal `discover` base has no UUID -> None."""
    if not frame_url:
        return None
    seg = frame_url.split("?")[0].split("#")[0].rstrip("/").split("/")[-1]
    return seg if _UUID_RE.fullmatch(seg or "") else None


# Rule/requirement docs + footage folders link out to these hosts. Captured in FULL (not just
# a "Brief" anchor) because campaigns label them differently — Brief / Requirements / Guidelines
# / Content — and some (DoorDash) have TWO (a rules doc AND a footage folder).
_RESOURCE_HOST_RE = re.compile(
    r"(docs\.google\.com|drive\.google\.com|sheets\.google\.com|slides\.google\.com|"
    r"notion\.so|notion\.site|dropbox\.com|onedrive\.live\.com|1drv\.ms|mega\.nz)", re.I)


def _dedupe_doubled(s):
    """Anchor text often renders the label twice (icon span + label span both carry it, e.g.
    'Clips & GuidelinesClips & Guidelines'). Collapse an exact immediate doubling — but only
    for non-trivial lengths, so a real word like 'ByeBye' isn't mangled."""
    n = len(s)
    if n >= 8 and n % 2 == 0 and s[:n // 2] == s[n // 2:]:
        return s[:n // 2]
    return s


def _clean_anchor_label(text):
    """A readable resource label: collapse whitespace, drop the leading service icon-word the
    anchor renders ('Google DriveBrief' -> 'Brief'), and undo the doubled-text artifact."""
    s = _dedupe_doubled(" ".join((text or "").split()))
    s = re.sub(r"^(Google Drive|Google Docs|Google Sheets|Google Slides|Google|Notion|"
               r"Dropbox|Drive|Docs|Sheets)\s*", "", s, flags=re.I).strip()
    return _dedupe_doubled(s) or "link"


def _resource_links_from_anchors(anchors):
    """ALL doc/drive/notion resource anchors in the dialog as [{url, label}], deduped by url,
    order preserved. Not filtered by the word 'Brief' — every rules doc AND footage folder."""
    out, seen = [], set()
    for a in anchors or []:
        href = (a.get("href") or "").strip()
        if not href or href in seen:
            continue
        if not _RESOURCE_HOST_RE.search(href):
            continue
        seen.add(href)
        out.append({"url": href, "label": _clean_anchor_label(a.get("text"))})
    return out


def _pick_brief_url(resource_links):
    """A single convenience 'brief' link (back-compat): prefer a rules-doc label, else the
    first resource. None if there are no resources."""
    if not resource_links:
        return None
    for r in resource_links:
        lab = (r.get("label") or "").lower()
        if any(k in lab for k in ("brief", "requirement", "guideline", "rule", "doc")):
            return r.get("url")
    return resource_links[0].get("url")


def _money(s):
    try:
        return float((s or "").replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _parse_modal_stats(text):
    """On-modal stats (no doc needed): pay rate, budget paid/total/remaining, min payout, max
    per video — parsed from the dialog's visible text. Also keeps the raw 'Earnings' snippet
    for eyeballing. Best-effort; any field absent -> omitted. Never raises. The dialog renders
    these as '$248,632 /$250,000' (paid/total), '$1.50 /1K' (rate), '$1.50 Min', '$1500 Max'."""
    if not text:
        return None
    t = " ".join(text.split())
    st = {}
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)\s*/\s*1\s*[kK]", t)
    if m:
        st["pay_per_1k"] = _money(m.group(1))
        st["pay_rate_text"] = m.group(0).strip()
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)\s*/\s*\$\s*([\d,]+(?:\.\d+)?)", t)
    if m:
        paid, total = _money(m.group(1)), _money(m.group(2))
        st["budget_paid"], st["budget_total"] = paid, total
        st["budget_remaining_fraction"] = (
            max(0.0, min(1.0, 1.0 - paid / total)) if (paid is not None and total) else None)
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)\s*Min\b", t)
    if m:
        st["min_payout"] = _money(m.group(1))
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)\s*Max\b", t)
    if m:
        st["max_per_video"] = _money(m.group(1))
    em = re.search(r"\bEarnings\b(.*?)(?:\bAnalytics\b|\bResources\b|$)", t, re.S)
    if em and em.group(1).strip():
        st["earnings_text"] = em.group(1).strip()[:300]
    return st or None


# Runs inside the app frame against the OPEN dialog. Returns the frame's client route
# (location.href — where the campaign UUID lives), the dialog's full visible innerText (the
# on-modal requirements/guidelines the clipper needs when there's no linked doc), and EVERY
# anchor (href + visible label) so all resource links are captured, not just one.
_MODAL_EXTRACT_JS = r"""
() => {
  const d = document.querySelector('[role="dialog"]')
        || document.querySelector('.campaign-details-modal-bg');
  const out = {frameUrl: location.href, found: !!d};
  if (!d) return out;
  out.requirementsText = (d.innerText || d.textContent || '').trim();
  out.anchors = [...d.querySelectorAll('a')].map(a => ({
    href: a.getAttribute('href') || a.href || '',
    text: (a.innerText || a.textContent || '').trim().slice(0, 120)
  })).filter(a => a.href);
  return out;
}
"""


def _capture_locator(page, fl, list_page_url):
    """Read EVERYTHING the clipper needs from the OPEN detail modal — the PRIMARY, navigation-
    free path (confirmed via --test-capture). One frame.evaluate pulls the frame route, the
    dialog's full visible text, and all anchors; the rest is parsed here:
      - campaign_id: the UUID in the app-frame route (identity only; the built whop.com URL
        does NOT resolve — it lands on Discover — so `url` is left None on purpose).
      - modal_requirements_text: the dialog's visible body (description + on-modal
        requirements/guidelines) so intake has the on-page rules even with no linked doc.
      - resource_links: ALL doc/drive/notion anchors as {url, label} (rules docs AND footage
        folders; some campaigns have both), labelled so intake/clipper can tell them apart.
      - modal_stats: pay rate, budget, min/max payout parsed from the modal text.
      - brief_url: a single convenience rules-doc link (back-compat).
    Every field degrades to None/[]; never raises. locator_missing (build_record) is True only
    when we captured NEITHER a campaign_id nor a url."""
    data = {}
    fr = get_app_frame(page)
    if fr is not None:
        try:
            data = fr.evaluate(_MODAL_EXTRACT_JS) or {}
        except Exception:
            data = {}
    frame_url = data.get("frameUrl") or _frame_url(page)
    campaign_id = _campaign_uuid_from_frame(frame_url)
    requirements = (data.get("requirementsText") or "").strip() or None
    resource_links = _resource_links_from_anchors(data.get("anchors"))
    return {
        "campaign_id": campaign_id,
        "url": None,                       # UUID url doesn't resolve — identity only
        "frame_url": frame_url,
        "modal_requirements_text": requirements,
        "resource_links": resource_links,
        "modal_stats": _parse_modal_stats(requirements),
        "brief_url": _pick_brief_url(resource_links),
    }


# --- rules readability (Notion fetch + exclusion) ------------------------------
# The clipper's intake can read on-modal text and Google Docs, but NOT Notion pages
# (JS-rendered). A campaign whose rules live ONLY in an unreadable source must not reach the
# clipper — clipping without the known banned-word list is a compliance-violation risk. So
# scout resolves rules readability here: prefer on-modal / Google-Doc rules; only when rules
# are ONLY in Notion does it try to fetch that page over HTTP; if that fails, the campaign is
# flagged rules_unreadable and EXCLUDED from the ranked output (kept in campaigns.json,
# segregated with a reason). Notion being present but REDUNDANT (real rules also on-modal or
# in a Doc) is never a reason to drop a campaign.

# The pointer-phrase list, rule-signal regex, and the section/substance helpers are the
# canonical pure versions in extract.py (also used to build modal_rules_text during the scrape);
# these thin wrappers keep resolve_rules_readability reading from one source of truth.
_modal_rules_section = extract.modal_rules_section
_has_substantive_rules = extract.has_substantive_rules


def _rules_gdoc_links(resource_links):
    return [r for r in (resource_links or [])
            if re.search(r"docs\.google\.com", r.get("url") or "", re.I)]


def _rules_notion_links(resource_links):
    return [r for r in (resource_links or [])
            if re.search(r"notion\.so|notion\.site", r.get("url") or "", re.I)]


def _next_data_text(raw):
    """Human-readable strings from a Next.js __NEXT_DATA__ blob (Notion super-sites embed the
    page content there). '' if absent/unparseable."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', raw, re.S)
    if not m:
        return ""
    try:
        data = json.loads(m.group(1))
    except Exception:
        return ""
    out = []

    def walk(o):
        if isinstance(o, str):
            s = o.strip()
            if len(s) >= 3 and " " in s and not s.startswith(("http", "/", "{", "[", "data:")):
                out.append(s)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    return " ".join(dict.fromkeys(out))


def _html_visible_text(raw):
    raw = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", raw, flags=re.I | re.S)
    raw = re.sub(r"<[^>]+>", " ", raw)
    import html as _html
    return " ".join(_html.unescape(raw).split())


def _meta_description(raw):
    m = re.search(r'<meta[^>]+(?:name|property)=["\'](?:og:description|description)["\']'
                  r'[^>]*content=["\']([^"\']+)', raw, re.I)
    return m.group(1) if m else ""


def _fetch_notion_text(url, timeout=20):
    """Best-effort fetch of a PUBLIC Notion page's rules text over plain HTTP. Returns
    (text, source) on success or (None, reason) on failure — private/gated/JS-only pages that
    expose no readable content fail here (and the campaign is then excluded). Never raises."""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        return None, f"fetch failed: {(str(e).splitlines() or ['?'])[0]}"
    text = " ".join(p for p in (_next_data_text(raw), _html_visible_text(raw),
                                _meta_description(raw)) if p)
    text = " ".join(text.split())
    low = text.lower()
    if not text or len(text) < 400 or "enable javascript" in low:
        return None, f"no usable rules text ({len(text)} chars — JS-only/gated/private)"
    return text[:8000], "notion-http"


def _gdoc_export_url(url):
    """The plaintext-export URL for a public Google DOCUMENT, or None (sheets/slides/other are
    not handled)."""
    m = re.search(r"docs\.google\.com/document/d/([A-Za-z0-9_-]+)", url or "")
    return f"https://docs.google.com/document/d/{m.group(1)}/export?format=txt" if m else None


def _fetch_gdoc_text(url, timeout=20):
    """Best-effort fetch of a PUBLIC Google Doc's plaintext via the export endpoint. Returns
    (text, source) on success or (None, reason) on failure. Never raises. Used ONLY to read the
    rules doc for the self-sourced-footage check (a private doc simply yields no text -> no flag,
    fail-open)."""
    exp = _gdoc_export_url(url)
    if not exp:
        return None, "not a google-doc url"
    import urllib.request
    try:
        req = urllib.request.Request(exp, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        return None, f"fetch failed: {(str(e).splitlines() or ['?'])[0]}"
    text = " ".join(raw.split())
    if not text or len(text) < 30:
        return None, f"empty/unreadable ({len(text)} chars — private/gated)"
    return text[:8000], "gdoc-export"


def _self_sourced_text(rec):
    """All the rules text available for a record — modal body + rules bullets + any fetched
    Notion/Google-Doc rules text — joined for the self-sourced-footage check."""
    return " ".join(t for t in (rec.get("rules_text"), rec.get("modal_requirements_text"),
                                rec.get("notion_rules_text"), rec.get("rules_doc_text")) if t)


def resolve_self_sourced(rec, cfg):
    """Resolve the SELF-SOURCED footage flag for one record: does the rules text tell clippers to
    find their OWN footage (campaign provides none)? Sets self_sourced (bool), self_sourced_phrase
    (the matched text or None), and self_sourced_factor (cfg.self_sourced_penalty when flagged,
    else 1.0). Pure over the record's already-present text; fails OPEN (high-precision detector)."""
    if not getattr(cfg, "self_sourced_enabled", True):
        rec["self_sourced"] = False
        rec["self_sourced_phrase"] = None
        rec["self_sourced_factor"] = 1.0
        return rec
    phrase = extract.detect_self_sourced(_self_sourced_text(rec))
    rec["self_sourced"] = bool(phrase)
    rec["self_sourced_phrase"] = phrase
    rec["self_sourced_factor"] = cfg.self_sourced_penalty if phrase else 1.0
    return rec


def enrich_self_sourced_docs(records, cfg):
    """Fetch Google-Doc rules text for FOOTAGE-LESS gdoc campaigns (where self-sourcing is
    plausible) and re-resolve the self-sourced flag with the doc text. The instruction usually
    lives in the doc, not the modal, so this is what makes the signal real. Bounded to the
    footage-less + gdoc-linked set, CACHED via rules_doc_text (persists in campaigns.json, so only
    the first run pays), off-Whop, best-effort — never raises. Returns (fetched, flagged)."""
    if not (getattr(cfg, "self_sourced_enabled", True)
            and getattr(cfg, "self_sourced_fetch_docs", True)):
        return 0, 0
    fetched = flagged = 0
    for rec in records:
        if rec.get("status") not in ("scraped", "refreshed"):
            continue
        if rec.get("self_sourced"):                 # already flagged from stored text
            flagged += 1
            continue
        if not rec.get("rules_doc_text"):
            # only footage-less campaigns with a gdoc rules link are worth a fetch
            if (rec.get("footage_presence") or {}).get("has_public_footage") is not False:
                continue
            gdocs = _rules_gdoc_links(rec.get("resource_links"))
            if not gdocs:
                continue
            text, _why = _fetch_gdoc_text(gdocs[0].get("url"))
            if text:
                rec["rules_doc_text"] = text
                fetched += 1
        resolve_self_sourced(rec, cfg)
        if rec.get("self_sourced"):
            flagged += 1
    if fetched or flagged:
        print(f"  Self-sourced footage: fetched {fetched} rules doc(s); "
              f"{flagged} campaign(s) flagged self-sourced (deranked ×{cfg.self_sourced_penalty})")
    return fetched, flagged


def resolve_rules_readability(rec, fetch=True):
    """Decide where a campaign's rules are readable FROM, and flag it rules_unreadable when
    they are ONLY in a source we can't read (Notion that won't fetch). Sets: rules_source
    (modal/gdoc/notion/unreadable/none), rules_readable (bool), rules_unreadable (bool) +
    rules_unreadable_reason, and notion_rules_text when a Notion fetch succeeds. Only fetches
    Notion for the ONLY-in-Notion case (never a broad crawl). Never raises."""
    rec.setdefault("notion_rules_text", None)
    resource_links = rec.get("resource_links") or []
    section = _modal_rules_section(rec.get("modal_requirements_text") or "")
    has_modal = _has_substantive_rules(section) or _has_substantive_rules(rec.get("rules_text"))
    gdocs = _rules_gdoc_links(resource_links)
    notions = _rules_notion_links(resource_links)

    def _set(source, readable, unreadable, reason):
        rec["rules_source"] = source
        rec["rules_readable"] = readable
        rec["rules_unreadable"] = unreadable
        rec["rules_unreadable_reason"] = reason

    if has_modal:
        _set("modal", True, False, None)
    elif gdocs:
        _set("gdoc", True, False, None)          # intake reads Google Docs fine
    elif notions:
        text, why = (None, "not fetched")
        if fetch:
            try:
                text, why = _fetch_notion_text(notions[0].get("url"))
            except Exception as e:               # belt-and-suspenders; _fetch already guards
                text, why = None, f"fetch error: {type(e).__name__}"
        if text:
            rec["notion_rules_text"] = text
            _set("notion", True, False, None)
        else:
            _set("unreadable", False, True,
                 f"rules only in Notion ({notions[0].get('url')}) — {why}")
    else:
        # No readable rules source AND no unreadable one either — out of scope for this
        # Notion feature; leave it in the board (don't newly exclude), just record the gap.
        _set("none", False, False, None)
    return rec


def resolve_rules_readability_all(records, fetch=True):
    """Run rules-readability resolution over every scraped/refreshed record. Logs how many are
    excluded (rules only in an unreadable Notion source)."""
    active = [r for r in records if r.get("status") in ("scraped", "refreshed")]
    excluded = []
    for r in active:
        resolve_rules_readability(r, fetch=fetch)
        if r.get("rules_unreadable"):
            excluded.append(r)
    if excluded:
        print(f"  Rules readability: {len(excluded)} campaign(s) EXCLUDED — rules only in an "
              f"unreadable source (Notion). They stay in campaigns.json, flagged, but are not "
              f"ranked/handed to the clipper:")
        for r in excluded:
            print(f"    - {r.get('name')!r}: {r.get('rules_unreadable_reason')}")
    return excluded


# Set True by the --test-capture diagnostic to make capture_campaign_url narrate each tier.
_CAPTURE_DEBUG = False

# Runs inside the app frame. Introspects the OPEN dialog for every place a campaign locator
# could live: anchors, buttons (to find the real expand/share control), id-like/data-*
# attributes, the frame's own client-side route (location.href — changes here do NOT touch
# the top address bar), and any Whop-style prefixed id token (app_/campaign_/prod_/…) sitting
# in the dialog HTML. Returns the raw dialog outerHTML too, for offline inspection.
_LOCATOR_DUMP_JS = r"""
() => {
  const d = document.querySelector('[role="dialog"]')
        || document.querySelector('.campaign-details-modal-bg');
  const out = {frameUrl: location.href, found: !!d};
  if (!d) return out;
  out.anchors = [...d.querySelectorAll('a')].slice(0, 40).map(a => ({
    href: a.getAttribute('href'), text: (a.textContent || '').trim().slice(0, 50)}));
  out.buttons = [...d.querySelectorAll('button,[role="button"]')].slice(0, 50).map(b => ({
    aria: b.getAttribute('aria-label'), text: (b.textContent || '').trim().slice(0, 50)}));
  const attrHits = [];
  for (const el of d.querySelectorAll('*')) {
    for (const at of el.attributes) {
      const n = at.name.toLowerCase();
      if ((n === 'id' || n.includes('campaign') || n.includes('slug') || n.includes('testid')
           || n.startsWith('data-')) && at.value) {
        attrHits.push(el.tagName.toLowerCase() + ' [' + at.name + '="'
                      + String(at.value).slice(0, 70) + '"]');
      }
    }
  }
  out.attrHits = [...new Set(attrHits)].slice(0, 80);
  const html = d.outerHTML || '';
  out.htmlLen = html.length;
  const idRe = /(?:app|camp|campaign|prod|biz|exp|comp|user|plan)_[A-Za-z0-9]{6,}/g;
  out.tokens = [...new Set(html.match(idRe) || [])].slice(0, 40);
  out.html = html;
  return out;
}
"""


def _dump_capture_diagnostics(page, idx, name, top_before, top_after,
                              fr_before, fr_after, reqs):
    """Heavy per-campaign locator diagnostics for --test-capture. Dumps every candidate
    source of a campaign locator so we can see where it ACTUALLY lives before writing any
    selector. Never raises."""
    print(f"    --- locator diagnostics [{idx}] {name!r} ---")
    print(f"      top-page url  before/after: {top_before}")
    print(f"                                  {top_after}   (changed={top_before != top_after})")
    print(f"      app-frame url before/after: {fr_before}")
    print(f"                                  {fr_after}   (changed={fr_before != fr_after})")
    try:
        for fi, fr in enumerate(page.frames):
            print(f"      frame[{fi}] name={fr.name!r} url={fr.url}")
    except Exception as e:
        print(f"      (frame enumerate failed: {type(e).__name__}: {e})")

    if reqs:
        print(f"      {len(reqs)} interesting network request(s) fired during modal open:")
        for m, u in reqs[:25]:
            print(f"        {m} {u}")
    else:
        print("      no campaign/app/api/graphql requests captured during modal open")

    fr = get_app_frame(page)
    if fr is None:
        print("      !! app-frame object not found — cannot introspect the dialog DOM")
        return
    try:
        data = fr.evaluate(_LOCATOR_DUMP_JS)
    except Exception as e:
        print(f"      (dialog DOM evaluate failed: {type(e).__name__}: {e})")
        return
    if not data or not data.get("found"):
        print(f"      !! no [role=dialog] found in the app frame "
              f"(frameUrl={None if not data else data.get('frameUrl')})")
        return

    print(f"      dialog frame location.href: {data.get('frameUrl')}")
    print(f"      id-like tokens in dialog HTML: {data.get('tokens') or '(none found)'}")
    anchors = data.get("anchors") or []
    print(f"      anchors in dialog ({len(anchors)}):")
    for a in anchors:
        print(f"        href={a.get('href')!r}  text={a.get('text')!r}")
    btns = data.get("buttons") or []
    print(f"      buttons in dialog ({len(btns)}):")
    for b in btns:
        print(f"        aria={b.get('aria')!r}  text={b.get('text')!r}")
    hits = data.get("attrHits") or []
    print(f"      id/data-* attributes in dialog ({len(hits)}):")
    for h in hits:
        print(f"        {h}")
    try:
        out = Path(f"probe_capture_{idx}.html")
        out.write_text(data.get("html") or "", encoding="utf-8")
        print(f"      full dialog HTML ({data.get('htmlLen')} chars) -> {out}")
    except Exception as e:
        print(f"      (dialog HTML dump failed: {type(e).__name__}: {e})")


def capture_campaign_url(page, fl, list_page_url, pacer, cfg):
    """Record a human-clickable campaign URL for the currently-open dialog.

    (1) Cheap: opening the dialog may have shallow-routed the top-page URL already.
    (2) Robust + navigation-free: read the campaign link's href straight from the dialog.
    (3) Fallback: click 'Expand to full page', diff the top-page / frame URL, return to the
    list. Fully non-fatal — returns (url_or_None, list_ok).
    """
    dbg = _CAPTURE_DEBUG
    if _looks_campaign_url(page.url, list_page_url):
        if dbg:
            print(f"      [capture] tier1 HIT — top page.url is campaign-specific: {page.url}")
        return page.url, True
    if dbg:
        print(f"      [capture] tier1 miss — page.url={page.url!r} == list base, no top-nav")

    dialog = fl.locator(S.DETAIL_DIALOG[0]).first

    href = _href_from_dialog(dialog, list_page_url)
    if href:
        if dbg:
            print(f"      [capture] tier2 HIT — campaign anchor href in dialog: {href}")
        return href, True   # got it with zero navigation — the reliable path
    if dbg:
        print("      [capture] tier2 miss — no campaign-looking <a href> in the dialog")

    expand = dialog.get_by_role("button", name="Expand to full page").first
    try:
        if expand.count() == 0:
            if dbg:
                print("      [capture] tier3 skip — no 'Expand to full page' button present")
            return None, True
    except Exception:
        return None, True

    pre_page, pre_frame = page.url, _frame_url(page)
    try:
        pacer.maybe_hover(expand)
        pacer.page_delay()
        expand.click(timeout=cfg.click_timeout_ms)
        time.sleep(1.5)
    except Exception as e:
        print(f"    (expand failed: {(str(e).splitlines() or ['?'])[0]})")
        return None, True

    post_page, post_frame = page.url, _frame_url(page)
    url = None
    if post_page != pre_page and _looks_campaign_url(post_page, list_page_url):
        url = post_page                       # preferred: clickable whop.com URL
    elif post_frame and post_frame != pre_frame:
        url = post_frame                      # fallback: frame-level (apps.whop.com)
    if dbg:
        print(f"      [capture] tier3 diff — page {pre_page!r} -> {post_page!r}; "
              f"frame {pre_frame!r} -> {post_frame!r}; picked url={url!r}")

    list_ok = _return_to_list(page, fl, list_page_url, pacer)
    return url, list_ok


def scrape_detail(page, fl, name, list_page_url, pacer, cfg):
    """Open the dialog for `name`, extract it, capture its locator, always close it."""
    dialog = open_detail(page, fl, name, pacer, cfg)
    detail, loc = {}, {}
    try:
        _scroll_dialog(page, pacer)
        _expand_dialog_text(dialog)   # reveal any collapsed "see more" rules before reading
        detail = extract.extract_detail(dialog)
        # PRIMARY: the app-frame route carries the campaign UUID once the modal is open
        # (confirmed via --test-capture). Navigation-free and reliable — also grabs brief_url.
        loc = _capture_locator(page, fl, list_page_url)
        # The frame-UUID is the locator; the built whop.com URL doesn't resolve, so `url` is
        # deliberately None. Only when the UUID ITSELF is missing (rare) do we fall back to the
        # old anchor/expand url tiers — so the fragile expand-and-return navigation never runs
        # on the happy path.
        if not loc.get("campaign_id"):
            url, _list_ok = capture_campaign_url(page, fl, list_page_url, pacer, cfg)
            loc["url"] = url
    finally:
        # If the fallback navigated away and back, the dialog is already gone; if it stayed
        # open (the normal path), Escape closes it. Either way this is safe.
        close_detail(page, fl)
    detail["url"] = loc.get("url")
    detail["campaign_id"] = loc.get("campaign_id")
    detail["brief_url"] = loc.get("brief_url")
    detail["frame_url"] = loc.get("frame_url")
    # modal_requirements_text comes from the frame-eval capture, but that path can come back
    # EMPTY even when the dialog rendered fine (the frame lookup failed while the dialog Locator
    # worked). Backfill from the dialog's own innerText so the on-modal rules are never lost.
    detail["modal_requirements_text"] = loc.get("modal_requirements_text") or detail.get("dialog_text")
    # And re-derive the clipper's full rules from whichever modal text we ended up with, so a
    # backfilled body still yields real rules rather than the pointer bullet.
    detail["modal_rules_text"] = detail.get("modal_rules_text") or extract.full_modal_rules(
        detail.get("modal_requirements_text"), detail.get("rules_text"))
    detail["resource_links"] = loc.get("resource_links") or []
    detail["modal_stats"] = loc.get("modal_stats")
    return detail


def _has_cached_detail(rec):
    """True when we hold a REAL scraped detail record for a campaign, not just a card-only
    stub. A scraped record stamps `scraped_at` (and carries rules/source detail); card-only
    stubs (`skipped_prefilter` / `not_listed_this_run` never-scraped) leave it None. This is
    the single definition of "known" used for BOTH pre-filter exemption and the
    refresh-vs-scrape partition, so a stub is treated as UNREACHED — re-evaluated by the
    pre-filter and actually scraped if it now passes — never routed to the free-refresh path
    where it would rank on empty detail and never get scraped."""
    return bool(rec and rec.get("scraped_at"))


def run_detail_pass(page, fl, survivors, prev_by_id, state, pacer, cfg, deadline_ts,
                    refresh, completed_ids=None):
    print(f"Phase 2 — detail pass on {len(survivors)} survivors "
          f"({'full refresh' if refresh else 'delta: unreached campaigns first'})...")
    if fl is None:
        print("  App frame not available — cannot open detail dialogs.")
        return [], []
    completed_ids = set(completed_ids or [])

    # Partition survivors. DONE campaigns (the clipper has exhausted them) are dropped —
    # neither scraped nor ranked. Of the rest, a campaign we already hold REAL cached detail
    # for (`_has_cached_detail`, not merely a card-only stub in prev_by_id) is KNOWN: it needs
    # NO browser visit and must not consume any session budget — just a free card-level
    # refresh, and it STAYS a ranked candidate. Everything else — never scraped, INCLUDING
    # card-only stubs from a prior pre-filter skip — is UNREACHED and gets the ENTIRE session
    # budget (so a stub that now passes the looser pre-filter finally gets scraped for detail
    # instead of ranking on empties). `--refresh` re-scrapes everything.
    to_refresh, to_scrape, n_done = [], [], 0
    for c in survivors:
        cid = c.get("id")
        if cid in completed_ids:
            n_done += 1
        elif (not refresh) and _has_cached_detail(prev_by_id.get(cid)):
            to_refresh.append(c)
        else:
            to_scrape.append(c)
    if n_done:
        print(f"  {n_done} clipper-completed campaign(s) skipped (on the DONE list).")

    results, new_ids = [], []

    # 1) KNOWN campaigns — cheap, NO browser, NO pacing, NO session budget. They remain
    #    ranked candidates (a scraped campaign is NOT done until the clipper says so); we
    #    only refresh volatile card-level numbers (budget paid/remaining, pay) from the list
    #    card and reuse all cached detail + analysis from campaigns.json. Cross-run budget
    #    trends still work off these card-level deltas.
    for c in to_refresh:
        rec = dict(prev_by_id[c["id"]])
        for k in ("budget_paid", "budget_total", "budget_remaining_fraction"):
            if c.get(k) is not None:
                rec[k] = c[k]
        if c.get("pay_per_1k") is not None:
            rec["pay_per_1k"] = c["pay_per_1k"]
            rec["pay_unit"] = c["pay_unit"]
            rec["pay_value"] = c["pay_value"]
        rec["status"] = "refreshed"
        rec["refreshed_at"] = _now_iso()
        results.append(rec)
    print(f"  {len(to_refresh)} known campaign(s) refreshed from the list (no re-scrape, "
          f"still ranked); {len(to_scrape)} unreached to scrape with the full budget.")

    # 2) UNREACHED campaigns — the ENTIRE session budget (max_campaigns / time) goes here,
    #    in list order, so successive runs keep filling the board where the last one stopped.
    consecutive_failures = 0
    processed = 0
    prev_name = None
    total = len(to_scrape)
    list_page_url = page.url  # baseline for detecting campaign URLs on expand
    for c in to_scrape:
        if processed >= cfg.max_campaigns:
            print("\n  Session cap: max campaigns reached — stopping cleanly.")
            break
        if time.monotonic() > deadline_ts:
            print("\n  Session cap: time budget reached — stopping cleanly.")
            break

        cid = c["id"]
        try:
            detail = scrape_detail(page, fl, c["name"], list_page_url, pacer, cfg)
            rec = build_record(c, detail, status="scraped")
            results.append(rec)
            if not state.is_known(cid):
                new_ids.append(cid)
            state.mark_scraped(cid, rec.get("url"))
            consecutive_failures = 0
        except StopRun:
            raise                       # HARD block (captcha / login wall) — end the run now
        except Exception as e:
            # RECOVERABLE (click timeout / slowness / transient): the click already retried
            # cfg.click_retries times, so this is a genuine per-card failure — but routine,
            # not a block. A real block would have raised StopRun above.
            consecutive_failures += 1
            log_error(cfg.errors_path, c.get("name"), e)
            # A mid-run block can also surface as an ordinary exception — check explicitly.
            if extract.is_challenge(page) or extract.is_login_wall(page):
                raise StopRun("challenge / login wall detected mid-run")
            # Half-opened dialog left over the list breaks the next click — recover first.
            _recover_list(page, fl, list_page_url, pacer)
            if consecutive_failures >= cfg.max_consecutive_failures:
                raise StopRun(f"{consecutive_failures} consecutive failures — likely a real "
                              "block or a page-structure change, not routine slowness")
            processed += 1
            continue

        processed += 1
        print(f"  {processed}/{total} unreached scraped · {len(new_ids)} new · "
              f"next break ~{pacer.campaigns_until_break}      ", end="\r")

        # rare human double-back: briefly re-open the previous campaign's dialog
        if prev_name and pacer.should_revisit():
            try:
                open_detail(page, fl, prev_name, pacer, cfg)
                pacer.page_delay()
            except StopRun:
                raise
            except Exception:
                pass
            else:
                close_detail(page, fl)
        prev_name = c["name"]

        pacer.tick()
        pacer.page_delay()

    print("")
    return results, new_ids


# --- assembly ------------------------------------------------------------------
def load_prev_campaigns(path):
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {c["id"]: c for c in data.get("campaigns", []) if c.get("id")}
    except Exception:
        return {}


def load_completed(path):
    """Set of campaign IDs the CLIPPER has processed/exhausted (the DONE list). The clipper
    writes `completed_campaigns.json` (or use --mark-done); these are the ONLY campaigns
    excluded from scraping AND ranking. Accepts either {"completed": [...]} or a bare list;
    ids may be strings or {"id": ...} objects. Never raises — a bad/missing file = no dones."""
    p = Path(path)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        items = data.get("completed", []) if isinstance(data, dict) else data
        return {(x.get("id") if isinstance(x, dict) else x) for x in items if x}
    except Exception:
        return set()


def save_completed(path, ids):
    """Persist the DONE list (sorted ids under a "completed" key). Atomic-ish write."""
    payload = {"completed": sorted(str(i) for i in ids if i)}
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def update_completed(path, ids, *, remove=False):
    """Add (or with remove=True, drop) campaign ids on the DONE list. Returns the new set."""
    current = load_completed(path)
    ids = {str(i) for i in ids if i}
    current = (current - ids) if remove else (current | ids)
    save_completed(path, current)
    return current


def assemble(results, skipped_records, prev_by_id, seen_ids, completed_ids=None):
    completed_ids = set(completed_ids or [])
    all_records, have = [], set()

    def _add(rec):
        cid = rec.get("id")
        if cid in have:
            return
        have.add(cid)
        all_records.append(rec)

    # Live results + prefilter-skipped, EXCLUDING clipper-completed (added once below as
    # status "completed" so they can't also appear as a ranked/skipped candidate).
    for rec in list(results) + list(skipped_records):
        if rec.get("id") in completed_ids:
            continue
        _add(rec)

    # Carry forward campaigns we knew about but didn't see listed this run (skip completed).
    seen = set(seen_ids)
    for cid, rec in prev_by_id.items():
        if cid in completed_ids or cid in seen or cid in have:
            continue
        carried = dict(rec)
        carried["status"] = "not_listed_this_run"
        _add(carried)

    # DONE campaigns (clipper-exhausted): preserved for history + shown in their own section,
    # but marked "completed" so they're EXCLUDED from ranking (and from scraping upstream).
    for cid in completed_ids:
        base = prev_by_id.get(cid)
        if base is None:
            continue  # nothing ever scraped for it — nothing to preserve
        done = dict(base)
        done["status"] = "completed"
        done["composite_score"] = 0        # terminal — never ranked (unambiguous, not stale)
        done["composite_breakdown"] = None
        done["pre_score"] = 0
        _add(done)

    # Cross-run + strategic signals need the FULL list (recurring-creator counts across
    # the whole run history). Runs before scoring so composite can weight them.
    strategic_mod.compute_strategic_signals(all_records)

    for rec in all_records:
        if rec.get("status") in ("scraped", "refreshed"):
            rec["pre_score"] = pre_score(rec)
            comp, breakdown = composite_score(rec)
            rec["composite_score"] = comp
            rec["composite_breakdown"] = breakdown
        else:
            rec.setdefault("pre_score", 0)
            rec.setdefault("composite_score", 0)
            rec.setdefault("composite_breakdown", None)
    return all_records


# --- throwaway URL-capture diagnostic (--test-capture) -------------------------
def run_test_capture(session, pacer, cfg, n):
    """THROWAWAY diagnostic: open the first `n` campaigns' detail modals on the LIVE DOM
    (open_detail -> heavy locator diagnostics -> capture_campaign_url -> close_detail) to find
    where the campaign locator actually lives. For each, it dumps every candidate source —
    top-page url, app-frame location.href (before/after open), all page frames, network
    requests fired during open, and the dialog's anchors / buttons / id+data-* attributes /
    embedded id tokens (+ full dialog HTML to probe_capture_<i>.html) — then runs the real
    capture_campaign_url so its (currently empty) result can be compared against them. Writes
    campaigns_test.json; NEVER touches the real campaigns.json. No ranking/footage/clips.

    Fails loud (exit 1) if the browser/login isn't usable — no cards, or no app frame."""
    global _CAPTURE_DEBUG
    _CAPTURE_DEBUG = True   # make capture_campaign_url narrate each tier
    page = session.page
    deadline_ts = time.monotonic() + cfg.max_minutes * 60  # generous; this is a short run

    # Capture network requests fired while a modal opens — the campaign id often rides in a
    # detail/GraphQL fetch URL even when nothing in the address bar or DOM changes.
    req_log = []

    def _on_request(req):
        try:
            u = req.url
            if re.search(r"campaign|app_|/api/|graphql|experience", u, re.I):
                req_log.append((req.method, u))
        except Exception:
            pass

    page.on("request", _on_request)

    cards = collect_cards(page, pacer, cfg, deadline_ts)
    if not cards:
        print("\n!! --test-capture: NO cards found — browser/login/selectors unavailable. "
              "Log in in the opened window, or run `python scout.py --probe` to diagnose. "
              "Aborting; nothing captured.")
        raise SystemExit(1)

    fl = get_app_frame_locator(page)
    if fl is None:
        print("\n!! --test-capture: app frame not available — cannot open detail dialogs. "
              "Aborting.")
        raise SystemExit(1)

    targets = cards[:n]
    print(f"\n== --test-capture (DIAGNOSTIC): {len(targets)} of {len(cards)} campaign(s) "
          f"through the full detail path, dumping every locator source ==")
    list_page_url = page.url
    records = []
    for i, c in enumerate(targets, 1):
        name = c.get("name")
        print(f"\n  [{i}/{len(targets)}] opening: {name} ...")
        top_before, fr_before = page.url, _frame_url(page)
        req_log.clear()
        try:
            dialog = open_detail(page, fl, name, pacer, cfg)
        except StopRun:
            raise  # challenge / login wall — fail loud, don't retry
        except Exception as e:
            msg = (str(e).splitlines() or ["?"])[0]
            print(f"      open failed: {type(e).__name__}: {msg}")
            records.append(build_record(c, {}, status="scrape_failed"))
            _recover_list(page, fl, list_page_url, pacer)
            continue

        # Modal is open — dump every candidate locator source BEFORE anything navigates.
        top_after, fr_after = page.url, _frame_url(page)
        _dump_capture_diagnostics(page, i, name, top_before, top_after,
                                  fr_before, fr_after, list(req_log))

        # Now run the REAL locator capture so we can see exactly what it yields vs. the
        # diagnostics: the frame-UUID primary, with the old url tiers only as a fallback.
        loc = {}
        try:
            loc = _capture_locator(page, fl, list_page_url)
            if not loc.get("campaign_id"):
                url, _list_ok = capture_campaign_url(page, fl, list_page_url, pacer, cfg)
                loc["url"] = url
        except Exception as e:
            print(f"      locator capture raised: {type(e).__name__}: {e}")
        rec = build_record(c, dict(loc), status="scraped")
        resolve_rules_readability(rec, fetch=True)   # incl. a Notion fetch if that's the only source
        req = (rec.get("modal_requirements_text") or "").replace("\n", " ")
        req = " ".join(req.split())
        stats = rec.get("modal_stats") or {}
        print(f"      => campaign_id={rec.get('campaign_id')!r}  locator_missing={rec.get('locator_missing')}")
        print(f"         frame_url  ={rec.get('frame_url')!r}")
        print(f"         modal_requirements_text[:200]: {req[:200]!r}"
              f"  (len={len(rec.get('modal_requirements_text') or '')})")
        rls = rec.get("resource_links") or []
        print(f"         resource_links ({len(rls)}):")
        for r in rls:
            print(f"            [{r.get('label')}] {r.get('url')}")
        if stats:
            print(f"         modal_stats: pay/1k={stats.get('pay_per_1k')} "
                  f"budget={stats.get('budget_paid')}/{stats.get('budget_total')} "
                  f"(rem {stats.get('budget_remaining_fraction')}) "
                  f"min_payout={stats.get('min_payout')} max/video={stats.get('max_per_video')}")
        else:
            print("         modal_stats: (none parsed)")
        rr = "READABLE" if rec.get("rules_readable") else ("UNREADABLE" if rec.get("rules_unreadable") else "no-rules")
        print(f"         rules: {rr} (source={rec.get('rules_source')})"
              + (f"  notion_rules_text len={len(rec.get('notion_rules_text') or '')}"
                 if rec.get("rules_source") == "notion" else "")
              + (f"  reason={rec.get('rules_unreadable_reason')}" if rec.get("rules_unreadable") else ""))

        try:
            close_detail(page, fl)
        except Exception:
            pass
        records.append(rec)
        pacer.page_delay()

    out = Path("campaigns_test.json")
    payload = {
        "generated_at": _now_iso(),
        "note": "THROWAWAY --test-capture diagnostic (URL capture only). Not a real run; "
                "the real campaigns.json was NOT touched.",
        "count": len(records), "campaigns": records,
    }
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n== locator-capture summary ==")
    got = sum(1 for r in records if r.get("campaign_id"))
    got_req = sum(1 for r in records if r.get("modal_requirements_text"))
    got_res = sum(1 for r in records if r.get("resource_links"))
    unreadable = sum(1 for r in records if r.get("rules_unreadable"))
    missing = sum(1 for r in records if r.get("locator_missing"))
    for r in records:
        flag = "MISSING" if r.get("locator_missing") else "ok"
        rls = r.get("resource_links") or []
        labels = ", ".join(f"{x.get('label')}" for x in rls) or "-"
        rr = "UNREADABLE" if r.get("rules_unreadable") else (r.get("rules_source") or "?")
        print(f"  [{flag:^7}] {(r.get('name') or '?')[:30]:30}  id={r.get('campaign_id') or '-'}  "
              f"rules={rr:<10} links={len(rls)} [{labels}]")
    print(f"\n  {got}/{len(records)} captured a campaign_id (UUID); "
          f"{got_req}/{len(records)} on-modal requirements; {got_res}/{len(records)} resource "
          f"link(s); {unreadable} EXCLUDED (rules unreadable); {missing} locator_missing.")
    print("  rules source per campaign: modal (on-page) / gdoc (Google Doc) / notion (fetched "
          "OK) / unreadable (Notion-only, not fetchable -> EXCLUDED) / no-rules.")
    print("  NOTE: campaign_id (UUID) is an IDENTIFIER only — the whop.com/<app>/<UUID> url does\n"
          "  NOT resolve (lands on Discover), so `url` is left None. The clipper reads rules from\n"
          "  BOTH modal_requirements_text AND the resource_links (rules docs + footage folders).")
    print(f"  Wrote {out} — the real campaigns.json was NOT touched.")
    print(f"  Per-campaign dialog HTML dumped to probe_capture_1..{len(records)}.html.")


# --- main ----------------------------------------------------------------------
def main():
    cfg = CONFIG
    parser = argparse.ArgumentParser(description="Scout — personal Whop Content Rewards scraper.")
    parser.add_argument("--force", action="store_true", help="ignore the 20h once-daily guard")
    parser.add_argument("--refresh", action="store_true", help="full re-scrape of every campaign")
    parser.add_argument("--recategorize", action="store_true",
                        help="clear the category cache and re-run the Groq categorizer on every "
                             "campaign (default reuses cached categories for unchanged content)")
    parser.add_argument("--probe", action="store_true", help="confirm selectors: screenshot + dump DOM")
    parser.add_argument("--test-capture", nargs="?", type=int, const=5, default=None, metavar="N",
                        help="THROWAWAY diagnostic: scrape only the first N campaigns (default 5) "
                             "through the full detail-modal path to exercise URL capture on the "
                             "real DOM; writes campaigns_test.json (NOT campaigns.json), prints a "
                             "capture summary, and does NO ranking / footage / proven-clips work")
    parser.add_argument("--mark-done", nargs="+", metavar="CAMPAIGN_ID",
                        help="mark campaign id(s) as clipper-completed (DONE list — excluded "
                             "from future scraping AND ranking), then exit")
    parser.add_argument("--unmark-done", nargs="+", metavar="CAMPAIGN_ID",
                        help="remove campaign id(s) from the DONE list, then exit")
    args = parser.parse_args()

    # DONE-list bookkeeping — pure file edits, no browser. Handle and exit.
    if args.mark_done or args.unmark_done:
        if args.mark_done:
            done = update_completed(cfg.completed_path, args.mark_done)
            print(f"Marked {len(args.mark_done)} campaign(s) DONE.")
        if args.unmark_done:
            done = update_completed(cfg.completed_path, args.unmark_done, remove=True)
            print(f"Removed {len(args.unmark_done)} campaign(s) from DONE.")
        print(f"DONE list now has {len(done)} campaign(s) -> {cfg.completed_path}")
        return

    state = State(cfg.state_path)

    # Once-daily guard (skipped for probe + the throwaway --test-capture diagnostic).
    if not args.probe and args.test_capture is None:
        hrs = state.hours_since_last_run()
        if hrs is not None and hrs < cfg.min_hours_between_runs and not args.force:
            wait = cfg.min_hours_between_runs - hrs
            print(f"Last run was {hrs:.1f}h ago. Once-daily guard: wait ~{wait:.1f}h "
                  f"or pass --force. Not today — browse manually if you like.")
            return

    profile = Path(cfg.profile_dir)
    first_run = not profile.exists() or not any(profile.iterdir()) if profile.exists() else True

    pacer = Pacer(
        base_delay=cfg.base_delay, fast_delay=cfg.fast_delay, fast_chance=cfg.fast_chance,
        afk_every=cfg.afk_every, afk_break=cfg.afk_break,
        long_afk_chance=cfg.long_afk_chance, long_afk_break=cfg.long_afk_break,
        revisit_chance=cfg.revisit_chance, scroll_step=cfg.scroll_step,
        scroll_pause=cfg.scroll_pause,
    )

    with Session(profile_dir=cfg.profile_dir, viewport=cfg.viewport, headful=True) as session:
        ensure_logged_in(session, first_run, pacer, always_prompt=args.probe)

        if args.probe:
            run_probe(session, pacer)
            return

        if args.test_capture is not None:
            run_test_capture(session, pacer, cfg, args.test_capture)
            return

        page = session.page
        start = time.monotonic()
        deadline_ts = start + cfg.max_minutes * 60
        errors_before = _count_errors(cfg.errors_path)

        stopped_reason = None
        results, new_ids, skipped = [], [], []
        cat_summary = None
        cards = []
        completed_ids = load_completed(cfg.completed_path)
        if completed_ids:
            print(f"  DONE list: {len(completed_ids)} clipper-completed campaign(s) will be "
                  f"skipped from scraping + ranking.")
        try:
            cards = collect_cards(page, pacer, cfg, deadline_ts)
            # Load prior campaigns BEFORE pre-filtering so known campaigns can bypass the
            # gate (they cost no session budget and must not be demoted out of the rankings).
            prev_by_id = load_prev_campaigns(cfg.campaigns_path)
            # Only campaigns we hold REAL detail for are exempt (card-only stubs stay subject
            # to the looser pre-filter so they can be re-evaluated and actually scraped).
            known_detail_ids = [cid for cid, r in prev_by_id.items() if _has_cached_detail(r)]
            survivors, skipped_pairs = prefilter(cards, cfg, known_ids=known_detail_ids)
            skipped = [card_only_record(c, "skipped_prefilter", r) for c, r in skipped_pairs]
            print(f"  Pre-filter: {len(survivors)} survivors, {len(skipped)} skipped "
                  f"(known campaigns exempt).")

            fl = get_app_frame_locator(page)  # still on the list; cards clicked here
            results, new_ids = run_detail_pass(
                page, fl, survivors, prev_by_id, state, pacer, cfg, deadline_ts,
                args.refresh, completed_ids
            )

            # Derive every analysis dimension (min-payout, max-payout, views-per-
            # submission, category, disqualifiers, competition, age/velocity, trends).
            # prev_rec drives cross-run trends + first_seen.
            for rec in results:
                enrich_active(rec, cfg, prev_rec=prev_by_id.get(rec.get("id")))
                rec["pre_score"] = pre_score(rec)
            # RULES READABILITY — resolve where each campaign's rules are readable from and
            # fetch Notion when it's the ONLY source; campaigns whose rules can't be read are
            # flagged rules_unreadable and excluded from the ranked output (report + clipper).
            resolve_rules_readability_all(results)
            # SELF-SOURCED footage — fetch the Google-Doc rules text for footage-less gdoc
            # campaigns and flag any that tell clippers to find their OWN footage (un-clippable by
            # a footage-download pipeline). Bounded, cached, off-Whop, fail-open. Must run AFTER
            # footage_presence (set in enrich_active) so the footage-less gate is available.
            enrich_self_sourced_docs(results, cfg)
            # FOOTAGE SUBSTANCE INTAKE — runs FIRST (accessibility is a hard disqualifier,
            # so an undownloadable-footage campaign is sunk before we spend effort on it).
            # Judges what I'd actually be clipping, not just the stats. Cached per campaign.
            intake_mod.probe_campaigns(results, cfg, pacer)
            # FOOTAGE LINK-LIVENESS — a metadata-only "is the footage actually still there?"
            # check (distinct from intake's accessibility): removed videos, empty/locked Drive
            # folders, offline-live-only stream channels. Dead-and-nothing-alive -> heavy derank
            # (composite × cfg.liveness_dead_penalty), never an exclude. Cached per link, spaced,
            # fail-open. Off-Whop (hits YouTube/Kick), so it runs in the analysis phase.
            liveness_mod.probe_campaigns(results, cfg)
            social_mod.probe_sources(results, cfg.social_top_n, pacer)
            footage_mod.probe_campaigns(results, cfg.footage_top_n, pacer)
            # Measure repeatable clippability from dedicated clipper accounts. Runtime
            # is unlimited, so analyze EVERY non-disqualified campaign (not a top-N):
            # this is a top-two ranking lever and coverage matters more than speed.
            clips_mod.probe_campaigns(results, cfg, pacer)
            # Groq-based categorization — runs AFTER intake so footage TITLES are available.
            # Overwrites the cheap keyword baseline from enrich_active with the model's
            # primary/secondary/confidence. Batched + content-hash cached; degrades to keyword
            # when Groq is unavailable. Ranking (assemble) then buckets by the primary category.
            cat_summary = categorize_mod.categorize_campaigns(
                results, cfg, recategorize=args.recategorize)
        except StopRun as e:
            stopped_reason = str(e)
            print(f"\n!! STOP: {e}. Saving progress and exiting. Not today — browse manually.")
            prev_by_id = load_prev_campaigns(cfg.campaigns_path)

        seen_ids = [c["id"] for c in cards if c.get("id")]
        all_records = assemble(results, skipped, prev_by_id, seen_ids, completed_ids)

        # Category-level ranking on top of per-campaign scoring (per-campaign ranking stays).
        category_ranking = scoring.rank_categories(all_records, cfg.category_agg)

        report.write_json(cfg.campaigns_path, all_records, category_ranking=category_ranking,
                          category_summary=cat_summary)
        report.write_summary_md(cfg.summary_path, all_records, category_ranking=category_ranking,
                                category_summary=cat_summary)
        state.finish_run()

        report.terminal_report(
            all_records,
            db_total=state.known_count,
            new_count=len(new_ids),
            failures=_count_errors(cfg.errors_path) - errors_before,
            category_ranking=category_ranking,
            category_summary=cat_summary,
        )
        if stopped_reason:
            print(f"\n(Run ended early: {stopped_reason})")


def _count_errors(path):
    p = Path(path)
    if not p.exists():
        return 0
    try:
        return sum(1 for _ in p.open(encoding="utf-8"))
    except Exception:
        return 0


if __name__ == "__main__":
    main()
