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


# --- self-sourced footage ------------------------------------------------------
# Some campaigns PROVIDE no footage — their rules/docs tell clippers to find their OWN
# ("find your own footage", "source your own clips", "use any footage of <person>",
# "clip any stream you find", "we don't provide footage"). These are un-clippable by a
# footage-DOWNLOAD pipeline, so scoring heavily deranks them. Detection is FLEXIBLE about the
# concept but HIGH-PRECISION (fail-open: a normal campaign must NEVER be flagged), and it
# LOCALLY SUPPRESSES a match when the surrounding words say footage IS provided (a "source
# footage library", a "content folder", "more footage is being added" — the BitLife case).
_SS_MEDIA = (r"(?:footage|clips?|streams?|vods?|videos?|content|highlights?|moments?|"
             r"material|gameplay)")
# an explicit "we don't provide footage / footage is not provided" — decisive on its own, and
# it CONTAINS the word 'provided', so it is checked SEPARATELY (never locally suppressed).
_SS_NO_PROVIDE = re.compile(
    r"\bwe\s+(?:do\s*n[o'’]?t|don['’]?t|do\s+not|will\s+not|won['’]?t|cannot|can['’]?t)\s+"
    r"(?:provide|supply|give|offer)\s+(?:any\s+|the\s+|raw\s+|source\s+)?"
    r"(?:footage|clips?|content|source\s+material|videos?)\b"
    r"|\bno\s+(?:raw\s+|source\s+)?(?:footage|clips?|source\s+material|videos?)\s+"
    r"(?:is\s+|are\s+|will\s+be\s+)?(?:provided|supplied|given|included|available|offered)\b"
    r"|\b(?:footage|clips?|content|source\s+material|videos?)\s+(?:is|are|will\s+be)\s+not\s+"
    r"(?:provided|supplied|included|given|available|offered)\b", re.I)
# the "find/use your own footage" family — genuine self-sourcing. Note 'create/make/edit/post
# your own clips' means PRODUCING clips (not sourcing footage) and is deliberately NOT matched.
_SS_PATTERNS = (
    # "<source-verb> your own [adj/name] <media>" — find/use your own [BitLife] footage/streams
    r"\b(?:find|sourc(?:e|ing)|gather|collect|pull|grab|obtain|get|use|dig\s+up|scour"
    r"(?:\s+for)?)\s+your\s+own\s+(?:[A-Za-z][\w'’-]*\s+){0,2}" + _SS_MEDIA + r"\b",
    # "your own [adj] footage/streams/vods/gameplay" as raw SOURCE material (not 'your own clips')
    r"\byour\s+own\s+(?:raw\s+|[A-Za-z][\w'’-]*\s+){0,2}"
    r"(?:footage|streams?|vods?|gameplay|source\s+material)\b",
    # "<verb> any <media> (of <X> | you find)" — use any footage of X / clip any stream you find.
    # NOTE: 'from' is deliberately excluded ("use any clip FROM outside this channel" is the
    # OPPOSITE — provided-footage-only — and negated forms are dropped by the negation guard).
    r"\b(?:use|clip|find|pull|grab|take|source|download)\s+any\s+" + _SS_MEDIA +
    r"\s+(?:of|you(?:\s+can)?\s+find)\b",
    # "any <media> of <X> you (can) find"
    r"\bany\s+" + _SS_MEDIA + r"\s+of\s+[^.\n]{1,40}?\byou(?:\s+can)?\s+find\b",
    # "find/source the footage yourself | on your own"
    r"\b(?:find|sourc(?:e|ing)|gather|locate)\s+(?:the\s+|all\s+)?" + _SS_MEDIA +
    r"[^.\n]{0,30}?\b(?:yourself|on\s+your\s+own)\b",
    # "you must find/source/provide the footage"
    r"\byou(?:['’]ll|\s+will)?\s+(?:must|need\s+to|have\s+to|are\s+expected\s+to|are\s+"
    r"responsible\s+for)\s+(?:find(?:ing)?|sourc(?:e|ing)|gather(?:ing)?|locat(?:e|ing)|"
    r"provid(?:e|ing))\s+(?:the\s+|your\s+own\s+|all\s+)?" + _SS_MEDIA + r"\b",
)
_SS_RE = [re.compile(p, re.I) for p in _SS_PATTERNS]
# words near a match that mean footage IS provided here (so the match is NOT self-sourcing)
_SS_PROVIDED_NEARBY = re.compile(
    r"\b(?:provided|we\s+provide|is\s+provided|are\s+provided|will\s+be\s+provided|library|"
    r"folder|below|here|link(?:ed)?|available|supplied|attached|in\s+the\s+(?:doc|drive))\b",
    re.I)
