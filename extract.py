"""DOM-agnostic parsers + Playwright extraction.

The parsers (parse_pay, parse_money, ...) work on plain text, so they stay
correct regardless of how rough the selectors currently are. The extraction
helpers pull that text out of the page via the candidate selectors in
selectors.py and never raise on a missing field — they return None.
"""
import re
from urllib.parse import urljoin, urlparse

import selectors as S

BASE = "https://whop.com"

# Platform words we recognize in free text (used for the prefilter).
PLATFORM_WORDS = [
    "tiktok", "shorts", "reels", "youtube", "instagram",
    "twitch", "kick", "twitter", "facebook", "snapchat",
]

# Hosts we're willing to run a footage probe against.
CHANNEL_HOSTS = ("youtube.com", "youtu.be", "twitch.tv", "kick.com")

# Social platforms we recognize by URL host, for the source-popularity lookup.
# Order matters only for reporting; classification is host-substring based.
SOCIAL_HOSTS = {
    "tiktok": ("tiktok.com",),
    "youtube": ("youtube.com", "youtu.be"),
    "instagram": ("instagram.com",),
    "kick": ("kick.com",),
    "x": ("twitter.com", "x.com"),
}


# --- pure parsers --------------------------------------------------------------
def parse_pay(text):
    """'$1.50 / 1k', '$2 per 1M', '$0.80/1K' -> normalized pay_per_1k."""
    out = {"pay_value": None, "pay_unit": None, "pay_per_1k": None}
    if not text:
        return out
    t = text.replace(",", "")
    m = re.search(
        r"\$?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:/|per)?\s*(1?\s*[kKmM]|1000000|1000)?", t
    )
    if not m:
        return out
    value = float(m.group(1))
    unit_raw = (m.group(2) or "").lower().replace(" ", "")
    if unit_raw in ("1m", "m", "1000000"):
        out.update(pay_value=value, pay_unit="per_1M", pay_per_1k=round(value / 1000.0, 4))
    else:  # default assumption is per-1k, the common Content Rewards unit
        out.update(pay_value=value, pay_unit="per_1k", pay_per_1k=round(value, 4))
    return out


def parse_money(text):
    if not text:
        return None
    m = re.search(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)", text)
    return float(m.group(1).replace(",", "")) if m else None


def parse_remaining_fraction(text, total=None):
    """Return the fraction of budget still available, 0..1, or None.

    Handles '38% remaining', '62% paid out', and '$1,234 remaining' (when total
    is known).
    """
    if not text:
        return None
    pct = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%", text)
    if pct:
        val = float(pct.group(1)) / 100.0
        if re.search(r"paid|used|spent|out", text, re.I):
            return max(0.0, 1.0 - val)
        return max(0.0, min(1.0, val))
    money = parse_money(text)
    if money is not None and total:
        return max(0.0, min(1.0, money / total))
    return None


def parse_platforms(text):
    if not text:
        return []
    t = text.lower()
    return [p for p in PLATFORM_WORDS if p in t]


def parse_int(text):
    if not text:
        return None
    m = re.search(r"([0-9][0-9,]*)", text)
    return int(m.group(1).replace(",", "")) if m else None


def campaign_id_from_url(url):
    if not url:
        return None
    path = urlparse(url).path.rstrip("/")
    seg = path.split("/")[-1] if path else ""
    return seg or url


def absolute(href):
    if not href:
        return None
    return urljoin(BASE, href)


def slugify(text):
    """Stable id from a campaign name (cards have no href/id in the list DOM)."""
    if not text:
        return None
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or None


def parse_budget_pair(text):
    """'$136,668/$250,000' -> (paid, total, remaining_fraction).

    On the card the first (bold) number is the amount paid out so far and tracks the
    progress bar; the second (faded) number is the total budget cap.
    """
    if not text:
        return (None, None, None)
    nums = [float(a.replace(",", "")) for a in re.findall(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)", text)]
    if len(nums) >= 2:
        paid, total = nums[0], nums[1]
        rem = max(0.0, min(1.0, 1.0 - paid / total)) if total > 0 else None
        return (paid, total, rem)
    if len(nums) == 1:
        return (None, nums[0], None)
    return (None, None, None)


# --- minimum-payout viability --------------------------------------------------
# A campaign often gates the FIRST payout behind a minimum ("$10 minimum payout",
# "you must reach $5 to withdraw"). Combined with pay_per_1k that tells us how many
# views a clip needs before it earns a single cent. A high minimum means a normal
# ~1k-view clip earns nothing, which is a trap worth flagging loudly.
_MIN_PAYOUT_PATTERNS = (
    # keyword ... $amount    e.g. "minimum payout of $10", "payout threshold: $5"
    r"(?:min(?:imum)?(?:\s+payout)?|payout\s+(?:minimum|threshold)|threshold|"
    r"cash\s*out|withdraw(?:al)?)[^$\n]{0,40}\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
    # $amount ... keyword    e.g. "$10 minimum", "$5 to withdraw", "$25 payout minimum"
    r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)[^$\n]{0,30}?"
    r"(?:min(?:imum)?|to\s+(?:cash\s*out|withdraw|be\s+paid|get\s+paid|payout)|payout\s+min)",
)


