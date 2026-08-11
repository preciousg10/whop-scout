# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Scout is a **personal, single-user research tool** that scrapes the user's *own*
logged-in Whop Content Rewards campaigns. It is intentionally low-volume, human-paced,
and once-daily. It is not a crawler and must never become one. The whole design
premise is "indistinguishable from me manually browsing my own account," so several
constraints below are hard requirements, not preferences.

## Commands

```powershell
# setup
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium

# confirm selectors (login is manual in the opened window, then press Enter)
python scout.py --probe

# normal daily run
python scout.py

# flags
python scout.py --force     # ignore the 20h once-daily guard
python scout.py --refresh   # full re-scrape of every campaign (default is delta)
python scout.py --recategorize  # clear the category cache + re-run the Groq categorizer

# DONE list — mark campaign(s) the CLIPPER has exhausted (excluded from scraping AND
# ranking; "already scraped" is NOT done). Pure file edit, no browser; writes
# completed_campaigns.json. The clipper appends to this itself via the handoff queue later.
python scout.py --mark-done <campaign-id> [<id> ...]
python scout.py --unmark-done <campaign-id> [<id> ...]   # restore to the board

# syntax check all modules
python -m py_compile scout.py pacing.py browser.py state.py extract.py scoring.py footage.py social.py report.py selectors.py proven_clips.py intake.py strategic.py

# probe a campaign's footage substance directly (accessibility/type/volume/density)
python intake.py https://www.youtube.com/watch?v=<id> https://drive.google.com/drive/folders/<id>

# analyze a creator's clippability directly via clipper-account discovery
# (name first; optional own-channel URLs feed only the low-weight reference)
python proven_clips.py "MrBeast" https://youtube.com/@MrBeast
```

There is no test framework wired up. The DOM-agnostic parsers in `extract.py`
(`parse_pay`, `parse_money`, `parse_remaining_fraction`, `parse_platforms`,
`parse_int`, `campaign_id_from_url`, `parse_min_payout`, `min_views_to_payout`,
`parse_min_view_threshold`, `references_resource_doc`, `extract_handles`, `parse_max_payout`,
`participants_per_1k_budget`, `payout_velocity`,
`detect_disqualifiers`, `classify_openness`, `classify_category`, `classify_categories`) are pure functions with no
Playwright dependency — test them by importing `extract` directly, no browser needed. `scoring.py`
(`pre_score`, `clippability`, `composite_score`, `pay_rate_factor`, `reach_factor`,
`expected_earnings`, `earnings_factor`, `core_signals_known`, `data_confidence_factor`,
`openness_factor`, `repeatable_factor`, `max_payout_factor`, `min_view_threshold_factor`,
`velocity_factor`/`_band`,
`competition_factor`, `compute_trends`, `content_type_factor`, `footage_supply_factor`,
`action_density_factor`, `footage_access_factor`, `style_fit`, `rank_categories`,
`project_budget_drain`,
`participant_growth`, `source_saturation_estimate`, `account_reusability`, `rank_categories`,
and their `*_factor` companions) and `social.parse_count` are likewise pure. `categorize.py`'s
signal/hash/prompt/parse/validate layer (`campaign_signals`, `content_hash`, `build_batch_prompt`,
`parse_batch_response`, `validate_result`, `keyword_result`) is testable with no Groq/network.
`strategic.py`
(`_norm_creator`, `compute_strategic_signals`) is testable with plain dicts. So is `intake.py`'s analysis layer (`classify_source`, `content_type_from_brief`,
`classify_content_type`, `analyze_transcript_density`, `aggregate_access`,
`footage_volume`) — test the substance logic with no network. So is the analysis layer of `proven_clips.py` (`clip_performance`,
`aggregate_clippability`, `score_candidate`, `filter_clips`, `analyze_text_patterns`,
`analyze_lengths`, `analyze_cadence`, `extract_template`, `median`, `percentile`) — no
yt-dlp/network needed to test the legitimacy/consistency/template math.

## Architecture

Runtime is standalone (`python scout.py`). The ONE model touchpoint is campaign
categorization (`categorize.py`, Groq — see below), which degrades to the keyword tagger when
Groq is unavailable so a run never depends on it; nothing else calls an LLM. Flat
module layout; `scout.py` imports the others and none import back into it (no
circular deps). Data flows in one direction:

`browser.Session` (headful persistent Chromium)
→ **Phase 1** `collect_cards` scrolls the list, `extract.extract_card` per card
→ `prefilter` splits survivors vs. `skipped_prefilter` (recorded with reasons)
→ **Phase 2** `run_detail_pass` visits survivors, `extract.extract_detail` per page
   (known campaigns are NOT re-opened — their card-level budget/pay are refreshed from the
   list view and static detail is reused, so the session budget goes to NEW campaigns)
→ `enrich_active` derives every rules-level analysis dimension (see below)
→ **`intake.probe_campaigns` runs FIRST** (footage substance — accessibility is a hard
  disqualifier, so an undownloadable campaign is sunk before further effort)