# a NEGATION right before the match REVERSES the instruction ("do NOT use any clip from
# outside...", "you cannot use your own footage") — footage is provided-only, NOT self-sourcing.
_SS_NEGATION_BEFORE = re.compile(
    r"\b(?:do\s*n[o'’]?t|don['’]?t|do\s+not|never|cannot|can['’]?t|must\s+not|"
    r"may\s+not|are\s+not\s+(?:allowed|permitted)|not\s+allowed\s+to)\s+\w*\s*$", re.I)


def detect_self_sourced(text):
    """The matched self-sourced-footage phrase (campaign provides no footage; clipper must find
    their own), or None when nothing genuine matches. Fail-open by design — high precision: a
    'find your own' match is dropped when nearby words show footage IS provided, and a NEGATED
    instruction ('do NOT use any clip from outside this channel') is dropped too. Pure/testable."""
    if not text:
        return None
    m = _SS_NO_PROVIDE.search(text)      # decisive; never locally suppressed
    if m:
        return " ".join(m.group(0).split())[:120]
    for rx in _SS_RE:
        for m in rx.finditer(text):
            if _SS_PROVIDED_NEARBY.search(text[max(0, m.start() - 40): m.end() + 40]):
                continue                 # footage provided nearby — not self-sourcing
            if _SS_NEGATION_BEFORE.search(text[max(0, m.start() - 24): m.start()]):
                continue                 # instruction is NEGATED — the opposite of self-sourcing
            return " ".join(m.group(0).split())[:120]
    return None


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
# Spanish/Portuguese: a MINIMUM payout gated behind a VIEW count — "Pago mínimo por reel: $4
# (10.000 visualizaciones)", "mínimo 10.000 visitas para pagar/cobrar", "necesitas N vistas
# para el pago". Anchored on MÍNIMO (never máximo) so the max-payout view figure is not read as
# a gate. Numbers use European "." grouping ("10.000" = 10,000) — handled by _views_to_int.
_MV_VIEWS_ES = r"(?:visualizaciones|visualizacoes|visualizações|visitas|vistas|reproducciones|reproduccoes|reproduções|views)"
_MV_MIN_ES = r"(?:pago\s+m[íi]nimo|m[íi]nimo\s+(?:de\s+)?(?:pago|retiro)|para\s+(?:pagar|cobrar|el\s+pago|el\s+retiro|retirar))"
_MIN_VIEW_PATTERNS = (
    # A: gating word, then "<num> views"  — "must reach 10K views", "minimum 10,000 views"
    _MV_BEFORE + r"\s+(?:of\s+)?([0-9][0-9.,]*)\s*([kKmMbB]?)\s*" + _VIEWS,
    # B: "<num> views", then gating word  — "10K views for payout", "10k views required"
    r"([0-9][0-9.,]*)\s*([kKmMbB]?)\s*" + _VIEWS + r"[^.\n]{0,25}?" + _MV_AFTER,
    # C: gating word, then "<num>K/M" with views IMPLIED (no "views" word, not $/followers),
    #    then payout context — catches "MUST REACH 10K FOR PAYOUT" (unit is mandatory here).
    _MV_BEFORE + r"\s+([0-9][0-9.,]*)\s*([kKmMbB])\b(?!\s*(?:follow|sub|dollar|usd))"
    r"[^.\n]{0,20}?" + _MV_PAYOUT_CTX,
    # D (ES/PT): "pago mínimo ... <num> visualizaciones/visitas" — minimum payout tied to views.
    _MV_MIN_ES + r"[^.\n]{0,40}?([0-9][0-9.,]*)\s*([kKmMbB]?)\s*" + _MV_VIEWS_ES,
    # E (ES/PT): "<num> visualizaciones/visitas ... para pagar/cobrar / mínimo" — gate after count.
    r"([0-9][0-9.,]*)\s*([kKmMbB]?)\s*" + _MV_VIEWS_ES +
    r"[^.\n]{0,30}?(?:para\s+(?:pagar|cobrar)|m[íi]nimo)",
)