def parse_min_payout(text):
    """Smallest explicit minimum-payout dollar figure in free text, or None.

    Fires ONLY on explicit minimum/threshold/withdraw language so it never mistakes
    a pay rate ("$1/1K") or budget ("$250,000") for a minimum. If nothing matches we
    return None (unknown) rather than guessing.
    """
    if not text:
        return None
    found = []
    for pat in _MIN_PAYOUT_PATTERNS:
        for m in re.finditer(pat, text, re.I):
            try:
                found.append(float(m.group(1).replace(",", "")))
            except (TypeError, ValueError):
                continue
    return min(found) if found else None


def min_views_to_payout(min_payout, pay_per_1k):
    """Views a single clip needs before it earns anything: min_payout / rate * 1000.

    None if either input is unknown/zero — we never invent a number.
    """
    if not min_payout or not pay_per_1k:
        return None
    return (min_payout / pay_per_1k) * 1000.0


# --- activity count (submissions / participants) -------------------------------
# The live Views/Submissions CHART is a shadow-DOM <number-flow-react> component and is
# NOT scraped (see selectors.py). But Whop also renders a single standalone activity count
# inline with the budget on the card/modal — the integer immediately after the "$paid/$total"
# budget pair. This is the only submissions/participants count available in the modal TEXT,
# and it's what the payout-health signal needs (meaningful activity + $0 paid = a paying-dead
# trap). Newline-anchored so it never grabs a pay rate / dollar figure elsewhere in the text.
_ACTIVITY_AFTER_BUDGET_RE = re.compile(
    r"\$\s*[0-9][0-9,]*(?:\.[0-9]+)?\s*/\s*\$\s*[0-9][0-9,]*(?:\.[0-9]+)?"
    r"\s*[\r\n]+\s*([0-9][0-9,]*)\b")
_ACTIVITY_MAX = 10_000_000     # sanity ceiling; a larger match is almost certainly not a count


