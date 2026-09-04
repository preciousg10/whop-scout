"""Offline, zero-cost language detection for the non-English derank (see scoring/composite).

Scout is an ENGLISH-ONLY operation, so campaigns whose text is clearly in another language
(Spanish/Portuguese/French are the common ones on Whop, plus non-Latin scripts) should sink
in the ranking. This module answers ONE cheap question from already-scraped text — "is this
clearly not English?" — with NO network and NO model (the hard Groq-free constraint).

Method (deliberately simple + auditable):
  1. Non-Latin scripts (Cyrillic/CJK/Arabic/…): if letters are mostly non-Latin, it's plainly
     not English.
  2. Otherwise a STOPWORD-RATIO check: what fraction of tokens are English function words vs
     Spanish/Portuguese/French ones. English prose is ~30-50% stopwords, so a body of text
     that scores high on es/pt/fr stopwords AND beats its English fraction is non-English.

FAIL OPEN: short or ambiguous text (few tokens, or no clear winner) returns `nonenglish=False`
with language "unknown"/"en" — we assume English rather than wrongly deranking. Only a CLEAR
non-English signal penalizes. Pure function, no Playwright/network — testable via
`import language`.
"""
import re
import unicodedata

# --- stopword sets (function words — the cheapest language fingerprint) ---------
# English. Kept broad; these dominate real English rules/brief text.
_EN = {
    "the", "be", "to", "of", "and", "a", "in", "that", "have", "it", "for", "not",
    "on", "with", "he", "as", "you", "do", "at", "this", "but", "his", "by", "from",
    "they", "we", "say", "her", "she", "or", "an", "will", "my", "one", "all", "would",
    "there", "their", "what", "so", "up", "out", "if", "about", "who", "get", "which",
    "go", "me", "when", "make", "can", "like", "no", "just", "him", "know", "take",
    "into", "your", "some", "could", "them", "than", "then", "now", "only", "its",
    "also", "back", "after", "use", "two", "how", "our", "work", "first", "well", "way",
    "even", "want", "because", "any", "these", "give", "day", "most", "us", "is", "are",
    "was", "were", "been", "has", "had", "must", "should", "every", "add", "clip", "clips",
    "video", "views", "content", "caption", "post", "should", "each", "per", "here",
}
# Spanish.
_ES = {
    "el", "la", "los", "las", "de", "del", "y", "o", "que", "en", "un", "una", "unos",
    "unas", "por", "con", "para", "no", "se", "su", "sus", "al", "lo", "como", "mas",
    "más", "pero", "le", "ya", "este", "esta", "esto", "estos", "estas", "muy", "sin",
    "sobre", "también", "tambien", "hasta", "donde", "quien", "desde", "todo", "todos",
    "toda", "todas", "durante", "debe", "deben", "ser", "cada", "menos", "otros", "otras",
    "contenido", "siempre", "únicamente", "unicamente", "vídeo", "video", "vídeos",
    "videos", "publicación", "publicacion", "creador", "canal", "vistas", "segundos",
    "mínimo", "minimo", "original", "página", "pagina", "etiqueta", "palabras", "está",
    "esta", "son", "está", "haz", "usa",
}
# Portuguese.
_PT = {
    "o", "a", "os", "as", "de", "do", "da", "dos", "das", "e", "ou", "que", "em", "um",
    "uma", "uns", "umas", "por", "com", "para", "não", "nao", "se", "seu", "sua", "seus",
    "suas", "ao", "aos", "como", "mais", "mas", "já", "ja", "este", "esta", "isto", "muito",
    "sem", "sobre", "também", "tambem", "até", "ate", "onde", "quem", "desde", "todo",
    "todos", "toda", "todas", "durante", "deve", "devem", "ser", "cada", "menos", "outros",
    "outras", "conteúdo", "conteudo", "sempre", "vídeo", "video", "vídeos", "publicação",
    "publicacao", "criador", "canal", "visualizações", "visualizacoes", "segundos",
    "página", "pagina", "não", "são", "sao", "faça", "faca", "use",
}
# French.
_FR = {
    "le", "la", "les", "de", "des", "du", "et", "ou", "que", "en", "un", "une", "pour",
    "avec", "pas", "ne", "se", "son", "sa", "ses", "au", "aux", "comme", "plus", "mais",
    "déjà", "deja", "ce", "cette", "ces", "très", "tres", "sans", "sur", "aussi", "où",
    "qui", "depuis", "tout", "tous", "toute", "toutes", "pendant", "doit", "doivent",
    "être", "etre", "chaque", "moins", "autres", "contenu", "toujours", "vidéo", "video",
    "vidéos", "publication", "créateur", "createur", "chaîne", "chaine", "vues", "secondes",
    "page", "dans", "vous", "votre", "est", "sont", "faire", "utilisez",
}
_NONENG = {"es": _ES, "pt": _PT, "fr": _FR}