→ `scoring.pre_score` → `social.probe_sources` + `footage.probe_campaigns` +
`proven_clips.probe_campaigns` (ALL non-disqualified campaigns)
→ `categorize.categorize_campaigns` (Groq primary/secondary/confidence — runs after intake so
footage titles exist; batched + content-hash cached; degrades to keyword)
→ `assemble` merges + carries forward, then `strategic.compute_strategic_signals` fills
the cross-run/strategic signals over the FULL list, then `composite_score` +
`rank_categories` (category ranking by primary category) → `report.*`.

**Cross-run + strategic signals (`strategic.py`, built on run history — NO new scraping).**
`enrich_active` accumulates a per-run `snapshot_history` (capped) alongside each `snapshot`;
`strategic.compute_strategic_signals(all_records)` then derives, per active campaign:
(1) **budget_drain** — projected days-until-empty with a sweet-spot band (stalled/too-fast
penalized); (2) **participant_growth** — growth rate + direction (fast=saturating penalty,
flat=opportunity bonus); (3) **source_saturation** — how mined-out the footage is, reusing
the clips `proven_clips` already found via its "<creator> clips" search (no new calls);
(4) **recurring_creator** — distinct campaigns this creator/brand has run across the whole
`campaigns.json` history (name-matched), weighted positively; (5) **account_reusability** —
can this feed an existing category account or does the brief demand a fresh branded one.
Each degrades to UNKNOWN/neutral. (6) A **personalized performance-learning** weight is only
a stub — `scoring.performance_factor` (returns 1.0) + the `my_performance.json` schema; the
learning logic is deliberately unbuilt.

**Footage SUBSTANCE (`intake.py`) — judges what you'd actually be clipping, not just the
stats.** Four metadata-only dimensions (NEVER downloads full video), cached per campaign
by the source-link set, each failure isolated, UNKNOWN kept neutral (never guessed):
(1) **accessibility** — is each source reachable? HTTP-first (YouTube `playabilityStatus`,
Drive folder access markers, direct-file HEAD) so it works WITHOUT yt-dlp; detects login
walls, Kick 403, private/locked Drive, dead links. No usable source → `footage_inaccessible`
disqualifier (composite 0). (2) **content type** — `standard_stream_vod`/`podcast_interview`
(clippable) vs `slideshow_photo`/`music_video`/`short_form_only_unusual`/`ugc_requires_my_face`/
`other` (heavy penalty). Real source durations override the brief — this is what catches the
"Odyssey" trap (two 2-min trailers → `short_form_only_unusual`). (3) **volume/refresh** —
total hours (when durations are readable) + one-time-vs-recurring (a channel is recurring;
weighted higher) + recency. (4) **action density** — eventful vs logistics/dead-talk fraction
from subtitles (yt-dlp `--write-auto-subs`, subtitles only); UNKNOWN when no transcript.
**yt-dlp is optional**: HTTP handles YouTube-video + Drive; yt-dlp adds channel volume/cadence,
Kick cookies, and subtitle text for density (set `cfg.cookies_from_browser` for gated VODs).