def parse_activity_count(modal_text):
    """The activity count Whop shows inline with the budget — the standalone integer right
    after the '$paid/$total' budget pair in the modal text (submissions / participants). Returns
    an int, or None when the pattern isn't present (never guessed). This is a PROXY for
    submissions: the real Views/Submissions chart is shadow-DOM and unscraped, so this single
    inline count is the payout-health activity signal. Pure/testable."""
    if not modal_text:
        return None
    m = _ACTIVITY_AFTER_BUDGET_RE.search(modal_text)
    if not m:
        return None
    try:
        n = int(m.group(1).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return n if 0 <= n <= _ACTIVITY_MAX else None


# --- approval rate -------------------------------------------------------------
# Every Whop campaign header shows an approval rate — "88% approval rate" — the % of
# submissions that get approved/paid. A KNOWN-and-LOW rate means most clips are rejected
# unpaid (wasted effort), so scoring deranks below a floor. It sits right after the
# creator/name in the modal header ("<Name>\n<Creator>\n…\nNN% approval rate\n<Name>…"),
# so the FIRST "NN% approval rate" in the modal text is the campaign's OWN rate. Pure/testable.
_APPROVAL_RATE_RE = re.compile(r"([0-9]{1,3})\s*%\s*approval\s+rate", re.I)


def parse_approval_rate(text):
    """The campaign's own approval rate as an int 0..100, or None (UNKNOWN) when not present.
    Reads the FIRST 'NN% approval rate' in the text (the header rate — the campaign's own).
    Fails to None on anything out of 0..100 so a garbage match never penalizes. Never guessed."""
    if not text:
        return None
    m = _APPROVAL_RATE_RE.search(text)
    if not m:
        return None
    try:
        n = int(m.group(1))
    except (TypeError, ValueError):
        return None
    return n if 0 <= n <= 100 else None


# --- minimum-VIEW payout threshold ---------------------------------------------
# DISTINCT from the minimum-payout-DOLLAR above: some campaigns pay NOTHING until a single
# video crosses a hard VIEW count — "VIDEO MUST REACH 10K FOR PAYOUT" (TraxNYC), "minimum
# 10,000 views to be paid", "10K views required for payout". For a new / zero-audience
# account this is brutal: a typical early clip never reaches the gate and earns $0, so scoring
# penalizes it hard. We fire ONLY when explicit gating language ("must reach", "minimum",
# "... for payout", "... to be paid", "... required") sits next to a view count, so a pay rate
# ("$1 / 1K views") or a dollar minimum is NEVER misread as a view gate.
_MIN_VIEW_FLOOR = 100          # ignore sub-100 stray numbers ("reach 5 views")
# A number preceded by "per", "/", or "$" is a RATE or dollar figure, not a view gate.
_RATE_PRECEDER_RE = re.compile(r"(?:per|/|\$)\s*$", re.I)
_VIEWS = r"(?:views?|view\s+count)"
_MV_BEFORE = (r"(?:must\s+(?:reach|hit|get|have|receive)|minimum(?:\s+of)?|min\.?|"
              r"at\s+least|reach(?:es)?|need(?:s|ed)?|require[sd]?|threshold(?:\s+of)?)")
_MV_AFTER = (r"(?:for\s+payout|to\s+(?:be\s+paid|get\s+paid|qualify|cash\s*out|withdraw|"
             r"payout|earn|count|be\s+eligible)|before\s+payout|required|minimum)")
_MV_PAYOUT_CTX = (r"(?:for\s+payout|to\s+(?:be|get)\s+paid|before\s+payout|"
                  r"to\s+(?:qualify|payout|earn|count|be\s+eligible)|payout)")
_MIN_VIEW_PATTERNS = (
    # A: gating word, then "<num> views"  — "must reach 10K views", "minimum 10,000 views"
    _MV_BEFORE + r"\s+(?:of\s+)?([0-9][0-9.,]*)\s*([kKmMbB]?)\s*" + _VIEWS,
    # B: "<num> views", then gating word  — "10K views for payout", "10k views required"
    r"([0-9][0-9.,]*)\s*([kKmMbB]?)\s*" + _VIEWS + r"[^.\n]{0,25}?" + _MV_AFTER,
    # C: gating word, then "<num>K/M" with views IMPLIED (no "views" word, not $/followers),
    #    then payout context — catches "MUST REACH 10K FOR PAYOUT" (unit is mandatory here).
    _MV_BEFORE + r"\s+([0-9][0-9.,]*)\s*([kKmMbB])\b(?!\s*(?:follow|sub|dollar|usd))"
    r"[^.\n]{0,20}?" + _MV_PAYOUT_CTX,
)


def _views_to_int(numstr, unit):
    try:
        n = float(str(numstr).replace(",", ""))
    except (TypeError, ValueError):
        return None
    mult = {"k": 1e3, "m": 1e6, "b": 1e9}.get((unit or "").lower(), 1)
    return int(round(n * mult))


def parse_min_view_threshold(text):
    """Largest explicit MINIMUM-VIEW payout gate in free text — the view count a single video
    must reach before ANY payout — or None. Fires only on explicit gating language next to a
    view count and skips pay-rate phrasing ("per 1K views", "/1K views") and dollar figures,
    so a rate or a min-payout-dollar is never read as a view gate. None when nothing matches —
    never guessed. Returns the MAX matched gate (the harshest a clip must clear)."""
    if not text:
        return None
    found = []
    for pat in _MIN_VIEW_PATTERNS:
        for m in re.finditer(pat, text, re.I):
            num_start = m.start(1)
            preceding = text[max(0, num_start - 12):num_start]
            if _RATE_PRECEDER_RE.search(preceding):
                continue  # "$1 per 1K views" / "/1K views" — a rate, not a gate
            val = _views_to_int(m.group(1), m.group(2))
            if val is not None and val >= _MIN_VIEW_FLOOR:
                found.append(val)
    return max(found) if found else None


# --- resource-doc reference (capture cross-check) ------------------------------
# Does the modal TEXT indicate a linked Doc / Drive / Notion / resource FOLDER exists? Used
# ONLY to cross-check capture: when this is true but resource_links came back empty, a doc was
# silently missed (surfaced as capture_suspect, never a change to capture itself). Detection is
# deliberately SPECIFIC — host names, service names, "... folder", "the doc", "swipe file",
# "linked below" — so ordinary words ("brief overview", "requirements:", "document your clips")
# don't false-fire. Mirrors scout._RESOURCE_HOST_RE's host set.
_RESOURCE_REF_PATTERNS = (
    # explicit resource-host names in the text (a bare / uncaught URL or a spelled-out host)
    r"docs\.google|drive\.google|sheets\.google|slides\.google|notion\.so|notion\.site|"
    r"dropbox\.com|onedrive|1drv\.ms|mega\.nz",
    # service names spelled out
    r"google\s+(?:doc|docs|drive|sheet|sheets|slide|slides)\b|\bnotion\b|\bdropbox\b",
    # a storage / asset FOLDER
    r"\b(?:drive|content|footage|clip|clips|asset|assets|media|raw|b-?roll)\s+folder\b|"
    r"\bfolder\s+(?:link|below|here)\b|\bfolder\s+of\s+(?:clips|footage|videos)\b",
    # a referenced DOC / sheet with a determiner or qualifier (not the bare word)
    r"\b(?:the|our|this|full|creative|attached|linked|pinned)\s+(?:doc|document|sheet|brief)\b|"
    r"\b(?:rules?|guidelines?|requirements?|content|resource|brief|style)\s+"
    r"(?:doc|document|sheet)\b|\bswipe\s+file\b",
    # explicit "linked / attached / pinned ..." pointing at a resource
    r"\b(?:linked|attached|pinned)\s+(?:below|here|above)\b|"
    r"\b(?:found|available|linked|attached)\s+in\s+the\s+(?:doc|drive|folder|notion)\b",
)
_RESOURCE_REF_RE = re.compile("(" + "|".join(_RESOURCE_REF_PATTERNS) + ")", re.I)


def references_resource_doc(text):
    """The matched phrase if `text` indicates a linked Doc/Drive/Notion/resource folder
    EXISTS, else None. Deliberately specific so ordinary words don't false-fire — used to
    detect that resource_links capture may have silently missed a doc."""
    if not text:
        return None
    m = _RESOURCE_REF_RE.search(text)
    if not m:
        return None
    return " ".join(m.group(1).split())[:60]


# --- max payout per video ------------------------------------------------------
_MAX_UNCAPPED_RE = re.compile(
    r"\b(no\s+max(?:imum)?|no\s+cap|uncapped|unlimited\s+(?:earnings|payout|payouts)|"
    r"no\s+(?:earning\s+)?limit|no\s+payout\s+cap)\b", re.I)
_MAX_PAYOUT_PATTERNS = (
    r"(?:max(?:imum)?(?:\s+payout)?|cap(?:ped)?(?:\s+at)?|up\s+to)\s*[^$\n]{0,25}\$\s*"
    r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:per\s+(?:video|clip|post|submission))?",
    r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:max(?:imum)?|cap)\b",
    r"up\s+to\s+\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s+per\s+(?:video|clip|post|submission)",
)