def _views_to_int(numstr, unit):
    """View count -> int, handling US ('10,000'/'1.5K') AND European ('10.000') number formats.
    A dot/comma run of 3-digit groups ('10.000', '1,234,567') is thousands grouping and is
    stripped; a single dot/comma with 1-2 trailing digits next to a k/m/b unit ('1,5K') is a
    decimal. None on garbage."""
    s = str(numstr).strip()
    u = (unit or "").lower()
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", s):        # 10.000 / 10,000 / 1.234.567 -> grouped
        s = s.replace(".", "").replace(",", "")
    elif re.fullmatch(r"\d+[.,]\d{1,2}", s) and u in ("k", "m", "b"):  # 1,5K / 1.5K -> decimal
        s = s.replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        n = float(s)
    except (TypeError, ValueError):
        return None
    mult = {"k": 1e3, "m": 1e6, "b": 1e9}.get(u, 1)
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


# --- dedicated-page/account requirement ---------------------------------------
# A real trap: the rules demand a page/account/channel used ONLY for this campaign's content
# ("must be a dedicated page for X", "página dedicada únicamente al contenido de X"). That burns
# a whole account slot and is incompatible with a general themed clip account, so scoring deranks
# it below campaigns usable from an account I already run. English + Spanish/Portuguese. Detection
# is high-precision (fail-open): only explicit "dedicated/exclusive page/account" language fires,
# never a passing "we're dedicated to quality". The object phrase is captured for the report.
_DEDICATED_PAGE_RE = re.compile(
    # EN: "dedicated page/account/channel/profile [for/to X]" / "page dedicated to/only X"
    r"\bdedicated\s+(?:page|account|channel|profile|handle)\b(?:\s+(?:for|to|only|solely|"
    r"exclusively)[^.\n]{0,40})?"
    r"|\b(?:page|account|channel|profile)\s+(?:that\s+is\s+|must\s+be\s+|entirely\s+)?"
    r"dedicated\s+(?:to|only|solely|exclusively)[^.\n]{0,40}"
    r"|\b(?:page|account|channel)\s+(?:used\s+)?(?:only|solely|exclusively)\s+for[^.\n]{0,40}"
    r"|\bseparate\s+(?:dedicated\s+)?(?:page|account|channel)\s+(?:for|dedicated)[^.\n]{0,40}"
    # ES/PT: "página/cuenta/canal/perfil dedicad[ao] [únicamente] [al contenido de X]"
    r"|\b(?:p[áa]gina|cuenta|canal|perfil)\s+dedicad[ao]s?(?:\s+[úu]nicamente|\s+exclusivamente)?"
    r"(?:\s+(?:al?|a\s+la|para|ao?|à)[^.\n]{0,40})?"
    # ES/PT: "página/cuenta exclusiva [para X]"
    r"|\b(?:p[áa]gina|cuenta|canal|perfil)\s+exclusiv[ao]s?(?:\s+(?:para|de|al?)[^.\n]{0,40})?",
    re.I)


def detect_dedicated_page(text):
    """The matched phrase if the rules require a DEDICATED page/account (used only for this
    campaign's content — a real account-slot cost), else None. English + Spanish/Portuguese,
    high-precision/fail-open. Pure/testable."""
    if not text:
        return None
    m = _DEDICATED_PAGE_RE.search(text)
    if not m:
        return None
    return " ".join(m.group(0).split())[:120]


