"""Centralized Whop selectors — UNCONFIRMED until you run `python scout.py --probe`.

Scout deliberately does NOT guess selectors blindly inside the scraping logic.
Every DOM hook the tool uses lives here, each as a list of candidate CSS/text
selectors tried in order (first match wins). Whatever text those hooks return is
handed to the DOM-agnostic parsers in extract.py, so the parsing stays correct
even while these selectors are rough.

Workflow: run `python scout.py --probe`. It logs you in, screenshots one card
and one detail page (probe_card.png / probe_detail.png) and saves the live HTML
(probe_list.html / probe_detail.html). Open those, fix the selectors below, and
re-probe until the values it prints look right. THEN do a real run.
"""

# --- Content Rewards list page -------------------------------------------------
# The Content Rewards discover app. The campaign grid sits BELOW a full-height
# featured hero banner and is lazy-mounted, so the crawler scrolls down past the
# hero before looking for cards. (Note: /discover/content-rewards/ is dead — it
# just redirects to /discover/ — so don't use it.)
LIST_URL = "https://whop.com/discover/app/app_QRxsQodZgK1r4D/"

# Whop's client router bounces a *direct* goto to LIST_URL back to /discover/, so
# we navigate like a user: open the discover page, then click the orange
# "Clipping / Content Rewards" banner card. LIST_URL is only a fallback.
DISCOVER_URL = "https://whop.com/discover/"
DISCOVER_BANNER = [
    'a[href*="app_QRxsQodZgK1r4D"]',   # the banner links to the app — most reliable
    'a[href*="/discover/app/"]:has-text("Content Rewards")',
    'a:has-text("Content Rewards")',
    'a:has-text("Clipping")',
    '[role="link"]:has-text("Content Rewards")',
]

# The Content Rewards experience is a Whop "app" embedded in a CROSS-ORIGIN iframe
# (apps.whop.com). All cards + detail content live INSIDE that frame — the top page
# only holds the iframe shell, so page.content() never captures the cards. The
# scraper locates this frame and runs every card/detail selector against it.
APP_FRAME_URL_HINT = "apps.whop.com"
APP_IFRAME = [
    'iframe[title="Content Rewards"]',
    'iframe[src*="apps.whop.com"]',
    'iframe[src*="/core/app/launch/"]',
]

# A single campaign card. CONFIRMED from the app-frame dump (apps.whop.com).
# Cards are <button> elements (NOT anchors) — they carry no href/id, so identity is
# derived by slugifying the name. The button's aria-label is "View <NAME> campaign".
CARD = [
    'button.card-wrapper',
    'button[aria-label^="View "][aria-label$=" campaign"]',
    'button[class*="card-wrapper"]',
]
# Visible title fallbacks (h3 = grid card, h2 = featured card). The primary name
# source is the button's aria-label, handled in extract.extract_card.
CARD_NAME = ['h3.line-clamp-1', 'h2.line-clamp-2', 'h3', 'h2']
# Pay-rate pill, e.g. "$1/1K" (grid) or "$8/1K views" (featured).
CARD_PAY = [
    '.verified-pill-blue',
    'text=/\\$[0-9.,]+\\s*\\/\\s*1?[kKmM]/',
]
# Budget shown as "paid / total", e.g. "$136,668/$250,000" (parsed as a pair).
CARD_BUDGET = ['text=/\\$[0-9,]+\\s*\\/\\s*\\$[0-9,]+/']
# Platforms are rendered as bare SVG icons with no text/aria on the card, so there is
# nothing reliable to read here — platform info comes from the detail page instead.
CARD_PLATFORMS = []
CARD_LINK = []  # cards are buttons, no href

# --- Campaign detail dialog ----------------------------------------------------
# CONFIRMED from probe_detail.html. Clicking a card does NOT navigate — it opens an
# in-frame Radix DIALOG (drawer/modal) overlaid on the list. Detail extraction is
# scoped to this dialog; close it with the Escape key (the list is preserved behind).
DETAIL_DIALOG = ['[role="dialog"]', '.campaign-details-modal-bg']

# The selectors below are RELATIVE TO THE DIALOG scope.
DETAIL_NAME = ['h2']                       # visible title (sr-only h2 holds it too)
DETAIL_CREATOR = ['span.truncate']         # "Creator Casino" in the header row
DETAIL_APPROVAL_RATE = ['text=/[0-9]{1,3}\\s*%\\s*approval rate/i']  # "88% approval rate" (header)
DETAIL_PAY = ['text=/\\$[0-9.,]+\\s*\\/\\s*1?[kKmM]/']          # "$1/1K views"
DETAIL_BUDGET_TOTAL = ['text=/\\$[0-9,]+\\s*\\/\\s*\\$[0-9,]+/']  # "$136,713/$250,000"
DETAIL_BUDGET_REMAINING = ['text=/\\$[0-9,]+\\s*\\/\\s*\\$[0-9,]+/']
DETAIL_PLATFORMS = []                       # shown as bare SVG icons, no text
DETAIL_RULES = ['span.break-all']          # requirement bullets; joined in extract
# "See more" / "Read more" / "Show more" toggles that collapse long rules text. Clicked
# best-effort before reading so truncated requirements fully render (scout._expand_dialog_text).
# Unconfirmed — degrades to a no-op when absent.
DETAIL_RULES_EXPAND = [
    'button:has-text("See more")', 'button:has-text("Read more")',
    'button:has-text("Show more")', 'text=/^\\s*see more\\s*$/i',
    'text=/^\\s*read more\\s*$/i', 'text=/^\\s*show more\\s*$/i',
]
DETAIL_PARTICIPANTS = ['text=/[0-9][0-9,]*\\s*(?:clippers|participants|creators)/i']
DETAIL_DEADLINE = ['text=/deadline|ends|closes/i']
# Primary join CTA — an "Apply" button means the campaign is application-gated (selection
# required), a "Join" button means an instant open join. Read best-effort from button/link
# elements in the dialog (extract._detect_join_cta); unconfirmed, so it degrades to None.
DETAIL_JOIN_CTA = ['button', 'a[role="button"]', '[role="button"]', 'a[href]']

# --- stats chart: Total views / Submissions — INTENTIONALLY NOT SCRAPED ---------
# The detail dialog carries a Views/Submissions chart, but views-per-submission was
# DROPPED as a signal (an average over a fat-tailed clip distribution — two viral clips
# out of 2,000 read "healthy" while everyone else earned nothing). The number also only
# lives in a <number-flow-react> shadow-DOM component (not the HTML), and a naive DOM read
# grabbed the pay pill's "1K" as a false constant 1000. So we no longer read it at all;
# proven-clipper distribution analysis (proven_clips.py) measures clip reliability instead.
# No stats selectors / network-capture hints remain — nothing here to pin.

# Source / footage links live inside the detail page body.
SOURCE_LINK_SELECTORS = [
    'a[href*="drive.google.com"]',
    'a[href*="youtube.com"]',
    'a[href*="youtu.be"]',
    'a[href*="twitch.tv"]',
    'a[href*="kick.com"]',
]

# --- Blocker / challenge detection --------------------------------------------
# Real captcha / bot-challenge surfaces only. Login-wall detection is separate
# (URL + password field) so ordinary "Log in" links don't trip a false stop.
CHALLENGE_MARKERS = [
    'iframe[src*="captcha" i]',
    'iframe[src*="turnstile" i]',
    'iframe[title*="challenge" i]',
    '#challenge-form',
    'text=/verify you are (a )?human/i',
    'text=/checking your browser/i',
]