def parse_max_payout(text):
    """Max payout PER VIDEO from the brief. Returns {max_payout_per_video, uncapped}.

    A cap limits the viral upside the model depends on. Explicit "no max / uncapped"
    language sets uncapped=True (best). Nothing found -> (None, False): honestly
    unknown, never guessed. Fires only on explicit max/cap/"up to" language so a pay
    rate or budget is never mistaken for a cap.
    """
    if not text:
        return {"max_payout_per_video": None, "uncapped": False}
    if _MAX_UNCAPPED_RE.search(text):
        return {"max_payout_per_video": None, "uncapped": True}
    for pat in _MAX_PAYOUT_PATTERNS:
        m = re.search(pat, text, re.I)
        if m:
            try:
                return {"max_payout_per_video": float(m.group(1).replace(",", "")),
                        "uncapped": False}
            except (TypeError, ValueError):
                continue
    return {"max_payout_per_video": None, "uncapped": False}


# --- competition + velocity numeric helpers ------------------------------------
def participants_per_1k_budget(participants, budget_total):
    """Clippers competing per $1k of budget. 3,000 on $30k (=100) is a very different
    fight from 20 on $30k (=0.67). None when either input is unknown."""
    if participants is None or not budget_total or budget_total <= 0:
        return None
    return round(participants / (budget_total / 1000.0), 2)


def payout_velocity(budget_paid, budget_total, days_active):
    """Fraction of budget paid out PER DAY. An old campaign creeping along means clips
    aren't earning. None unless all inputs are known and positive — a fresh campaign
    (days_active handled by the caller) must not look like a stalled one."""
    if budget_paid is None or not budget_total or budget_total <= 0:
        return None
    if days_active is None or days_active <= 0:
        return None
    return round((budget_paid / budget_total) / days_active, 6)


# --- hard disqualifiers ---------------------------------------------------------
# Detected from the brief/rules and flagged LOUDLY with the specific reason. These
# sink a campaign to the bottom; they are never hidden. Patterns are deliberately
# specific (requirement language, not passing mentions) to avoid false positives.
_DQ_GAMBLING = re.compile(
    r"\b(casino|gambling|gamble|sportsbook|slots?|poker|roulette|blackjack|sweepstakes?|"
    r"1xbet|bookmaker|place\s+a\s+bet|betting|bet365|stake\.com)\b", re.I)
_DQ_FACE = re.compile(
    r"\b(face\s+on\s+camera|on[-\s]camera|show\s+your\s+face|film\s+yourself|"
    r"record\s+yourself|talking[-\s]head|ugc|user[-\s]generated|be\s+on\s+camera|"
    r"your\s+own\s+voice|voiceover\s+required|must\s+appear\s+on)\b", re.I)
_DQ_FOLLOWERS = re.compile(
    r"((?:minimum|at\s+least|must\s+have|require[sd]?|min\.?)\s*(?:of\s+)?"
    r"[0-9][0-9,]*\s*[kmb]?\+?\s*followers"
    r"|[0-9][0-9,]*\s*[kmb]?\+?\s*followers?\s+(?:minimum|required|or\s+more|min\b))", re.I)
_DQ_GEO = re.compile(
    r"([0-9]{2,3}\s*%[^.\n]{0,30}(?:audience|viewers|followers))"
    r"|audience\s+(?:must\s+be|located|based|primarily|mostly)[^.\n]{0,30}"
    r"|(?:majority|primarily|mostly)\s+(?:from\s+)?[A-Z][a-z]+\s+audience"
    r"|(?:us|usa|uk|india|indian|tier[-\s]?1|english[-\s]speaking)\s+(?:based\s+)?audience", re.I)
_DQ_FORMAT = re.compile(
    r"\b(slideshow|image\s+post|photo\s+post|carousel\s+only|static\s+image|"
    r"picture\s+only|music\s+only|audio\s+only|no\s+video\s+content)\b", re.I)
_DQ_ADSPEND = re.compile(
    r"\b(ad\s+spend|run\s+(?:paid\s+)?ads|paid\s+ads|spark\s+ads|boost(?:ed)?\s+(?:the\s+)?post|"
    r"advertising\s+budget|paid\s+promotion\s+required|paid\s+media)\b", re.I)
_DQ_LANG = re.compile(
    r"\b(?:must\s+be\s+in|content\s+in|only\s+in|written\s+in|spoken\s+in)\s+"
    r"(spanish|french|german|portuguese|hindi|arabic|russian|japanese|korean|chinese|"
    r"mandarin|italian|turkish|indonesian|vietnamese|thai)\b"
    r"|\b(spanish|french|german|portuguese|hindi|arabic|russian|japanese|korean|chinese|"
    r"italian|turkish)[-\s]only\b|\bnon[-\s]english\b", re.I)
_DQ_FOOTAGE_ACCESS = re.compile(
    r"\b(request\s+access|dm\s+(?:me|us|for)|join\s+(?:our\s+)?discord|members?\s+only|"
    r"login\s+required|ask\s+for\s+(?:the\s+)?footage|footage\s+(?:available\s+)?"
    r"(?:in|on)\s+(?:discord|the\s+whop|our\s+server))\b", re.I)