# --- PERSON-NAME dedicated account requirement (distinct from the category dedication above) ---
# A worse trap than a generic dedicated page: the rules demand the posting ACCOUNT / channel /
# USERNAME be dedicated to a SPECIFIC NAMED PERSON — "username must contain Yomi", "dedicated JZ
# Garcia page", "account dedicated to clipping <Name>". That forces a brand-NEW dedicated account
# PER campaign (high cost, doesn't scale), so scoring deranks it. It must be DISTINGUISHED from a
# generic CATEGORY/theme dedication ("dedicated clipping account", "sports account", "faceless
# page") — those are fine and must NOT derank. The distinguishing signal: the dedication target
# is a PROPER NAME (Capitalized, matches the creator, and is not a category word), not a lowercase
# theme word. Fail-loud: a pattern that matches but whose target can't be confidently classified
# is returned as `uncertain` (no penalty, but surfaced/logged) rather than silently deranked.
#
# Category / theme words a dedication target may be — these are NOT person names, so a dedication
# to one of them is a generic (allowed) dedication, never the person derank.
_DEDICATION_GENERIC = {
    "clipping", "clips", "clip", "clipper", "clippers", "content", "edit", "edits", "editing",
    "compilation", "compilations", "highlight", "highlights", "fan", "fans", "fanpage",
    "theme", "themed", "niche", "sports", "sport", "gaming", "game", "games", "gamer", "meme",
    "memes", "funny", "viral", "faceless", "face", "streamer", "streamers", "streaming",
    "stream", "streams", "podcast", "podcasts", "news", "music", "brand", "branded", "product",
    "movie", "movies", "tv", "film", "films", "football", "soccer", "basketball", "nba", "nfl",
    "ufc", "mma", "anime", "reaction", "reactions", "irl", "vlog", "vlogs", "the", "this",
    "that", "your", "our", "new", "dedicated", "separate", "official", "campaign", "page",
    "account", "channel", "profile", "only", "specific", "related", "topic", "niche", "our",
    "single", "one",
}
# Structural nouns a captured target may trail into — trimmed off before classifying.
_DEDICATION_STRUCTURAL = {"page", "account", "channel", "profile", "fanpage", "handle", "pages"}
# A proper-name token: starts uppercase (covers "Yomi", "Denzel", and all-caps handles "JZ").
_NAME_TOK = r"[A-Z][A-Za-z0-9'’.\-]*"
_PERSON_DEDICATED_PATTERNS = (
    # A: username / handle / channel name MUST contain / include / start-with <target>
    (r"(?:user\s?name|handle|display\s+name|channel\s+name|page\s+name|account\s+name)\s+"
     r"(?:must|has\s+to|have\s+to|should|needs?\s+to)\s+"
     r"(?:contain|include|have|start\s+with|begin\s+with|feature|reference|mention|be)\s+"
     r"(?:the\s+(?:name\s+|word\s+)?)?[\"'“”]?"
     r"(" + _NAME_TOK + r"(?:\s+" + _NAME_TOK + r"){0,2})"),
    # B: "dedicated <Name> page/account/channel/profile/fanpage"  ("dedicated JZ Garcia page")
    (r"dedicated\s+(" + _NAME_TOK + r"(?:\s+" + _NAME_TOK + r"){0,2})\s+"
     r"(?:page|account|channel|profile|fan\s?page)"),
    # C: "page/account/... dedicated to (clipping/posting/...) <Name>"
    (r"(?:page|account|channel|profile|fan\s?page)\s+(?:that\s+is\s+|must\s+be\s+)?"
     r"dedicated\s+to\s+(?:clipping|posting|covering|uploading|only)?\s*"
     r"(" + _NAME_TOK + r"(?:\s+" + _NAME_TOK + r"){0,2})"),
)
_PERSON_DEDICATED_RE = [re.compile(p, re.I) for p in _PERSON_DEDICATED_PATTERNS]