**Ranking model (`scoring.composite_score`).** Composite is a visible product of levers,
each unknown input mapping to a NEUTRAL 1.0 (surfaced as UNKNOWN, never guessed).
**VIEWS DOMINATE RATE** by design: pay rate is only a modest nudge (`pay_rate_factor`,
~0.75–1.4×), while creator reach (`reach_factor`, ~0.6–2.8×) and **expected earnings per
clip** (`earnings_factor`, ~0.3–2.5×) drive the rank — a $0.50/1k campaign for a huge,
highly clippable creator beats a $3/1k one for a small creator whose clips get 800 views.
`base = budget_remaining × clippability × reach_factor × pay_rate_factor`, then
`× confidence × min_penalty × below_min_penalty × earnings_factor × data_confidence_factor`
and the analysis levers.
**Expected earnings per clip** = proven clipper MEDIAN views (`repeatable_clippability.
median_views`, from `proven_clips`) × `pay_per_1k` / 1000 — the number that actually
matters (what a typical clip earns me), UNKNOWN/neutral when clipper data or the rate is
missing (`scoring.expected_earnings`), surfaced prominently in `campaigns_summary.md`
alongside the rate. The strict **minimum-payout gate** (`below_min_penalty` 0.15) fires
when those PROVEN expected views fall below `min_views_to_payout` — a typical clip earns
$0 — and only when both numbers are known.
A DISTINCT **minimum-VIEW payout gate** (`min_view_threshold` / `min_view_threshold_factor`)
handles campaigns that pay $0 until a single video crosses a hard VIEW count ("VIDEO MUST
REACH 10K FOR PAYOUT"). `extract.parse_min_view_threshold` reads it from the modal
requirements + rules text (explicit "must reach / minimum / … for payout / … to be paid"
language next to a view count; pay rates like "$1/1K views" and dollar minimums are never
misread as gates), and the penalty scales hard with the threshold — ~0.6× at 1K, ~0.12×
(SEVERE) at 10K, ~0.04× at 50K+ — because a zero-audience start rarely clears it. Shown as a
`MIN-VIEW-THRESHOLD` flag in the report. Unlike `below_min_penalty` it needs no proven-clipper
data, so it fires as soon as the threshold is detected. UNKNOWN/no-gate → neutral 1.0.

**Data-confidence factor (`data_confidence_factor` × `core_signals_known`).** Because every
UNKNOWN maps to a neutral 1.0, a campaign with NO real data could float to the top on nothing
but neutrals (a huge fresh budget alone once made "Call of Duty" #1 with UNKNOWN clippability/
earnings/reach/footage/content-type). So composite is discounted by how many of **5 CORE
signals** were actually measured — repeatable clippability, expected earnings/clip, creator
reach, footage accessibility, content type: 5/5 → 1.0 (no penalty) down to 0/5 → 0.35
(heaviest). An unverified campaign is a gamble; a proven one a known quantity, and should
outrank it. The count is shown as "X/5 core signals known" per campaign, and **ties break
toward the more-known campaign** (`report`/`terminal_report` sort key).

**Data-coverage report (`report._coverage_md`/`_coverage_rows`).** Because most levers map
UNKNOWN→neutral, a ranking can be built on almost no real signal. So every run leads
`campaigns_summary.md` (and the terminal report) with a coverage table over the ranked
campaigns — footage substance/accessibility/volume/density, repeatable-clippability,
creator reach, expected-earnings/clip. Coverage is measured **known / ACHIEVABLE** (how many
campaigns even expose each signal's prerequisite — a resolvable handle for reach, a source
link for footage, a creator name for clips), NOT known/total, because several signals are
structurally unavailable pre-join; comparing against 100% would make a run working as well as
the data allows look broken. Each row shows known/achievable, % of achievable, and the
**ceiling** (achievable/total). Banners: "⚠️ under half of AVAILABLE data" when <50% of
achievable, and "⚠️ STALLED SIGNAL(S)" when any signal captured 0 of a non-trivial available
pool (catches a working feature that broke, e.g. the reach-lookup regression).

The **clip-quality** lever is
`repeatable_factor` (proven-clippability, 0.5–2.5×), from `proven_clips`'s pooled-median +
consistency + cross-clipper spread. Views-per-submission was DROPPED (an average over a
fat-tailed clip distribution — two viral clips out of 2,000 read "healthy" while everyone
else earned nothing — so it can't distinguish "clips reliably land" from "two got lucky";
the stats chart is no longer scraped at all). Supporting: `max_payout_factor` (a low per-video cap penalizes the viral
upside; uncapped is best), `velocity_factor` (paid/total/day — an OLD campaign creeping
along is a strong negative; a FRESH one is neutral, never penalized), `competition_factor`
(participants per $1k budget), and the four **footage-substance** factors from `intake.py`:
`content_type_factor` (non-standard = heavy penalty), `footage_supply_factor` (recurring >
one-time), `action_density_factor` (logistics-heavy = heavy penalty), `footage_access_factor`
(partial accessibility penalized; full inaccessibility disqualifies). **Style fit**
(`scoring.style_fit`) is a tunable multiplier ALONGSIDE the money signals (it never replaces
them) that scores how well a campaign fits a CHAOTIC/high-energy clip channel — stream
highlights / reactions / gaming / action / memes rank ABOVE polished produced content
(jewelry, corporate, music videos). It's built only from signals already captured — `category`
(`extract.classify_category`) plus footage `content_type` + `action_density` (`intake.py`), so
NO new scraping. One knob tunes it: `scoring.STYLE_FIT_WEIGHT` (default 0.6 → a 0.4×–1.6× swing;
`0.0` disables it entirely, toward `1.0` lets style dominate) via `factor = 1 + WEIGHT*(2*fit-1)`,
with the per-category chaos affinities in `STYLE_FIT_CATEGORY` (+ `STYLE_FIT_CONTENT`/
`STYLE_FIT_DENSITY` nudges) editable to re-profile the channel; neutral/unknown → exactly 1.0.
Surfaced per campaign in the report ("Style fit: …") and in the composite breakdown. A campaign
with any **hard disqualifier** (rules-level OR `footage_inaccessible`) is forced to composite 0 — it
sinks but is shown in the report's DISQUALIFIED section with reasons. On top of these,
`composite_score` multiplies the six cross-run/strategic factors from `strategic.py` (budget
drain, participant growth, source saturation, recurring creator, account reusability, and the
neutral `performance_factor` stub) — see the "Cross-run + strategic signals" note above.

**Categorization (`categorize.py`, Groq).** Replaces the weak name-only keyword tagger (which
dumped ~182 campaigns into "other", missing obvious ones like "Jesser x ClipFarm"=sports). A
Groq model reads whatever signal each campaign exposes — name (always), `modal_requirements_text`
(when present, truncated), footage/resource TITLES (the strings intake already captured, first
few, **titles as TEXT — never downloading video**), and creator (tiebreaker) — and returns a
`primary_category` from a FIXED set (`categorize.CATEGORIES`: sports/streamer_irl/podcast_talking/
gaming/music/brand_product/meme/news/movie_tv/other), optional `secondary_categories`, and a
`category_confidence` (high/low). Guards: the model may ONLY pick from the fixed list (anything
else → "other"); insufficient/conflicting signals → "other"/low, never a guessed label; calls are
BATCHED (`cfg.category_batch_size`, default 20) to respect Groq free-tier limits; results are
CACHED keyed to a hash of each campaign's CONTENT (`cfg.category_cache_path`) so unchanged
campaigns skip Groq and edited ones re-categorize (`--recategorize` clears the cache). The pass
runs AFTER intake (so footage titles exist), prints a 20-campaign sample for eyeballing, and
returns a breakdown (per-category counts, low-confidence total, how far "other" shrank vs the
keyword tagger) shown in the report. It **degrades to the keyword tagger** when Groq is
unavailable (no `GROQ_API_KEY` / no `groq` package / `SCOUT_OFFLINE=1`), marked `category_source`
= groq|cache|keyword_fallback — a run never depends on the model. Pure helpers (`campaign_signals`,
`content_hash`, `build_batch_prompt`, `parse_batch_response`, `validate_result`, `keyword_result`)
are network-free/testable.

**Category-level ranking (`scoring.rank_categories`, on top of per-campaign scoring — the
per-campaign ranking is unchanged and still primary).** Each rankable campaign counts toward
exactly ONE category — its **primary** `rec["category"]` (from the Groq categorizer) — so
secondary tags never inflate counts (`rec["categories"]` = primary+secondaries is kept for
reference only). A category's score is an aggregate of its members' composites, chosen by the
`cfg.category_agg` knob (`top5` default | `top3` | `top10` | `average` | `best`; unknown →
default). Only rankable campaigns count (`_is_rankable`: scraped/refreshed AND not disqualified /
rules_unreadable / prohibited) — so existing exclusions are respected; the min-VIEW-threshold
penalty is NOT an exclusion, it already lives in the composite, so a view-floored campaign still
counts with its penalized score. A category with fewer than `CATEGORY_FULL_MIN` (5) members is
flagged `thin` (its score rests on a small sample). The ranking (highest score first, each with
count/thin/top campaigns) is written to `campaigns.json` (`category_ranking`), the summary MD
("## Category ranking"), and the terminal report.

**`enrich_active(rec, cfg, prev_rec)`** (in scout.py) fills, per active record: min-payout
viability, `max_payout_per_video`/`uncapped`, `participants_per_1k_budget`, `category`
(`extract.classify_category`), `open_to_all` (`extract.classify_openness` — yes/no/unclear),
`disqualifiers` (`extract.detect_disqualifiers` — gambling/face-or-voice/min-followers/geo/
non-clippable-format/paid-ad-spend/non-English/gated-footage/**application-gated**),
`first_seen_at`+`days_active`+`payout_velocity`, `min_view_threshold`
(`extract.parse_min_view_threshold`), the `style_fit` inputs, and the per-run `snapshot` +
cross-run `trends` (`scoring.compute_trends` vs `prev_rec.snapshot`: accelerating/steady/
stalling/dead, off budget-paid + participant deltas). It also runs the **resource-capture
cross-check**: when the modal text references a linked Doc/Drive/Notion/folder
(`extract.references_resource_doc`) but `resource_links` came back EMPTY, it sets
`capture_suspect`=True (+`capture_suspect_reason`, the matched phrase) so a silently-missed doc
surfaces as a `CAPTURE-SUSPECT` report warning instead of passing as "no docs" — DETECTION
ONLY, it never touches capture logic. It reads only already-scraped fields, so it's pure/cheap
and idempotent.

**Application/selection gate (`open_to_all` + the `application_gated` disqualifier).** A
campaign that isn't an instant open join — you must apply, be accepted/approved, get invited,
or wait for a spot (e.g. Medal's "Content Program") — is unusable for a pipeline that must
start clipping immediately, so it's a HARD disqualifier (composite 0, shown in the DISQUALIFIED
section with the matched text). Detected from BOTH the brief (`extract._DQ_APPLICATION`,
deliberately scoped so "terms apply"/"accepted formats"/"approval rate"/"invite code" don't
false-fire) and the page CTA (`extract._detect_join_cta` reads whether the join button says
"Apply" vs "Join"; unconfirmed selector, degrades to None). `extract.classify_openness` sets
`open_to_all` = **no** (gated → DQ), **yes** (clearly open — instant join / anyone can join),
or **unclear** (no signal → neutral). Scoring's `openness_factor` weights yes 1.1× / unclear
1.0× / no 0.5× (no is moot — already DQ'd). Briefs are often terse ("refer to Resources"), so
the page-CTA signal is what catches most real gates on a live run.

Key module responsibilities:
- **`scout.py`** — the `Config` dataclass at the top is the single source of truth for
  every tunable (pacing, caps, pre-filter thresholds, output paths). Orchestration,
  CLI, login flow, session caps, and the `StopRun` abort path all live here.
- **`selectors.py`** — every Whop DOM hook, each a list of candidate selectors tried
  in order. This is the *only* place selectors belong; extraction logic never inlines
  them. **Selectors are unconfirmed** in this build (see below).
- **`extract.py`** — two layers: pure text parsers (safe to change/test freely) and
  Playwright extraction helpers that feed those parsers text pulled via `selectors.py`.
  Extraction never raises on a missing field — it returns `None`.
- **`pacing.py`** — the `Pacer` owns all timing/human-behavior. Anything that adds a
  delay, break, scroll, or hover goes through it so the behavior stays centralized and
  auditable.
- **`state.py`** — `state.json`: the 20h guard (`hours_since_last_run`) and known
  campaign IDs that drive delta/resume. Campaign *data* lives in `campaigns.json`, not
  here.
- **`strategic.py`** — cross-run + strategic signals from run history (no new scraping):
  budget drain / participant growth (from `snapshot_history`), source saturation (reuses
  proven_clips results), recurring creator (name-matched across `campaigns.json`), account
  reusability. `compute_strategic_signals` runs in `assemble` before scoring. Pure math +
  factors live in `scoring.py`. The `my_performance.json` (user-maintained ground truth of
  my real results) + `scoring.performance_factor` are the unbuilt learning hook.
- **`intake.py`** — footage SUBSTANCE probe (ported/adapted from the clipper project's
  `intake.py`/`analyze.py`/`download.py`). Accessibility + content-type + volume/refresh +
  action-density, metadata-only, HTTP-first (yt-dlp optional), cached per campaign,
  never-raise. `probe_campaigns` runs before the social/footage/clipper passes and can add
  the `footage_inaccessible` disqualifier. See the "Footage SUBSTANCE" note above. Also a
  standalone CLI: `python intake.py <source_url> ...`.
- **`categorize.py`** — Groq campaign categorizer (see the "Categorization" note above). Mirrors
  the sibling clipper's Groq client (SDK, `llama-3.3-70b-versatile`, rate-limit backoff). Pure
  signal/hash/prompt/parse/validate helpers are network-free; the orchestration batches, caches
  by content hash, prints a sample, and degrades to `extract`'s keyword tagger. The `groq`
  package + `GROQ_API_KEY` are the only external dependency, and their absence is non-fatal.

**Rules readability + Notion exclusion (`resolve_rules_readability`).** The clipper's intake
can read on-modal rules and Google Docs but NOT Notion pages (JS-rendered). Clipping a campaign
whose banned-word list is unknown is a compliance risk, so scout resolves per campaign WHERE the
rules are readable from: `modal` (substantive on-page requirements — `_modal_rules_section` +
`_has_substantive_rules`, which ignore pointer-only sections like "Refer to the Google Docs"),
`gdoc` (a `docs.google.com` resource link — intake reads it), or, ONLY when rules live solely in
Notion, it fetches that public page over HTTP (`_fetch_notion_text`: `__NEXT_DATA__` + visible
text + meta; needs ≥400 usable chars) → `notion` (stored as `notion_rules_text`) or, on
failure, `rules_unreadable=True` with a reason. **rules_unreadable campaigns are EXCLUDED from
the ranked/active output** (`report` segregates them into an "Excluded — rules unreadable"
section; `pickcampaign.rank_campaigns` skips them) but kept in `campaigns.json`. Notion that is
merely REDUNDANT (real rules also on-modal or in a Doc) never drops a campaign. Runs in `main`
after `enrich_active`; only the only-in-Notion cases hit the network. `--test-capture` prints
each campaign's `rules_source`.
- **`proven_clips.py`** — answers "is THIS creator repeatably clippable?" via
  DEDICATED CLIPPER-ACCOUNT IDENTITY, fully automatically (no human confirmation). It
  (1) DISCOVERS clipper accounts by searching **YouTube** (`yt-dlp ytsearch`, primary;
  TikTok/IG are optional bonus and their absence never lowers confidence), (2)
  AUTO-SCORES each candidate's legitimacy as a dedicated clipper of this creator
  (`score_candidate`: creator-name-in-handle + short-clip share + reposts-creator;
  edit/montage/AMV/compilation/tribute accounts hard-excluded) and auto-trusts those
  above `clipper_trust_threshold`, (3) NOISE-FILTERS clips inside trusted accounts
  (`filter_clips`: keep only 15–90s, drop edit/AMV/AI/best-of/top-10/tribute captions;
  filtered counts reported), (4) AGGREGATES across trusted clippers
  (`aggregate_clippability`): pooled median views, consistency (many 10k–50k beats a
  lone 1M outlier), clipper count, and median views RELATIVE TO EACH CLIPPER'S OWN
  followers (small clippers pulling big views = content carries the clip = the signal
  that matters for a zero-audience start). That aggregate is the PRIMARY score; the
  creator's own channel is a separate LOW-WEIGHT reference (`analyze_creator_reference`,
  blended at 10%). yt-dlp metadata only, NO downloads. Fills `repeatable_clippability`
  on each top campaign (feeds the heavy `repeatable_factor` in `composite_score`) and
  writes `campaign_template.json` (winning-clip length/caption/cadence patterns, per
  campaign, for the clipper). We deliberately DON'T match clips back to a specific
  content stream (constraint #7). **No trusted clippers found → UNKNOWN, score None —
  fail loud, never fabricate.** Optional `clip_farms.json` (`by_campaign_id` /
  `by_creator`) seeds extra candidate accounts (still auto-scored). A `content_analysis`
  field + `analyze_clip_content` stub are the deliberate hook for a FUTURE
  download+vision pass — **not built** (would need the no-downloads constraint lifted).
  **Analysis-phase performance (`probe_campaigns`).** Clippability is a CREATOR property,
  so campaigns are grouped by creator and each creator is discovered ONCE per run (dedupe)
  and CACHED across runs in `proven_clips_cache.json` (`_load_cache`/`_write_cache`, keyed
  by `_norm_creator`): a recurring creator is reused within its TTL and never re-fetched
  (`clips_cache_max_age_days` for a scored result, the shorter `clips_cache_unknown_age_days`
  to retry UNKNOWN sooner; the cache also seeds from clippability already carried on campaign
  records). Fetches run on a small thread pool (`clips_workers`, off-Whop yt-dlp needs no
  human pacing so `pacer=None` there) with a per-creator wall-clock timeout
  (`clips_campaign_timeout_s`) so one stuck lookup can't hang the phase (a stall is recorded
  UNKNOWN and its worker abandoned, bounded by yt-dlp's own subprocess timeouts). Progress is
  logged as "analyzing X/Y". Only **VIABLE survivors** are analyzed (`_clip_viable`): a
  scraped/refreshed, non-disqualified campaign that still has budget remaining and isn't
  flagged below-minimum-payout — the expensive discovery is NOT spent on marginal campaigns
  (they keep clippability UNKNOWN/neutral and are still ranked). `clips_analyze_all` covers
  every viable survivor; set it False to cap at the top `clips_top_n` by pre-score, or
  `clips_viability_floor=False` to restore full-coverage analysis of every non-DQ survivor.

## Delta / DONE list / session budget (important)

`run_detail_pass` **partitions survivors** up front: (1) **DONE** campaigns (on the DONE
list — the clipper has exhausted them) are dropped entirely; (2) **KNOWN** campaigns (we
already hold cached detail in `campaigns.json`) get a **free card-level refresh** — budget
paid/remaining + pay re-read from the list card, all detail + analysis reused — with **NO
browser visit, NO pacing, NO session budget**, and they STAY ranked candidates; (3)
**UNREACHED** campaigns (never scraped) get the **ENTIRE** session budget (200-campaign /
90-min caps apply ONLY to this loop). So each run spends its whole Whop budget filling in
never-seen campaigns and resumes where the last run's cap cut it off — it never re-scrapes
knowns to "refresh" them (that was the old bug: knowns paced through the loop and ate the
budget — "54 done, 1 new"). `--refresh` forces everything back into the unreached/scrape path.

**"Already scraped" ≠ "done".** A scraped campaign stays a valid ranked candidate until the
CLIPPER marks it exhausted. The DONE list (`completed_campaigns.json`,
`load_completed`/`update_completed`, `--mark-done`/`--unmark-done`) is the ONLY thing that
removes a campaign from scraping AND ranking; `assemble` marks those `status="completed"`
(composite forced to 0, shown in their own summary section, never ranked). The clipper writes
this file itself via the handoff queue later.

`load_prev_campaigns` + `assemble` merge new results, skipped records, carried-forward
`not_listed_this_run` campaigns, and completed ones. Changing this logic means touching the
partition in `run_detail_pass` and the completed/carry-forward handling in `assemble` together.

## Selectors are unconfirmed — the `--probe` workflow

Whop's live DOM was not accessible at build time (login is manual, on the user's
machine), so the selectors in `selectors.py` are educated placeholders. The rule is
**never guess selectors blind inside logic** — instead run `--probe`, which logs in,
screenshots a card + detail page, dumps `probe_*.html`, and prints sample extraction.
Fix `selectors.py` against those artifacts before trusting a real run. When extraction
returns mostly `None`, the selectors are wrong, not the parsers.

**The cards live inside a cross-origin app iframe — the single most important fact
about scraping Whop here.** The list page is a client-rendered Vite SPA (assets under
`/_web/assets/*.js`) whose served HTML is just a shell, AND the Content Rewards
experience is a Whop "app" embedded via `<iframe title="Content Rewards"
src=".../core/app/launch/?redirect=…apps.whop.com…">`. The campaign grid renders
**inside that frame** (origin `apps.whop.com`), so `page.content()` on the top document
returns zero cards no matter how long you wait. Everything runs against the app
**Frame**, not the Page:
- **Queries/scrolling/clicks go through a `FrameLocator`** (`get_app_frame_locator`,
  i.e. `page.frame_locator(...)`), which re-resolves the frame lazily on each call and
  survives the app re-rendering — unlike a captured `Frame` object, which goes stale.
- **`.content()` dumps and `.url` tracking use the `Frame` object** (`get_app_frame`,
  from `page.frames` matching `APP_FRAME_URL_HINT` = `apps.whop.com`) — a FrameLocator
  can't give those.
- `wait_for_feed` returns the **FrameLocator** (settles network, scrolls the top page
  so the iframe mounts past the hero, then polls *inside the frame* for cards). Returns
  it even when no cards were detected, so the probe can still query/dump.
- `pick_card_selector`, `extract.extract_card/extract_detail`, and card iteration take a
  `scope` that is the FrameLocator (Page only as fallback).
- `scroll_list` wheel-scrolls with the cursor over the iframe's bounding box so the
  frame's own feed scrolls. `is_challenge`/`is_login_wall` stay on the Page.

Navigation: `selectors.LIST_URL` (`/discover/app/app_...`) gets **bounced to
`/discover/` by the client router** on a direct `goto`, so `navigate_to_list` opens
`DISCOVER_URL` and *clicks* the banner (`selectors.DISCOVER_BANNER`); `goto` is fallback
only. The grid also sits below a full-height hero and is lazy-mounted.

The probe dumps the **frame's** HTML to `probe_list.html`/`probe_detail.html` and reaches
detail by *clicking* a card and reading the resulting frame (a top-page `goto` would
break the embed). No cards in a dump ⇒ suspect the frame wasn't found / render timing
before suspecting card selectors.

**Confirmed card markup (from the app-frame dump):** cards are `<button class="card-wrapper">`
elements (NOT anchors) with `aria-label="View <NAME> campaign"`. They have **no href and
no id**, so `extract_card` derives identity by slugifying the name (`extract.slugify`)
and `url` is `None` at list time. Fields on the card: name (aria-label / `h3.line-clamp-1`
/ `h2.line-clamp-2`), pay pill `.verified-pill-blue` (e.g. `$1/1K`; featured cards add
`views`), budget as a `$paid/$total` pair → `extract.parse_budget_pair` (first = paid /
progress, second = total; `remaining = 1 - paid/total`). Platforms are bare SVG icons
with no text, so platform info must come from the detail page. Names/cards repeat across
carousels (Featured / All Campaigns / category rows) — dedupe by slug.

**Detail is an in-frame Radix dialog, NOT a navigation.** Clicking a card opens
`div[role="dialog"].campaign-details-modal-bg` overlaid on the list (which stays
mounted behind it). So Phase 2 (`open_detail` → `extract_detail(dialog)` →
`close_detail`) works like this:
- `open_detail` clicks the card via `fl.get_by_role("button", name=f"View {name}
  campaign", exact=True)` — lookup is by **accessible name**, so it never depends on
  list scroll position surviving (the card re-resolves wherever it is).
- `extract_detail` is scoped to the **dialog Locator**; `DETAIL_*` selectors are
  relative to it. Rules come from `span.break-all` bullets (joined), budget is the same
  `$paid/$total` pair, pay is `$1/1K views`. Source links (`drive.google`/`youtube`)
  appear only on campaigns that have them.
- **Stats chart (Views / Submissions) — INTENTIONALLY NOT SCRAPED.** The dialog has a
  Views/Submissions chart, but views-per-submission was DROPPED as a signal (an average over
  a fat-tailed clip distribution — two viral clips out of 2,000 read "healthy" while everyone
  else earned nothing). The value also only lives in a `<number-flow-react>` shadow-DOM
  component (not in `page.content()`), and a naive DOM read grabbed the pay pill's "1K" as a
  false constant 1000. So the whole stats read (StatsCapture network path, DOM toggle read,
  the `extract` stat parsers, the `DETAIL_STATS_*`/`STATS_*` selectors) was REMOVED — nothing
  reads the chart. `proven_clips` distribution analysis measures clip reliability instead.
  There is no stats endpoint to pin; `--probe` no longer dumps `probe_stats_api.json`.
- `capture_campaign_url` records a clickable campaign URL in three tiers: (1) cheap — the
  dialog may have already shallow-routed the top-page URL; (2) **navigation-free + reliable**
  — `_href_from_dialog` reads the campaign link's `href` straight off an anchor in the dialog
  (Whop's expand/share/title controls are usually `<a href>`), absolutized via `_abs_whop_url`
  and validated by `_looks_campaign_url`; (3) fallback — click **"Expand to full page"**, diff
  the top-page/frame URL, then `_return_to_list` (`page.go_back` → `wait_for_feed`; full re-nav
  last resort). Prefer tier 2 — the old click-and-diff path was fragile (it depended on the
  navigate-away + `go_back` surviving) and left `url = None` a lot; the summary now prints
  "URL not captured" plainly when it fails. All non-fatal. URLs persist in `campaigns.json`,
  so delta runs don't re-capture them.
- **Campaign LOCATOR for the clipper handoff (Task A).** `build_record` stores three fields:
  `url` (from `capture_campaign_url`), `campaign_id` (derived from that URL via
  `extract.campaign_id_from_url` — the last path segment, e.g. `app_QRxsQodZgK1r4D`; a dialog
  may also supply `detail["campaign_id"]` directly), and `locator_missing` — True **only** when
  we captured NEITHER a URL nor an id. Card-only stubs (`card_only_record`) set
  `locator_missing=True` by definition (never opened). A miss NEVER crashes — it just flags the
  campaign so the clipper's `pickcampaign.py` can fail loud on an unlocatable pick instead of
  silently proceeding. These persist in `campaigns.json` and feed the Scout→Clipper handoff
  (the clipper reads this file directly; see its CLAUDE.md).
- `close_detail` presses **Escape** (Radix closes on it) and waits for the dialog to
  detach; the list is preserved behind, so no rescroll is needed.
- Cards aren't virtualized (~500 stay mounted), so `get_by_role` finds any card after a
  back-nav without needing to restore scroll position.

## Hard constraints (do not violate)

- **UTF-8 everywhere.** All file I/O passes `encoding="utf-8"`, and `scout.py` reconfigures
  `stdout`/`stderr` to UTF-8 at import (Windows consoles default to cp1252 and
  UnicodeEncodeError'd the moment a run printed an emoji campaign name — a latent crash). When
  adding new file reads/writes or console prints, keep them UTF-8.
- **Always headful.** Never add headless mode.
- No stealth/evasion plugins, no proxy rotation, no concurrency (one page, one tab,
  strictly sequential), no direct private-API calls.
- Never automate the login form or touch credentials; login is always manual.
- **Never navigate programmatically during manual login** — Whop's OAuth redirect chain
  will collide with a `page.goto` and raise "Navigation interrupted by another
  navigation". All navigation goes through `safe_goto` (never raises), and the login
  phase only navigates *after* the user's Enter, with retry-or-wait. Nothing in
  `ensure_logged_in` may exit or crash the run.
- Never retry through a captcha/challenge/login wall. A captcha/challenge or a login wall
  mid-run raises `StopRun` immediately (a HARD block) — save progress and exit; the tool
  degrades to "not today, browse manually" and never escalates. **Routine click slowness is
  NOT a block:** Whop's cross-origin iframe is often slower than one click timeout, so card
  and dialog clicks use a generous `cfg.click_timeout_ms` (28s) and retry `cfg.click_retries`
  times via `_click_with_retry` before counting as one failure. Only after
  `cfg.max_consecutive_failures` (8) recoverable failures in a row — a likely real block or
  page-structure change, not slowness — does the tripwire raise `StopRun`. Distinguish the
  types: `open_detail` raises plain exceptions for transient failures (retried/counted) but
  `StopRun` ONLY for a detected challenge/login wall; the scrape loop also re-checks
  `is_challenge`/`is_login_wall` on any error and runs `_recover_list` (dismiss a half-open
  dialog + confirm the feed is healthy) so one slow card can't cascade.
- Keep the once-daily (20h) guard and the 200-campaign / 90-minute session caps intact.
  These bound the **on-Whop scraping** session (politeness — "indistinguishable from me
  browsing my own account"). The **off-Whop analysis** phase (yt-dlp clipper discovery
  in `proven_clips`, hitting YouTube not Whop) is deliberately uncapped on RUNTIME — it may
  run for hours overnight — but scopes its WORK to the VIABLE survivors (`_clip_viable`),
  deduped by creator and cached across runs, and parallelized on a small thread pool with
  per-creator timeouts (see the `proven_clips.py` note). Do not conflate the two: the
  analysis-phase parallelism/caps are off-Whop and never relax the Whop-session caps, and
  the off-Whop runtime being uncapped never relaxes them either.
- All human-pacing changes go through `pacing.Pacer`; keep intervals randomized (never
  periodic).

## Files never to commit

`whop_profile/` holds the live logged-in session — treat it like a credential. Runtime
outputs (`campaigns.json`, `campaigns_summary.md`, `campaign_template.json`,
`proven_clips_result.json`, `proven_clips_cache.json`, `category_cache.json`, `state.json`,
`errors.log`, `probe_*`) are gitignored. Categorization needs `GROQ_API_KEY` in the env and
the `groq` package (in requirements.txt); without them it degrades to the keyword tagger.
`clip_farms.json` and `my_performance.json` are optional user-maintained input (my own
recorded results — personal ground truth), not scout outputs; scout never writes them.
`completed_campaigns.json` (the DONE list) is clipper/user-maintained state — scout reads it
every run and `--mark-done`/`--unmark-done` edit it, but it's personal state, gitignored.