# APPLICATION / SELECTION gate — the campaign isn't an instant open join; you must apply,
# be accepted/approved, get invited, or wait for a spot before you can clip. Unusable for an
# automated pipeline that starts clipping immediately. Deliberately requires gating context
# around "apply/accepted/selection" so passing words ("terms apply", "accepted formats",
# "selection of clips") don't false-fire.
_DQ_APPLICATION = re.compile(
    r"\bappl(?:y|ication)\s+(?:to\s+)?(?:join|participate|clip|the\s+(?:program|campaign)|"
        r"required|process|form|here|now|below|via|through)"
    r"|\b(?:to\s+)?appl(?:y|ication)\s+(?:is\s+)?(?:required|needed)"
    r"|\bmust\s+(?:first\s+)?(?:apply|be\s+(?:accepted|approved|selected|invited))"
    r"|\b(?:we['’]?ll|we\s+will|we)\s+(?:review|approve|accept)\s+(?:your\s+)?"
        r"(?:application|applicants?)"
    r"|\bapproved\s+members?\s+only"
    r"|\baccepted\s+(?:creators?|clippers?|members?|applicants?|participants?)"
    r"|\bselection\s+process\b|\bselected\s+(?:creators?|clippers?|applicants?|participants?)"
    r"|\binvite[-\s]?only\b|\bby\s+invitation\b|\binvitation[-\s]?only\b"
    r"|\bwait\s?list(?:ed)?\b"
    r"|\blimited\s+spots?\b|\bspots?\s+are\s+limited\b"
        r"|\blimited\s+number\s+of\s+(?:creators?|clippers?|spots?)"
    r"|\bonce\s+(?:you(?:['’]?re|\s+are)\s+)?(?:accepted|approved)"
    r"|\bpending\s+approval\b|\bsubject\s+to\s+approval\b"
        r"|\bapprov(?:al|ed)\s+(?:required|needed|process)"
    r"|\bacceptance\s+(?:required|into|is)", re.I)
# Clearly OPEN — instant free join, anyone can participate. The type the pipeline wants.
_OPEN_JOIN = re.compile(
    r"\binstant(?:ly)?\s+join\b|\bjoin\s+(?:instantly|now|for\s+free|the\s+campaign\s+now)\b"
    r"|\bopen\s+to\s+(?:all|everyone|anyone|the\s+public)\b"
    r"|\banyone\s+can\s+(?:join|participate|clip|enter)\b"
    r"|\bfree\s+to\s+join\b|\bno\s+application(?:\s+(?:required|needed))?\b"
    r"|\bstart\s+clipping\s+(?:now|immediately|today|right\s+away)\b", re.I)


def _snippet(m, width=70):
    s = re.sub(r"\s+", " ", m.group(0)).strip()
    return (s[:width] + "…") if len(s) > width else s


def detect_disqualifiers(text, platforms=None, source_links=None, join_cta=None):
    """Return a list of hard-disqualifier dicts {code, reason} found in the brief.

    Each is a reason to sink (never hide) the campaign. Empty list = clean. Conservative
    by design — only explicit requirement/keyword language fires. Gambling/casino is a
    reputational hard-DQ regardless of everything else. `join_cta` (from the page: 'apply'
    or 'join', best-effort) lets an Apply button flag application-gating even when the brief
    text doesn't spell it out.
    """
    t = text or ""
    out = []

    def add(code, label, m=None, note=None):
        detail = _snippet(m) if m is not None else note
        reason = label + (f": “{detail}”" if detail else "")
        out.append({"code": code, "reason": reason})

    if (m := _DQ_GAMBLING.search(t)):
        add("gambling", "gambling/casino/betting-adjacent — reputationally risky", m)
    if (m := _DQ_FACE.search(t)):
        add("face_or_voice", "requires face/voice on camera (UGC, not clipping)", m)
    if (m := _DQ_FOLLOWERS.search(t)):
        add("min_followers", "requires a minimum follower count", m)
    if (m := _DQ_GEO.search(t)):
        add("geo_demographic", "requires specific audience geography/demographics", m)
    if (m := _DQ_FORMAT.search(t)):
        add("non_clippable_format", "non-clippable format (slideshow/image/music-only)", m)
    if (m := _DQ_ADSPEND.search(t)):
        add("paid_ad_spend", "requires paid ad spend", m)
    if (m := _DQ_LANG.search(t)):
        add("non_english", "requires a language I can't produce (non-English)", m)
    if not (source_links or []) and (m := _DQ_FOOTAGE_ACCESS.search(t)):
        add("footage_gated", "footage not auto-downloadable (gated / no source links)", m)
    # application / selection gate — brief text first (with the matched snippet), else the
    # page CTA (an "Apply" button instead of "Join").
    if (m := _DQ_APPLICATION.search(t)):
        add("application_gated",
            "application/selection required — not open to instant join", m)
    elif join_cta == "apply":
        add("application_gated",
            "application/selection required — not open to instant join",
            note="Apply button on the page (not an instant Join)")
    return out