def _classify_dedication_target(target, creator_name=None):
    """(is_person, confidence, reason). is_person is True (a specific named person -> derank),
    False (a generic category/theme -> do NOT derank), or None (matched but AMBIGUOUS -> fail-loud,
    surfaced not silently deranked)."""
    if not target:
        return False, None, "empty target"
    toks = [t for t in re.split(r"\s+", target.strip()) if t]
    while toks and toks[-1].lower().strip(".,'’") in _DEDICATION_STRUCTURAL:
        toks.pop()   # drop a trailing "page"/"account" the pattern swept in
    if not toks:
        return False, None, "only structural words (page/account)"
    low = [t.lower().strip(".,'’") for t in toks]
    if all(t in _DEDICATION_GENERIC for t in low):
        return False, None, f"generic/category dedication ('{' '.join(low)}') — not a person"
    if creator_name:
        cn = {w.lower().strip(".,'’") for w in re.split(r"[\s@/]+", creator_name) if len(w) > 1}
        if cn & {t for t in low if t not in _DEDICATION_GENERIC}:
            return True, "high", f"target matches creator name '{creator_name}'"
    proper = [t for t, l in zip(toks, low)
              if re.match(r"[A-Z]", t) and l not in _DEDICATION_GENERIC]
    if proper:
        return True, "low", f"proper name '{' '.join(proper)}' (not a category word)"
    return None, None, f"ambiguous target '{' '.join(toks)}'"


def detect_person_dedicated(text, creator_name=None):
    """Does the rules text require the posting ACCOUNT / channel / USERNAME be dedicated to a
    SPECIFIC NAMED PERSON ("username must contain Yomi", "dedicated JZ Garcia page", "account
    dedicated to clipping <Name>")? That forces a brand-new dedicated account PER campaign — a
    real cost that doesn't scale — so scoring deranks it. DISTINCT from a generic CATEGORY/theme
    dedication ("dedicated clipping account", "sports account", "faceless page"), which is fine
    and never fires here.

    High-precision + FAIL-LOUD: when a dedication/username pattern matches but the target can't be
    confidently classified as a person vs a category, `uncertain=True` (NO penalty applied, but
    surfaced so it can be logged/reviewed). A clear person match short-circuits and wins. Pass the
    creator name (when known) to strengthen the match. Pure/testable. Returns a dict:
        {required, phrase, target, is_person, confidence, uncertain, reason}."""
    out = {"required": False, "phrase": None, "target": None, "is_person": False,
           "confidence": None, "uncertain": False, "reason": "no person-dedication requirement"}
    if not text:
        return out
    for rx in _PERSON_DEDICATED_RE:
        for m in rx.finditer(text):
            target = (m.group(1) or "").strip(" \t\"'“”.,")
            is_person, conf, reason = _classify_dedication_target(target, creator_name)
            phrase = " ".join(m.group(0).split())[:140]
            if is_person is True:
                out.update(required=True, phrase=phrase, target=target, is_person=True,
                           confidence=conf, uncertain=False, reason=reason)
                return out   # a clear person match wins outright
            if is_person is None:
                # matched but ambiguous — remember it (fail-loud), keep scanning for a clearer hit
                out.update(phrase=phrase, target=target, is_person=False,
                           uncertain=True, reason=reason)
    return out


