# Scout

A personal research tool that scrapes **your own** logged-in Whop Content Rewards
campaigns at human pace, once a day, and writes them to a JSON file and a
readable Markdown summary.

> Companion project: this repo (**whop-scout**) researches and ranks which campaigns are worth
> clipping for; [**whop-clipper**](https://github.com/preciousg10/whop-clipper) turns the footage
> from the ones you take on into finished vertical clips.

It is deliberately *not* a crawler. One visible browser window, one tab, low
volume, erratic human timing, and it **stops the moment anything looks like a
block or challenge** — degrading to "not today, browse manually" rather than
pushing through. No headless mode, no stealth plugins, no proxies, no login
automation, no private-API hits.

---

## Setup

Requires Python 3.9+ (3.11+ recommended).

```powershell
# from the scout/ folder
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Windows PowerShell
pip install -r requirements.txt
python -m playwright install chromium
```

`yt-dlp` comes from `requirements.txt`. It's only used to read channel metadata
for the footage probe (video counts / durations / recent upload dates). It never
downloads video. If it's missing, Scout just skips that step.

---

## First run (manual login)

```powershell
python scout.py --probe
```

A real Chromium window opens on Whop. **Log in yourself** in that window, then
return to the terminal and press Enter. Scout never sees or stores your
credentials — it just reuses the session saved under `./whop_profile/`.

`--probe` then navigates to the Content Rewards list, screenshots one card and
one campaign detail page (`probe_card.png`, `probe_detail.png`), saves their HTML
(`probe_list.html`, `probe_detail.html`), and prints what it extracted with the
current selectors.

**Before trusting any real run, open those screenshots/HTML and confirm the
selectors in `selectors.py`.** Whop's DOM isn't confirmed in this build, so every
selector lives in that one file with fallback candidates — fix them until the
probe's sample output looks right, then re-run `--probe` to check.

---

## Daily run

```powershell
python scout.py
```

- **Once-daily guard:** refuses if the last run was < 20h ago. Override with
  `--force`.
- **Delta by default:** full detail scrape only for *new* campaigns; for
  already-known ones it just refreshes the card-level budget/pay numbers from the
  list view.
- **Session caps:** stops cleanly at 200 campaigns **or** 90 minutes, whichever
  comes first, and resumes next time.

### Flags

| Flag        | Effect                                                        |
|-------------|---------------------------------------------------------------|
| `--force`   | Ignore the 20h once-daily guard.                              |
| `--refresh` | Full re-scrape of **every** campaign, not just new ones.      |
| `--probe`   | Log in + screenshot/dump a card and a detail page; no scrape. |

---

## How a run works

1. **Phase 1 — list pass.** Human-scrolls the Content Rewards list until no new
   cards load, collecting name, pay rate, budget, platforms, and URL per card.
2. **Pre-filter** (tunables at the top of `scout.py`): min pay/1k, budget
   remaining > 15%, platforms must include tiktok/shorts/reels. Failing campaigns
   are still recorded in `campaigns.json` as `skipped_prefilter` **with the
   reason** — nothing is silently dropped. Filters only apply to values actually
   parsed, so rough selectors don't cause invisible over-filtering.
3. **Phase 2 — detail pass.** Visits only survivors and extracts the full record
   (pay + unit, budgets, platforms, source/footage links, verbatim rules,
   participant count, deadline, timestamp).
4. **Pre-score** — a crude sort hint only: `pay_per_1k × budget_left_fraction ÷
   max(participants, 1)`. Not a recommendation.
5. **Footage probe** — for the top 30 by pre-score, reads YouTube/Twitch/Kick
   channel metadata via `yt-dlp` (no downloads).

If 3 campaigns fail in a row, or a captcha / challenge / login wall appears,
Scout **stops immediately**, saves progress, and reports.

---

## Outputs (land in this folder)

| File                    | What it is                                                       |
|-------------------------|------------------------------------------------------------------|
| `campaigns.json`        | Structured array, stable schema, includes skipped + reasons.     |
| `campaigns_summary.md`  | Human-readable, sorted by pre-score; each header links to Whop.  |
| `state.json`            | Bookkeeping: last run + known campaign IDs (for delta/resume).   |
| `errors.log`            | Per-campaign failures (URL + error), appended.                   |
| `whop_profile/`         | Your persistent login session. **Never share or commit this.**   |
| `probe_*.png/.html`     | Selector-confirmation artifacts from `--probe`.                  |

---

## Pacing & bans (by design)

Base per-page delay 1.5–6s (20% of the time a fast 0.3–0.8s click-through), AFK
breaks every 6–15 campaigns for 45–150s (10% stretch to 3–5 min), incremental
wheel scrolling, occasional hovers, and a 5% chance to double back to the
previous campaign. Every interval is re-randomized — nothing is periodic.

Never: headless, stealth/evasion libraries, proxy rotation, concurrency, direct
private-API calls, retrying through a captcha, automating login, or more than one
run per day by default. If Whop blocks or challenges, Scout stops — it never
escalates.

All of the above is tunable in the `Config` block at the top of `scout.py`.