# A token is a run of letters (Unicode-aware so accents/ñ/ç stay attached).
_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

# --- thresholds (tuned on the live board; see the module test) -----------------
MIN_TOKENS = 8          # below this, text is too short to judge -> fail open (assume English)
MIN_NE_HITS = 3         # need at least this many non-English stopword hits to call it
MIN_NE_FRACTION = 0.12  # ...and they must be >=12% of tokens
NONLATIN_MIN_LETTERS = 12    # min letters before a non-Latin-script verdict
NONLATIN_FRACTION = 0.30     # this fraction of letters non-Latin -> clearly non-English


def _latin_share(text):
    """(letter_count, non_latin_letter_count). 'Latin' = the Basic/Extended Latin blocks that
    cover English + es/pt/fr accents. Anything else (Cyrillic/CJK/Arabic/Greek/…) is 'other'."""
    letters = nonlatin = 0
    for ch in text:
        if not ch.isalpha():
            continue
        letters += 1
        try:
            name = unicodedata.name(ch)
        except ValueError:
            nonlatin += 1
            continue
        if not name.startswith("LATIN"):
            nonlatin += 1
    return letters, nonlatin


def detect_language(*texts):
    """Classify the combined text. Returns a dict:

        {language, nonenglish, confidence, tokens, basis, scores}

    `nonenglish=True` ONLY on a clear signal (drives the composite derank); short/ambiguous
    text fails OPEN (nonenglish=False, language 'unknown'/'en'). Never raises."""
    text = " ".join(t for t in texts if t)
    out = {"language": "unknown", "nonenglish": False, "confidence": None,
           "tokens": 0, "basis": "no text", "scores": {}}
    if not text.strip():
        return out

    # 1) Non-Latin script — mostly Cyrillic/CJK/Arabic/… is plainly not English.
    letters, nonlatin = _latin_share(text)
    if letters >= NONLATIN_MIN_LETTERS and nonlatin / letters >= NONLATIN_FRACTION:
        out.update(language="non-latin", nonenglish=True, confidence="high",
                   basis=f"{nonlatin}/{letters} letters are non-Latin script")
        return out

    # 2) Stopword ratio (Latin scripts: en vs es/pt/fr).
    tokens = [t.lower() for t in _TOKEN_RE.findall(text)]
    n = len(tokens)
    out["tokens"] = n
    if n < MIN_TOKENS:
        out["basis"] = f"only {n} tokens — too short to judge (assume English)"
        return out

    en_hits = sum(1 for t in tokens if t in _EN)
    ne_counts = {lang: sum(1 for t in tokens if t in words) for lang, words in _NONENG.items()}
    best_lang = max(ne_counts, key=ne_counts.get)
    ne_hits = ne_counts[best_lang]
    en_frac = en_hits / n
    ne_frac = ne_hits / n
    out["scores"] = {"en": round(en_frac, 3),
                     **{k: round(v / n, 3) for k, v in ne_counts.items()}}

    # Clear non-English: enough absolute + relative non-English stopwords, AND it out-scores
    # the English fraction. Otherwise fail OPEN (English/unknown, no penalty).
    if ne_hits >= MIN_NE_HITS and ne_frac >= MIN_NE_FRACTION and ne_frac > en_frac:
        conf = "high" if (ne_frac >= 0.18 and ne_frac >= en_frac * 1.5) else "low"
        out.update(language=best_lang, nonenglish=True, confidence=conf,
                   basis=(f"{best_lang} stopwords {ne_frac:.0%} ({ne_hits}/{n}) "
                          f"> English {en_frac:.0%} ({en_hits}/{n})"))
        return out

    out.update(language="en" if en_hits else "unknown", nonenglish=False,
               confidence="high" if en_frac >= 0.15 else "low",
               basis=(f"English {en_frac:.0%} ({en_hits}/{n}) vs best non-English "
                      f"{best_lang} {ne_frac:.0%} ({ne_hits}/{n}) — not clearly non-English"))
    return out