# --- member-gated / incomplete rules -------------------------------------------
# Some campaigns keep the REAL rules behind joining ("CHECK FULL GUIDELINES ON SIDEBAR AFTER YOU
# JOIN", "full rules after joining", "reglas completas después de unirte"), so Scout only ever
# sees a PARTIAL rule set. This is a FLAG (not a derank): it tells me the captured rules can't be
# fully trusted. High-precision English + Spanish/Portuguese; fail-open (no match -> None).
_RULES_INCOMPLETE_RE = re.compile(
    # EN: "full/complete/detailed guidelines|rules|requirements ... after/once you join/accepted"
    r"\b(?:full|complete|all|detailed|the\s+full|the\s+complete)\s+"
    r"(?:guidelines?|rules?|requirements?|details?|instructions?|brief)\b[^.\n]{0,50}?"
    r"\b(?:after|once|when|upon)\s+(?:you\s+)?(?:join|joining|are\s+accepted|accepted|inside)\b"
    # EN: "after/once you join ... guidelines|rules|sidebar|discord|whop"
    r"|\b(?:after|once|when)\s+(?:you\s+)?(?:join|joining|are\s+accepted)\b[^.\n]{0,50}?"
    r"\b(?:guidelines?|rules?|requirements?|full\s+details?|sidebar|whop|discord|server)\b"
    # EN: "join to see/access the (full) rules|guidelines"
    r"|\bjoin\s+(?:to\s+)?(?:see|view|access|read|get|unlock)\s+(?:the\s+)?(?:full\s+)?"
    r"(?:rules?|guidelines?|requirements?|details?)\b"
    # EN: "guidelines/rules on the sidebar|discord|whop" (the sidebar/whop is post-join)
    r"|\b(?:full\s+)?(?:guidelines?|rules?)\s+(?:are\s+)?(?:on|in)\s+(?:the\s+)?"
    r"(?:sidebar|whop\s+(?:sidebar|channel)|discord\s+(?:after|once))\b"
    # ES/PT: "reglas/normas/guía completas ... después de/al unir(te|se)/entrar"
    r"|\b(?:reglas?|normas?|gu[íi]as?|requisitos?|instrucciones?|regras?)\s+"
    r"(?:completa?s?|detallada?s?|completas?)\b[^.\n]{0,50}?"
    r"\b(?:despu[ée]s\s+de|al|una\s+vez\s+que)\s+(?:unir(?:te|se)|entrar|ingresar|aceptad)"
    r"|\b(?:despu[ée]s\s+de|al|una\s+vez)\s+(?:unir(?:te|se)|entrar|ingresar)\b[^.\n]{0,50}?"
    r"\b(?:reglas?|normas?|gu[íi]a|requisitos?|regras?)\b",
    re.I)


def detect_rules_incomplete(text):
    """The matched phrase if the rules say the FULL guidelines live behind joining (so the
    captured rules are only PARTIAL), else None. English + Spanish/Portuguese; a FLAG, not a
    derank. High-precision/fail-open. Pure/testable."""
    if not text:
        return None
    m = _RULES_INCOMPLETE_RE.search(text)
    if not m:
        return None
    return " ".join(m.group(0).split())[:120]


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


# --- modal rules extraction ----------------------------------------------------
# Rules live in DIFFERENT places per campaign. Some render the full requirements
# inline in the modal (Santa Cruz: required hashtags, on-screen-text format, min
# length, English-only, no watermark), some only leave a POINTER bullet ("SEE RULES
# ... BELOW IN RESOURCES") and put the real rules in a linked Doc. The span.break-all
# bullets often capture ONLY the pointer, so we also pull the substantive rules
# section out of the dialog's full visible text for the clipper (modal_rules_text).

# A requirements line that only POINTS elsewhere carries no actual rules — strip
# these before judging substance so a pointer alone never reads as "has rules".
RULES_POINTER_PHRASES = (
    "refer to the google docs", "refer to the google doc", "refer to google docs",
    "refer to google doc", "refer to the doc", "refer to the docs", "refer to the brief",
    "refer to the resources", "refer to resources", "see the google doc", "see the doc",
    "see the requirements doc", "see below", "see resources", "guidelines on content links",
    "guidelines on the content links", "guidelines on content", "content requirements",
    "in the google doc", "in the doc below", "link below", "links below",
    "for the campaign requirements", "for the requirements", "campaign requirements",
    "see rules, requirements, and content document below in resources",
    "rules, requirements, and content document below in resources",
)
# Words/patterns that betray REAL rules even in a short section.
RULE_SIGNAL_RE = re.compile(
    r"\b(must|required|do not|don'?t|banned|prohibited|watermark|caption|hashtag|on-?screen|"
    r"audience|tier|provided footage|no outside|comment|mention|disclosure|geo|min |max )"
    r"|#\w|\d+%", re.I)