# --- prohibited (vice) category exclusion --------------------------------------
# Betting/gambling/casino/sportsbook, alcohol/drinking, vape/nicotine and similar vice
# categories are AUTO-EXCLUDED from the ranked output (mirrors the rules_unreadable
# exclusion): kept in campaigns.json flagged excluded_prohibited, segregated in the report,
# and skipped by the clipper's pickcampaign — NOT merely sunk to composite 0 like the
# gambling disqualifier. Detected across the campaign NAME, CATEGORY and on-modal
# requirements TEXT. These three lists are the config knob — EXTEND them to broaden coverage.
#
# STRONG terms: unambiguous — a single hit is a clear disqualification.
PROHIBITED_TERMS_STRONG = [
    "casino", "gambling", "sportsbook", "sports betting", "roulette", "blackjack",
    "baccarat", "slot machine", "online slots", "betting site", "betting app",
    "place a bet", "place bets", "parlay", "parlays", "wager", "wagering",
    "bookmaker", "sweepstakes", "crash game", "plinko",
    "alcohol", "alcoholic", "liquor", "whiskey", "whisky", "vodka", "tequila",
    "bourbon", "brewery", "distillery", "hard seltzer",
    "vape", "vaping", "e-cigarette", "e-cig", "nicotine",
]
# WEAK terms: real in a vice context but also occur innocently (bet/stake/odds/drink…).
# A hit here ALONE is treated as BORDERLINE — still excluded, but flagged for review so a
# false positive is surfaced, never silently kept.
PROHIBITED_TERMS_WEAK = [
    "bet", "bets", "betting", "gamble", "odds", "stake", "stakes", "poker",
    "drinking", "drink", "beer", "wine", "seltzer",
]
# Known vice BRANDS — a match anywhere is a clear disqualification regardless of copy.
PROHIBITED_BRANDS = [
    "roobet", "stake.com", "stake.us", "creator casino", "fliff", "bet365",
    "1xbet", "draftkings", "fanduel", "betmgm", "caesars sportsbook", "bovada",
    "rollbit", "duelbits", "gamdom", "csgoroll", "prizepicks", "underdog fantasy",
    "chumba casino", "pulsz", "high 5 casino", "shuffle.com", "bc.game", "betway",
]


def _compile_prohibited(terms):
    # Word-ish boundaries via lookarounds: embedded dots (stake.com) still match cleanly,
    # while substrings never false-fire ('bet' must not hit 'abet'/'sherbet'/'bet365').
    return [(t, re.compile(r"(?<!\w)" + re.escape(t) + r"(?!\w)", re.I)) for t in terms]


_PROHIBITED_BRAND_RX = _compile_prohibited(PROHIBITED_BRANDS)
_PROHIBITED_STRONG_RX = _compile_prohibited(PROHIBITED_TERMS_STRONG)
_PROHIBITED_WEAK_RX = _compile_prohibited(PROHIBITED_TERMS_WEAK)


def detect_prohibited_category(name=None, category=None, text=None):
    """Detect a prohibited/vice campaign (gambling/betting/casino/sportsbook, alcohol/
    drinking, vape/nicotine, or a known vice brand) from the campaign NAME, CATEGORY and
    on-modal requirements TEXT. Returns None when clean, else a dict:
        {reason, matched: [term, ...], borderline: bool, sources: [field, ...]}.

    Conservative by design: an unambiguous keyword (casino/sportsbook/alcohol/vape…) or a
    known brand (Roobet/Fliff/Creator Casino…) is a CLEAR disqualification. An ambiguous
    keyword alone (bet/stake/odds/drink — also innocent) is flagged BORDERLINE: still
    excluded, but surfaced for review so a false positive isn't hidden, never silently kept.
    Pure/testable — no network. Keyword/brand lists are the PROHIBITED_* module constants."""
    fields = {"name": name or "", "category": category or "", "requirements": text or ""}

    def scan(patterns):
        hits, srcs = [], set()
        for term, rx in patterns:
            for field, val in fields.items():
                if val and rx.search(val):
                    hits.append(term)
                    srcs.add(field)
                    break
        return hits, srcs

    brand_hits, brand_src = scan(_PROHIBITED_BRAND_RX)
    strong_hits, strong_src = scan(_PROHIBITED_STRONG_RX)
    weak_hits, weak_src = scan(_PROHIBITED_WEAK_RX)
    if not (brand_hits or strong_hits or weak_hits):
        return None

    borderline = not (brand_hits or strong_hits)   # only ambiguous keywords matched
    sources = sorted(brand_src | strong_src | weak_src)
    if brand_hits:
        lead = "matched prohibited brand " + ", ".join(f"'{b}'" for b in brand_hits)
    elif strong_hits:
        lead = "matched " + ", ".join(f"'{t}'" for t in strong_hits)
    else:
        lead = "ambiguous vice keyword " + ", ".join(f"'{t}'" for t in weak_hits)
    prefix = "prohibited category" + (" (BORDERLINE — review)" if borderline else "")
    reason = f"{prefix}: {lead} (in {', '.join(sources)})"
    return {"reason": reason, "matched": brand_hits + strong_hits + weak_hits,
            "borderline": borderline, "sources": sources}


def classify_openness(text, join_cta=None):
    """Is the campaign open to an instant free join? 'no' (application/selection-gated),
    'yes' (clearly open — anyone can join now), or 'unclear' (no explicit signal → neutral).

    The page CTA wins when known: an 'Apply' button means gated, a 'Join' button means open.
    Otherwise reads the brief. Never guesses — absence of any signal is 'unclear'.
    """
    t = text or ""
    if join_cta == "apply" or _DQ_APPLICATION.search(t):
        return "no"
    if join_cta == "join" or _OPEN_JOIN.search(t):
        return "yes"
    return "unclear"