def language_text(campaign):
    """Cheap text bundle for detection: name + EVERY captured rules body (bullets, on-modal
    requirements, the substantive modal rules section, and any fetched Notion/Google-Doc text) +
    creator handle/description (all already scraped — no new work). Reading every rules body (not
    just rules_text + modal_requirements_text) is FIX 4: a Spanish campaign whose rules landed in
    modal_rules_text or a fetched doc is still detected instead of slipping through as English."""
    return " ".join(t for _, t in language_segments(campaign) if t)


def language_segments(campaign):
    """Labeled text segments for PER-FIELD language detection (FIX 1). Returns
    [(label, text), ...] over every captured text field — title/name, creator name/handle,
    audience/description, and each rules body. Splitting into segments (instead of one blob) is
    what catches a non-English TITLE or DESCRIPTION when the rules doc happens to be English:
    the Yomi Denzel trap was a short French description ("gagne 1$ pour chaque 1000 vues")
    drowned out to invisibility in a combined blob of long English rules. All fields are already
    scraped — no new work."""
    src = campaign.get("source") or {}
    handles = " ".join(h.get("handle") or h.get("url") or ""
                       for h in (src.get("handles") or []) if isinstance(h, dict))
    modal = " ".join(str(x) for x in (campaign.get("modal_requirements_text"),
                                      campaign.get("modal_rules_text")) if x)
    docs = " ".join(str(x) for x in (campaign.get("notion_rules_text"),
                                     campaign.get("rules_doc_text")) if x)
    return [
        ("title", str(campaign.get("name") or "")),
        ("creator", " ".join(x for x in (str(src.get("name") or ""), handles) if x.strip())),
        ("description", str(src.get("description") or "")),
        ("rules", str(campaign.get("rules_text") or "")),
        ("modal", modal),
        ("doc", docs),
    ]


def detect_language_fields(segments):
    """Detect language across labeled segments (FIX 1), firing `nonenglish` when the COMBINED
    text OR ANY substantive individual segment reads clearly non-English. A short non-English
    title/description is diluted below the stopword thresholds in a combined blob of long English
    rules, so per-segment detection is what surfaces it — if the DESCRIPTION or TITLE is clearly
    non-English we derank even when the rules doc is English.

    Fail-open is preserved exactly: `detect_language` already returns nonenglish=False for short/
    ambiguous text, so a per-segment verdict only fires on a genuinely clear signal. Returns the
    same dict shape as `detect_language`, plus `trigger` = which segment (or 'combined') fired.
    Never raises."""
    seg_list = [(lbl, t) for lbl, t in (segments or []) if t and t.strip()]
    combined = detect_language(*[t for _, t in seg_list])
    if combined.get("nonenglish"):
        combined["trigger"] = "combined"
        combined["basis"] = "combined text — " + combined.get("basis", "")
        return combined
    # Per-field: any substantive segment that clearly reads non-English fires the derank even
    # though the combined blob (dominated by English rules) did not.
    hits = []
    for lbl, t in seg_list:
        r = detect_language(t)
        if r.get("nonenglish"):
            r["trigger"] = lbl
            hits.append((lbl, r))
    if hits:
        # Prefer the strongest signal: high-confidence first, then the largest non-English
        # stopword fraction for its detected language.
        def strength(item):
            r = item[1]
            return (r.get("confidence") == "high",
                    (r.get("scores") or {}).get(r.get("language"), 0.0))
        lbl, r = max(hits, key=strength)
        r["basis"] = f"{lbl} field — " + r.get("basis", "")
        return r
    combined["trigger"] = None
    return combined