def modal_rules_section(modal_text):
    """The requirements/guidelines section of the modal text (between a 'Content
    Requirements'/'Requirements' heading and the Earnings/Analytics/Resources blocks),
    or '' if none. Pure — no Playwright."""
    if not modal_text:
        return ""
    t = " ".join(modal_text.split())
    m = re.search(r"Content Requirements(.*?)(?:\bEarnings\b|\bAnalytics\b|\bResources\b|$)",
                  t, re.I | re.S) or re.search(
        r"\bRequirements\b(.*?)(?:\bEarnings\b|\bAnalytics\b|\bResources\b|$)", t, re.I | re.S)
    return (m.group(1).strip() if m else "")


def has_substantive_rules(text):
    """True if `text` carries actual rules (not just a pointer to a doc). Substance =
    enough words left after removing pointer phrases, OR any concrete rule-signal
    keyword. Pure — no Playwright."""
    if not text or not text.strip():
        return False
    low = text.lower()
    for p in RULES_POINTER_PHRASES:
        low = low.replace(p, " ")
    words = re.findall(r"[a-z0-9%+$#]+", low)
    return len(words) >= 6 or bool(RULE_SIGNAL_RE.search(text))


def full_modal_rules(full_text, bullets=None):
    """The best available FULL rules text for the clipper, drawn from the modal.
    Prefers the clean requirements SECTION of the dialog text; falls back to the whole
    dialog text when it carries real rules but has no clean heading; else to substantive
    bullets. Returns None when only a pointer (or nothing) is present. Pure — no
    Playwright, safe to test on plain strings."""
    section = modal_rules_section(full_text)
    if has_substantive_rules(section):
        return section
    if has_substantive_rules(full_text):
        return (full_text or "").strip() or None
    if has_substantive_rules(bullets):
        return (bullets or "").strip() or None
    return None


def extract_detail(scope):
    """Detail fields from the campaign dialog. `scope` is the dialog Locator (or a
    frame/page). Missing -> None; never raises."""
    pay = parse_pay(_text(scope, S.DETAIL_PAY))
    # Budget shows as "$paid/$total" (same as the card); parse the pair.
    budget_paid, budget_total, budget_rem = parse_budget_pair(_text(scope, S.DETAIL_BUDGET_TOTAL))
    platforms_text = _text(scope, S.DETAIL_PLATFORMS) if S.DETAIL_PLATFORMS else None
    source_links = [absolute(h) for h in _all_hrefs(scope, S.SOURCE_LINK_SELECTORS)]

    # The dialog's full visible text — pulled from the dialog Locator that already
    # yielded pay/budget/approval, so it is reliable even when the separate frame-eval
    # capture (modal_requirements_text) comes back empty.
    dialog_text = _safe_inner_text(scope) or None

    # Requirement bullets (span.break-all); fall back to the whole dialog text.
    rules = _join_texts(scope, S.DETAIL_RULES)
    if not rules:
        rules = dialog_text

    # Full rules for the clipper: many campaigns leave only a POINTER bullet in
    # span.break-all ("SEE RULES ... BELOW IN RESOURCES") while the real rules
    # (hashtags, on-screen format, requirements) render elsewhere in the modal. Pull the
    # substantive rules section out of the dialog text so the clipper gets real rules,
    # not just the pointer. rules_text (the scoring input) is left as-is.
    modal_rules_text = full_modal_rules(dialog_text, rules)

    # approval rate — the header "NN% approval rate". Try the dedicated element(s) first, then
    # fall back to the FULL dialog innerText (dialog_text), which reliably carries the header
    # line even when the selectors miss — NOT the `rules` bullets, which are the requirement
    # list and never contain the header. This is what makes approval capture reliable on every
    # scrape (the header rate is the first "NN% approval rate" in the text). None -> UNKNOWN.
    approval_rate = parse_approval_rate(_text(scope, S.DETAIL_APPROVAL_RATE))
    if approval_rate is None:
        approval_rate = parse_approval_rate(dialog_text)
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
        "modal_rules_text": modal_rules_text,
        "dialog_text": dialog_text,   # full modal innerText; backfills modal_requirements_text
        "participants": parse_int(_text(scope, S.DETAIL_PARTICIPANTS)) if S.DETAIL_PARTICIPANTS else None,
        "deadline": _text(scope, S.DETAIL_DEADLINE),
        "join_cta": _detect_join_cta(scope),
    }