# --- category tagging ----------------------------------------------------------
_CATEGORY_KEYWORDS = {
    "streamer_irl": ["twitch", "kick.com", "streamer", " irl", "just chatting",
                     "live stream", "livestream", "stream highlight", "subathon", "vtuber"],
    "gaming": ["gaming", "gameplay", "fortnite", "minecraft", "valorant", "warzone",
               "call of duty", "league of legends", " gta", "roblox", "apex legends",
               "speedrun", " fps ", "gamer", "esports"],
    "sports": ["nba", "nfl", "soccer", "football", " ufc", " mma", "boxing", "athlete",
               "formula 1", " f1 ", "basketball", "wrestling", " wwe", "sports"],
    "podcast_talking": ["podcast", "episode", "interview", "talking", "guest", "the show",
                        "sit-down", "sit down", "conversation", "commentary"],
    "brand_product": ["brand", "product", " app ", "saas", "ecommerce", "e-commerce",
                      "shop", "our product", "company", "startup", "software", "promote our"],
    "music": ["music", " song", "artist", " track", "album", "spotify", "rapper",
              "musician", "producer", " beat "],
    "meme": ["meme", "funny", "shitpost", "viral moment", "comedy skit", "clip compilation"],
}


def _category_scores(name, text, platforms=None):
    """Raw keyword hit-count per category (+ the twitch/kick streamer boost). Shared by the
    single-best `classify_category` and the multi-tag `classify_categories`."""
    hay = f" {name or ''} {text or ''} ".lower()
    scores = {cat: sum(hay.count(k) for k in kws) for cat, kws in _CATEGORY_KEYWORDS.items()}
    if set(platforms or []) & {"twitch", "kick"}:
        scores["streamer_irl"] += 2
    return scores


def classify_category(name, text, platforms=None):
    """Tag a campaign with its single BEST category: streamer_irl / gaming / sports /
    podcast_talking / brand_product / music / meme / other. Keyword-scored; ties break by the
    order above. 'other' when nothing matches (honest, not a guess)."""
    scores = _category_scores(name, text, platforms)
    best = max(scores, key=lambda c: scores[c])
    return best if scores[best] > 0 else "other"


def classify_categories(name, text, platforms=None):
    """ALL categories a campaign fits (multi-tag), for category-level ranking — a sports-podcast
    counts toward BOTH sports and podcast_talking. Every category with a keyword hit, ordered by
    hit-count (then the canonical order). `['other']` when nothing matches — never empty."""
    scores = _category_scores(name, text, platforms)
    order = list(_CATEGORY_KEYWORDS)
    hits = [c for c in order if scores[c] > 0]
    hits.sort(key=lambda c: (-scores[c], order.index(c)))
    return hits or ["other"]


# --- source handles ------------------------------------------------------------
_URL_RE = re.compile(r"https?://[^\s)>\]\"'}]+", re.I)
# Bare "@handle on tiktok"-style mentions where a platform word sits nearby.
_MENTION_RE = re.compile(r"@([A-Za-z0-9_.]{2,30})", re.I)


def _classify_host(url):
    u = (url or "").lower()
    for platform, hosts in SOCIAL_HOSTS.items():
        if any(h in u for h in hosts):
            return platform
    return None


def _handle_from_url(platform, url):
    """Best-effort readable handle from a profile URL ('.../@name' or '.../name')."""
    try:
        path = urlparse(url).path.strip("/")
    except Exception:
        return None
    if not path:
        return None
    seg = path.split("/")[0]
    if platform == "youtube" and not seg.startswith("@") and seg in ("channel", "c", "user"):
        parts = path.split("/")
        seg = parts[1] if len(parts) > 1 else seg
    return seg or None


def extract_handles(text, source_links=None):
    """Social profile handles referenced by a campaign brief.

    Pulls full URLs out of `text` and `source_links`, classifies each by host into a
    known platform (TikTok/YouTube/Instagram/Kick/X), and de-dupes. Returns a list of
    dicts: {platform, url, handle, followers: None, recent_avg_views: None}. The count
    fields start None on purpose — the social lookup fills them, and anything it can't
    retrieve stays None (unknown), never fabricated.
    """
    seen = {}
    urls = list(source_links or [])
    if text:
        urls += _URL_RE.findall(text)
    for raw in urls:
        url = (raw or "").rstrip('.,);]}"\'')
        platform = _classify_host(url)
        if not platform:
            continue
        key = (platform, url.lower())
        if key in seen:
            continue
        seen[key] = {
            "platform": platform,
            "url": url,
            "handle": _handle_from_url(platform, url),
            "followers": None,
            "recent_avg_views": None,
        }
    return list(seen.values())


def name_from_aria(aria):
    """'View <NAME> campaign' -> '<NAME>'."""
    if not aria:
        return None
    m = re.match(r"\s*View\s+(.*?)\s+campaign\s*$", aria)
    return (m.group(1) if m else aria).strip() or None


# --- locator helpers -----------------------------------------------------------
def _text(scope, candidates):
    """First non-empty inner_text among candidate selectors, else None."""
    for sel in candidates:
        try:
            loc = scope.locator(sel).first
            if loc.count() > 0:
                txt = loc.inner_text(timeout=1500).strip()
                if txt:
                    return txt
        except Exception:
            continue
    return None


def _safe_inner_text(scope):
    try:
        return scope.inner_text(timeout=1500)
    except Exception:
        return ""


def _first_href(scope, candidates):
    for sel in candidates:
        try:
            loc = scope.locator(sel).first
            if loc.count() > 0:
                href = loc.get_attribute("href")
                if href:
                    return href
        except Exception:
            continue
    return None


def _all_hrefs(scope, candidates):
    out = []
    for sel in candidates:
        try:
            locs = scope.locator(sel)
            for i in range(locs.count()):
                href = locs.nth(i).get_attribute("href")
                if href and href not in out:
                    out.append(href)
        except Exception:
            continue
    return out


# --- blocker detection ---------------------------------------------------------
def is_challenge(page):
    for sel in S.CHALLENGE_MARKERS:
        try:
            if page.locator(sel).first.count() > 0:
                return True
        except Exception:
            continue
    return False


def is_login_wall(page):
    url = (page.url or "").lower()
    if any(k in url for k in ("/login", "sign-in", "signin", "/auth")):
        return True
    try:
        if page.locator('input[type="password"]').first.count() > 0:
            return True
    except Exception:
        pass
    return False


# --- extraction ----------------------------------------------------------------
def extract_card(card):
    """Card-level fields from the list view. Never raises.

    Cards are <button> elements with no href/id: the name comes from the button's
    aria-label ("View <NAME> campaign"), the id is a slug of that name, and there is
    no per-card URL (the detail URL is only obtainable by clicking, done in Phase 2).
    """
    try:
        aria = card.get_attribute("aria-label")
    except Exception:
        aria = None
    name = name_from_aria(aria) or _text(card, S.CARD_NAME)

    pay = parse_pay(_text(card, S.CARD_PAY))
    paid, total, rem = parse_budget_pair(_text(card, S.CARD_BUDGET))
    platforms_text = _text(card, S.CARD_PLATFORMS) if S.CARD_PLATFORMS else None

    return {
        "id": slugify(name),
        "name": name,
        "url": None,
        "pay_value": pay["pay_value"],
        "pay_unit": pay["pay_unit"],
        "pay_per_1k": pay["pay_per_1k"],
        "budget_paid": paid,
        "budget_total": total,
        "budget_remaining_fraction": rem,
        "platforms": parse_platforms(platforms_text),
    }


def _join_texts(scope, candidates, sep="\n"):
    """Join the text of ALL matches of the first candidate that matches anything."""
    for sel in candidates:
        out = []
        try:
            locs = scope.locator(sel)
            for k in range(locs.count()):
                t = locs.nth(k).inner_text(timeout=1500).strip()
                if t and t not in out:
                    out.append(t)
        except Exception:
            continue
        if out:
            return sep.join(out)
    return None


_CTA_APPLY_RE = re.compile(r"\bappl(?:y|ication)\b", re.I)
_CTA_JOIN_RE = re.compile(r"\bjoin\b", re.I)


def _detect_join_cta(scope):
    """Read the primary join CTA: 'apply' (application-gated) or 'join' (instant open), else
    None. Best-effort over button/link elements (selectors unconfirmed). An Apply CTA wins —
    it's the disqualifying signal — so we scan all candidates and prefer it. Never raises."""
    saw_join = False
    for sel in S.DETAIL_JOIN_CTA:
        try:
            locs = scope.locator(sel)
            count = locs.count()
        except Exception:
            continue
        for i in range(min(count, 40)):
            try:
                txt = (locs.nth(i).inner_text(timeout=800) or "").strip()
            except Exception:
                continue
            if not txt or len(txt) > 40:      # a real CTA label is short; skip prose
                continue
            if _CTA_APPLY_RE.search(txt):
                return "apply"
            if _CTA_JOIN_RE.search(txt):
                saw_join = True
        if saw_join:
            return "join"
    return "join" if saw_join else None


def extract_detail(scope):
    """Detail fields from the campaign dialog. `scope` is the dialog Locator (or a
    frame/page). Missing -> None; never raises."""
    pay = parse_pay(_text(scope, S.DETAIL_PAY))
    # Budget shows as "$paid/$total" (same as the card); parse the pair.
    budget_paid, budget_total, budget_rem = parse_budget_pair(_text(scope, S.DETAIL_BUDGET_TOTAL))
    platforms_text = _text(scope, S.DETAIL_PLATFORMS) if S.DETAIL_PLATFORMS else None
    source_links = [absolute(h) for h in _all_hrefs(scope, S.SOURCE_LINK_SELECTORS)]

    # Requirement bullets (span.break-all); fall back to the whole dialog text.
    rules = _join_texts(scope, S.DETAIL_RULES)
    if not rules:
        rules = _safe_inner_text(scope) or None

    # approval rate — the header "NN% approval rate". Try the dedicated element, then fall
    # back to parsing the dialog text (the header rate is the first "NN% approval rate").
    approval_rate = parse_approval_rate(_text(scope, S.DETAIL_APPROVAL_RATE))
    if approval_rate is None:
        approval_rate = parse_approval_rate(rules)

    return {
        "name": _text(scope, S.DETAIL_NAME),
        "creator": _text(scope, S.DETAIL_CREATOR),
        "approval_rate": approval_rate,
        "pay_value": pay["pay_value"],
        "pay_unit": pay["pay_unit"],
        "pay_per_1k": pay["pay_per_1k"],
        "budget_paid": budget_paid,
        "budget_total": budget_total,
        "budget_remaining_fraction": budget_rem,
        "platforms": parse_platforms(platforms_text),
        "source_links": source_links,
        "rules_text": rules,
        "participants": parse_int(_text(scope, S.DETAIL_PARTICIPANTS)) if S.DETAIL_PARTICIPANTS else None,
        "deadline": _text(scope, S.DETAIL_DEADLINE),
        "join_cta": _detect_join_cta(scope),
    }
