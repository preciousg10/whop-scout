"""Groq-based campaign categorizer — replaces the weak name-only keyword tagger.

The old `extract.classify_category` scored campaign NAMES against keyword lists and dumped
~182 campaigns into "other" (missing obvious ones like "Jesser x ClipFarm" = sports). This
module instead lets a Groq model read EVERY signal a campaign actually exposes and judge the
category — because the data is uneven (of ~676 campaigns: 100% have a name, ~54% have
modal_requirements_text, ~47% have footage/resource links), so no single field can be assumed.

Signals fed per campaign (whatever exists):
  - name                       (always present; often states the category outright)
  - modal_requirements_text    (when present — the strongest signal; truncated)
  - footage/resource TITLES    (when present — the YouTube/VOD title strings intake already
                                captured, e.g. "MrBeast Plays Soccer" ⇒ sports; first few only,
                                TITLES AS TEXT — we never download or read video content)
  - creator / source name      (mainly a tiebreaker when the name is just a person)

Output per campaign: primary_category (ONE from the FIXED set), secondary_categories
(optional, same set), category_confidence (high/low). If signals are insufficient or
conflicting the model is instructed to return "other"/low rather than GUESS — a wrong
confident label is worse than an honest "other".

Guards: (1) fixed category list — the model may only choose from it (anything else is coerced
to "other"); (2) uncertain ⇒ "other"/low, never fabricated; (3) Groq calls are BATCHED (many
campaigns per request) to respect free-tier limits; (4) results are CACHED keyed to a hash of
each campaign's CONTENT (name + modal + titles + creator) so re-runs skip unchanged campaigns —
`--recategorize` clears the cache for a fresh pass; (5) a 20-campaign sample is printed for
eyeballing; (6) category RANKING uses primary_category only (see scoring.rank_categories).

Degrades safely: with no GROQ_API_KEY / no `groq` package / offline, it falls back to the
keyword classifier (marked as such) so a run never crashes. Pure helpers (signals/hash/prompt/
parse/validate) are network-free and unit-testable.
"""
import hashlib
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import extract

# --- the FIXED category set (guard #1) — the model may choose ONLY from these -----
CATEGORIES = [
    "sports", "streamer_irl", "podcast_talking", "gaming", "music",
    "brand_product", "meme", "news", "movie_tv", "other",
]
_CATEGORY_SET = set(CATEGORIES)

# Categorization needs a LARGE-context model so a 20-campaign batch doesn't 413 ("request too
# large" — what killed the old 8B-instant). The original 70B pick, llama-3.3-70b-versatile, has
# since been DECOMMISSIONED by Groq (its models.list no longer offers it — a call 404s with
# "model does not exist or you do not have access to it"), so we default to openai/gpt-oss-120b:
# it's a currently-available 120B model (bigger context than the old 70B, so even safer against
# 413), and it's the model the sibling clipper already runs on these same keys. The daily-TOKEN
# budget is handled by the per-run cap + cache (not by shrinking the model). Override with
# cfg.category_model or $SCOUT_GROQ_MODEL (NOT the clipper's $GROQ_MODEL). A residual 413 on any
# batch is handled adaptively by splitting the batch — see _categorize_chunk.
GROQ_MODEL_DEFAULT = "openai/gpt-oss-120b"
# Recorded in the cache for provenance only. We do NOT discard the cache on a version change —
# re-categorizing all ~450 campaigns every run is what blew the free-tier DAILY token budget.
# Cached categorizations are always kept; only UNCACHED campaigns are sent to Groq. To force a
# fresh pass under a new prompt, use `--recategorize` (which is quota-aware via the per-run cap).
PROMPT_VERSION = 3
_MODAL_CAP = 800        # chars of modal_requirements_text fed to the model (keep prompts small)
_MAX_TITLES = 5         # footage/resource titles per campaign (task: first 3–5)


# =============================================================================
# PURE helpers — no network, unit-testable
# =============================================================================
def _clean(s):
    return " ".join((s or "").split())


def campaign_signals(rec):
    """Extract the categorization signals a record actually exposes. Missing fields degrade to
    empty — we use whatever exists, never assume a field is present."""
    name = _clean(rec.get("name"))
    modal = _clean(rec.get("modal_requirements_text"))[:_MODAL_CAP]
    titles = []
    for s in ((rec.get("footage_intake") or {}).get("sources") or []):
        t = _clean(s.get("title"))
        if t and t.lower() not in ("youtube", "google drive"):
            titles.append(t)
    # Resource-link labels are a weak secondary title source (skip generic doc labels).
    if len(titles) < _MAX_TITLES:
        for rl in (rec.get("resource_links") or []):
            lab = _clean(rl.get("label"))
            if lab and not re.fullmatch(r"(?i)(brief|requirements?|guidelines?|rules?|doc|link|"
                                        r"content|resources?)", lab):
                titles.append(lab)
    titles = titles[:_MAX_TITLES]
    creator = _clean(rec.get("creator") or (rec.get("source") or {}).get("name"))
    return {"name": name, "modal": modal, "titles": titles, "creator": creator}


def content_hash(signals):
    """Stable hash of a campaign's CONTENT (guard #4). Changes iff the signals we feed the
    model change, so unchanged campaigns reuse the cache and edited ones re-categorize."""
    blob = json.dumps({"n": signals.get("name", ""), "m": signals.get("modal", ""),
                       "t": signals.get("titles", []), "c": signals.get("creator", "")},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def validate_result(obj):
    """Coerce a model result to the fixed schema (guard #1 + #2). Unknown/invalid primary →
    'other'; secondaries filtered to the set; confidence in {high,low} else 'low'; an 'other'
    primary is always low-confidence (honest uncertainty, never a confident 'other')."""
    obj = obj if isinstance(obj, dict) else {}
    prim = obj.get("primary") or obj.get("primary_category") or "other"
    prim = prim if prim in _CATEGORY_SET else "other"
    raw_sec = obj.get("secondary") or obj.get("secondary_categories") or []
    if not isinstance(raw_sec, list):
        raw_sec = []
    seen, sec = set(), []
    for s in raw_sec:
        if s in _CATEGORY_SET and s != prim and s != "other" and s not in seen:
            seen.add(s)
            sec.append(s)
    conf = obj.get("confidence") or obj.get("category_confidence")
    conf = conf if conf in ("high", "low") else "low"
    if prim == "other":
        conf = "low"
    return {"primary": prim, "secondary": sec[:3], "confidence": conf}


def keyword_result(rec):
    """Fallback categorization from the legacy keyword tagger, coerced to the fixed schema.
    Always low-confidence (the keyword tagger is exactly what we're replacing)."""
    cats = extract.classify_categories(rec.get("name"), rec.get("rules_text"),
                                       rec.get("platforms")) or ["other"]
    cats = [c for c in cats if c in _CATEGORY_SET] or ["other"]
    return validate_result({"primary": cats[0], "secondary": cats[1:], "confidence": "low"})


def build_batch_prompt(signals_list):
    """(system, user) for one batch. The user payload is a compact JSON list of per-campaign
    signals with an index `i`; the model returns one object per `i`."""
    system = (
        "You categorize short-form CLIP campaigns (creators pay clippers to post viral clips) "
        "into exactly ONE primary category, using only the signals given. Choose primary and "
        "any secondaries ONLY from this fixed list:\n"
        + ", ".join(CATEGORIES) + "\n\n"
        "Definitions: sports=athletes/leagues/matches; streamer_irl=Twitch/Kick/IRL/just-"
        "chatting streamers; podcast_talking=podcasts/interviews/sit-down talk shows; "
        "gaming=gameplay/esports; music=musicians/songs/labels; brand_product=a specific NAMED "
        "company/app/product/brand being advertised (e.g. StockX, eBay, a SaaS app, a token); "
        "meme=meme/comedy/shitpost pages; news=news/politics/commentary on current events; "
        "movie_tv=films/TV shows/trailers/cinematic; other=none fit or unclear.\n\n"
        "Read every signal (name, requirements text, footage TITLES, creator). The name often "
        "states it (e.g. 'Double Coverage Podcast'⇒podcast_talking, 'Warriors of the Wasteland | "
        "Movie'⇒movie_tv); footage titles reveal it (e.g. 'MrBeast Plays Soccer'⇒sports).\n\n"
        "brand_product is NOT a catch-all — use it ONLY when a specific named brand/product/"
        "company/app is the subject. Generic content campaigns with NO identifiable brand — e.g. "
        "'UGC', 'captions', 'talking-head', 'German Captions', 'non-English content' — are NOT "
        "brand_product; if you cannot identify the actual subject, use 'other' with 'low'.\n\n"
        "If the name is (or contains) a PERSON, categorize by WHO THAT PERSON IS using your "
        "knowledge of public figures: a musician/rapper/singer⇒music (e.g. 'Lainey Wilson' is a "
        "country singer⇒music), an athlete⇒sports, a Twitch/Kick/IRL streamer⇒streamer_irl, a "
        "podcaster/talk-show host⇒podcast_talking, an actor/filmmaker⇒movie_tv. Use the creator "
        "field the same way. Only if you don't recognize the person AND no other signal helps, "
        "use 'other'/low.\n\n"
        "CRITICAL: if the signals are insufficient or conflicting, return primary_category="
        "\"other\" with confidence=\"low\" — never default to brand_product when unsure. Do NOT "
        "guess — a wrong confident label is worse than an honest \"other\". Never invent a "
        "category outside the list.\n\n"
        "Return ONLY a JSON object: {\"results\":[{\"i\":<index>,\"primary\":<category>,"
        "\"secondary\":[<categories>],\"confidence\":\"high\"|\"low\"}, ...]} — one object per "
        "input campaign, same indices, no prose."
    )
    items = []
    for i, s in enumerate(signals_list):
        item = {"i": i, "name": s.get("name", "")}
        if s.get("modal"):
            item["requirements"] = s["modal"]
        if s.get("titles"):
            item["footage_titles"] = s["titles"]
        if s.get("creator"):
            item["creator"] = s["creator"]
        items.append(item)
    user = "Categorize these campaigns:\n" + json.dumps(items, ensure_ascii=False)
    return system, user


def _extract_json_object(raw):
    """Best-effort parse of a JSON object from a model response (handles fences/prose)."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        pass
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def parse_batch_response(raw, n):
    """Map a batch response back to `n` validated results, aligned by the object's `i` index.
    Any missing/garbled entry defaults to a safe 'other'/low — never a crash, never a guess."""
    default = {"primary": "other", "secondary": [], "confidence": "low"}
    out = [dict(default) for _ in range(n)]
    obj = _extract_json_object(raw)
    results = None
    if isinstance(obj, dict):
        results = obj.get("results")
    elif isinstance(obj, list):
        results = obj
    if not isinstance(results, list):
        return out
    for k, r in enumerate(results):
        if not isinstance(r, dict):
            continue
        idx = r.get("i")
        if not isinstance(idx, int) or not (0 <= idx < n):
            idx = k if k < n else None
        if idx is None:
            continue
        out[idx] = validate_result(r)
    return out


# =============================================================================
# Groq client + batching (network) — mirrors the sibling clipper's groq_chat
# =============================================================================
def groq_keys():
    """Every Groq API key set in the environment, in rotation order. The keys are the NUMBERED
    GROQ_API_KEY_1, GROQ_API_KEY_2, … (the clipper's convention — the shared free-tier keys
    rotated on rate-limit), plus a bare GROQ_API_KEY if one is also set. Deduped, order
    preserved. Empty list ⇒ no key configured. This is why the categorizer used to think Groq
    was 'unavailable': it read ONLY the bare GROQ_API_KEY, which isn't set — only the numbered
    ones are."""
    keys, seen = [], set()
    # numbered keys, ascending (GROQ_API_KEY_1..N); scan a generous range then any stragglers
    numbered = []
    for name, val in os.environ.items():
        m = re.fullmatch(r"GROQ_API_KEY_(\d+)", name)
        if m and val and val.strip():
            numbered.append((int(m.group(1)), val.strip()))
    for _n, val in sorted(numbered, key=lambda kv: kv[0]):
        if val not in seen:
            seen.add(val)
            keys.append(val)
    bare = (os.environ.get("GROQ_API_KEY") or "").strip()
    if bare and bare not in seen:
        keys.append(bare)
    return keys


def groq_available():
    if os.environ.get("SCOUT_OFFLINE") == "1" or not groq_keys():
        return False
    try:
        import groq  # noqa: F401
    except Exception:
        return False
    return True


def _unavailable_reason():
    """A precise reason Groq is unavailable — distinguishes the three causes the old
    'no key/package/offline' message lumped together, so a run tells you WHICH to fix."""
    if os.environ.get("SCOUT_OFFLINE") == "1":
        return "SCOUT_OFFLINE=1"
    if not groq_keys():
        return "no API key (set GROQ_API_KEY_1..N or GROQ_API_KEY)"
    try:
        import groq  # noqa: F401
    except Exception:
        return "`groq` package not installed in this venv (pip install groq)"
    return "client init failed"


class _GroqPool:
    """A rotating pool of Groq clients over the configured keys. One exhausted key must not sink
    the run, so on a per-minute limit we rotate to the next key (cheaper than waiting) and on a
    DAILY cap we retire that key for the rest of the run and rotate. Only when EVERY key is
    retired do we treat the whole daily budget as gone. `current()` always points at a LIVE
    (non-retired) key; clients are created lazily and reused. Never raises on construction."""
    def __init__(self, keys):
        self._keys = list(keys)
        self._live = list(range(len(self._keys)))   # original indices still usable this run
        self._clients = {}                           # idx -> Groq client (lazy)
        self._pos = 0                                # position within _live

    def _client_for(self, idx):
        c = self._clients.get(idx)
        if c is None:
            from groq import Groq
            c = Groq(api_key=self._keys[idx])
            self._clients[idx] = c
        return c

    def key_count(self):
        return len(self._keys)

    def live_count(self):
        return len(self._live)

    def current(self):
        """(original_index, client) for the current live key, or (None, None) if all retired."""
        if not self._live:
            return None, None
        self._pos %= len(self._live)
        idx = self._live[self._pos]
        return idx, self._client_for(idx)

    def rotate(self):
        if self._live:
            self._pos = (self._pos + 1) % len(self._live)

    def retire(self, idx):
        """Drop a key that hit its DAILY cap; keep the cursor on a still-live key."""
        if idx in self._live:
            self._live.remove(idx)
        if self._live:
            self._pos %= len(self._live)


def _groq_client():
    """A rotating pool over ALL configured keys, or None if Groq is unavailable."""
    if not groq_available():
        return None
    try:
        return _GroqPool(groq_keys())
    except Exception:
        return None


class GroqDailyLimit(Exception):
    """Raised when Groq's DAILY token/request budget is exhausted (TPD/RPD, or a 'try again'
    hint longer than a per-minute window). Retrying is pointless — the day's quota is gone — so
    this propagates up and stops Groq calls for the rest of the run (remainder → keyword)."""


class RequestTooLarge(Exception):
    """Raised on a 413 'request too large' — the batch exceeds the model's context/limit.
    Retrying identically can't help; the caller SPLITS the batch and retries the halves."""


def _is_request_too_large(msg):
    """A 413 / oversized-request error (distinct from a rate limit — even when Groq phrases it
    against TPM). Checked BEFORE rate-limit classification so it isn't mistaken for a wait."""
    low = msg.lower()
    return ("413" in msg or "request too large" in low or "request_too_large" in low
            or "reduce the length" in low or "maximum context length" in low
            or "context_length_exceeded" in low or "too many tokens" in low)


# A required wait longer than this is a daily cap, not a per-minute one — not worth blocking
# a run for. Per-minute waits are seconds; daily waits are minutes/hours.
_MAX_MINUTE_WAIT_S = 90.0


def _parse_wait_seconds(msg):
    """Seconds from a Groq 'try again in 5m30s' / '8.5s' / '2h34m' hint, or None."""
    m = re.search(r"try again in ([0-9hms.\s]+)", msg, re.I)
    if not m:
        return None
    total, found = 0.0, False
    for val, unit in re.findall(r"([\d.]+)\s*(h|m|s)", m.group(1), re.I):
        found = True
        total += float(val) * {"h": 3600, "m": 60, "s": 1}[unit.lower()]
    return total if found else None


def _classify_rate_limit(msg):
    """('daily' | 'minute' | 'error', wait_seconds_or_None). 'daily' = TPD/RPD/'per day' or a
    wait longer than a per-minute window (retrying can't help today). 'minute' = a short,
    recoverable per-minute (TPM/RPM) limit. 'error' = a non-rate failure."""
    low = msg.lower()
    is_rate = ("429" in msg or "rate limit" in low or "rate_limit" in low
               or "tpm" in low or "tpd" in low or "rpm" in low or "rpd" in low)
    if not is_rate:
        return "error", None
    wait = _parse_wait_seconds(msg)
    is_daily = ("per day" in low or "tpd" in low or "rpd" in low or "daily" in low
                or (wait is not None and wait > _MAX_MINUTE_WAIT_S))
    return ("daily" if is_daily else "minute"), wait


def _groq_chat(pool, system, user, model, *, retries=3, max_tokens=2048):
    """One chat completion, ROTATING across the pool's keys. `pool` is a `_GroqPool`.
    A per-minute rate limit (TPM/RPM) on a key with siblings → rotate to the next key
    immediately (cheaper than waiting); with only ONE live key left, honor Groq's short
    'try again in Xs' hint a FEW times. A DAILY cap (TPD/RPD, or a very long wait) RETIRES that
    key for the run and rotates; only when EVERY key is retired do we raise GroqDailyLimit (the
    whole daily budget is gone — the caller then stops calling Groq). A 413 raises
    RequestTooLarge (the caller splits the batch). Returns the message string, or None on a
    transient/other failure across the live keys (that batch defers to keyword)."""
    minute_waits = 0
    # Bound total tries: one pass over the live keys, plus a few single-key minute honors.
    max_tries = pool.live_count() + retries + 1
    for _ in range(max_tries):
        idx, client = pool.current()
        if client is None:                          # no live keys remain
            break
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                temperature=0.1,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            return resp.choices[0].message.content or ""
        except Exception as e:
            msg = str(e)
            if _is_request_too_large(msg):          # 413 — split the batch, don't wait/retry
                raise RequestTooLarge(msg.splitlines()[0][:200])
            kind, wait = _classify_rate_limit(msg)
            if kind == "daily":
                print(f"    Groq key #{idx + 1} daily cap reached — retiring it, rotating.")
                pool.retire(idx)
                continue
            if kind == "minute":
                if pool.live_count() > 1:           # rotate instead of waiting
                    print(f"    Groq key #{idx + 1} per-minute limit — rotating to next key.")
                    pool.rotate()
                    continue
                if minute_waits < retries:          # single live key: honor a short wait
                    w = (wait + 0.5) if wait else min(2.0 ** minute_waits, 20.0) + random.uniform(0, 1.0)
                    print(f"    Groq per-minute limit (single live key) — waiting {w:.1f}s "
                          f"(retry {minute_waits + 1}/{retries})…")
                    time.sleep(w)
                    minute_waits += 1
                    continue
                return None
            # non-rate error (bad key, transient network, etc.) — try the next key
            print(f"    Groq key #{idx + 1} call failed ({model}): {msg.splitlines()[0][:140]}")
            pool.rotate()
            continue
    if pool.live_count() == 0:                       # every key hit its daily cap this run
        raise GroqDailyLimit("all Groq keys reached their daily cap")
    return None


def _categorize_batch(client, signals_list, model):
    """Categorize one batch via Groq. Returns validated results aligned to `signals_list`,
    or None if the call failed (caller decides how to fall back)."""
    system, user = build_batch_prompt(signals_list)
    raw = _groq_chat(client, system, user, model)
    if raw is None:
        return None
    return parse_batch_response(raw, len(signals_list))


def _categorize_batch_retry(client, signals_list, model, *, batch_retries=1, cooldown=8.0):
    """`_categorize_batch` plus a FEW whole-batch retries for a transient per-minute failure.
    A GroqDailyLimit is NOT caught here — it propagates so the run stops calling Groq (no point
    retrying a dead daily quota). Kept modest (default 1 retry): the budget is daily, so
    hammering wastes the run's time, and unfinished campaigns simply defer to the next run."""
    for attempt in range(batch_retries + 1):
        res = _categorize_batch(client, signals_list, model)   # may raise GroqDailyLimit
        if res is not None:
            return res
        if attempt < batch_retries:
            wait = cooldown * (attempt + 1)
            print(f"    Batch failed — cooling down {wait:.0f}s then retrying "
                  f"({attempt + 1}/{batch_retries})…")
            time.sleep(wait)
    return None


def _categorize_chunk(client, signals_list, model, *, batch_retries=1):
    """Categorize a chunk, returning a PER-ITEM list (len == len(signals_list)) of
    result-dict-or-None. On a 413 'request too large' it SPLITS the chunk in half and retries
    each half (halving until it fits or down to one campaign), so a batch that's momentarily too
    big for the model's context shrinks instead of failing wholesale — the safety net for the
    old 8B 413s and any residual 70B 413. GroqDailyLimit propagates (stops the run); a
    transient/other failure yields None for those items (→ keyword_fallback, deferred)."""
    try:
        res = _categorize_batch_retry(client, signals_list, model, batch_retries=batch_retries)
    except RequestTooLarge as e:
        n = len(signals_list)
        if n <= 1:
            print(f"    413 on a single campaign — deferring to keyword ({e}).")
            return [None]
        mid = n // 2
        print(f"    413 request too large — splitting {n} into {mid}+{n - mid} and retrying.")
        return (_categorize_chunk(client, signals_list[:mid], model, batch_retries=batch_retries)
                + _categorize_chunk(client, signals_list[mid:], model, batch_retries=batch_retries))
    return list(res) if res is not None else [None] * len(signals_list)


# =============================================================================
# Cache
# =============================================================================
def _load_cache(path):
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    # Keep ALL valid cached categorizations regardless of prompt_version — re-categorizing
    # everything on a version bump is exactly what blew the daily budget. Only genuinely
    # uncached campaigns are sent to Groq (see categorize_campaigns); --recategorize is the
    # explicit, quota-capped way to refresh under a new prompt.
    if isinstance(data, dict) and "entries" in data:
        entries = data.get("entries")
        return entries if isinstance(entries, dict) else {}
    return data if isinstance(data, dict) else {}   # legacy bare-dict cache -> reuse


def _write_cache(path, entries, model):
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(),
               "model": model, "prompt_version": PROMPT_VERSION,
               "count": len(entries), "entries": entries}
    try:
        Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                              encoding="utf-8")
    except Exception:
        pass


# =============================================================================
# Apply + orchestrate
# =============================================================================
def _apply(rec, result, source):
    """Write a categorization onto a record. category (primary) drives ranking + style_fit;
    categories keeps [primary]+secondaries for reference (ranking uses primary only)."""
    prim = result["primary"]
    sec = result["secondary"]
    rec["category"] = prim
    rec["category_confidence"] = result["confidence"]
    rec["category_secondaries"] = list(sec)
    rec["categories"] = [prim] + [s for s in sec if s != prim]
    rec["category_source"] = source


def _sample_indices(n, k):
    """Up to k indices spread evenly across 0..n-1 (a representative eyeball sample)."""
    if n <= k:
        return list(range(n))
    step = n / float(k)
    return sorted({min(n - 1, int(i * step)) for i in range(k)})


def _print_sample(active, k=20):
    print(f"\n  --- categorization sample ({min(k, len(active))} of {len(active)}) — "
          f"eyeball before trusting the board ---")
    for i in _sample_indices(len(active), k):
        c = active[i]
        name = (c.get("name") or "(unnamed)")[:44]
        print(f"    {name:<44}  {c.get('category') or 'other':<15}  "
              f"{c.get('category_confidence') or 'low'}  [{c.get('category_source') or '?'}]")
    print("")


def summarize(active):
    """Breakdown for the report: count per category, low-confidence total, and how far 'other'
    shrank vs the legacy keyword tagger on the same set."""
    by_cat, low = {}, 0
    for c in active:
        cat = c.get("category") or "other"
        by_cat[cat] = by_cat.get(cat, 0) + 1
        if (c.get("category_confidence") or "low") == "low":
            low += 1
    keyword_other = sum(1 for c in active
                        if extract.classify_category(c.get("name"), c.get("rules_text"),
                                                     c.get("platforms")) == "other")
    return {
        "total": len(active),
        "by_category": dict(sorted(by_cat.items(), key=lambda kv: (-kv[1], kv[0]))),
        "low_confidence": low,
        "other_now": by_cat.get("other", 0),
        "other_keyword_baseline": keyword_other,
        "sources": {s: sum(1 for c in active if c.get("category_source") == s)
                    for s in ("groq", "cache", "keyword_fallback")},
    }


def categorize_campaigns(records, cfg, *, recategorize=False, client=None, sample_n=20):
    """Categorize every scraped/refreshed campaign with Groq (batched + cached), in place.
    Fills category (primary), category_secondaries, category_confidence, categories,
    category_source. Prints a sample and returns a summary. Never raises."""
    active = [r for r in records if r.get("status") in ("scraped", "refreshed")]
    if not active:
        print("  Categorizer: no active campaigns to categorize.")
        return summarize(active)

    path = getattr(cfg, "category_cache_path", "category_cache.json")
    # Model resolution: cfg override → a SCOUT-SPECIFIC env override → the 70B default. We do NOT
    # inherit the bare $GROQ_MODEL here: that variable is the sibling CLIPPER's model (currently
    # openai/gpt-oss-120b) and letting it leak in would silently run categorization on the wrong
    # model. Categorization needs llama-3.3-70b-versatile (the 8B 413'd on 20-campaign batches);
    # use SCOUT_GROQ_MODEL only if you deliberately want to override scout's categorizer.
    model = (getattr(cfg, "category_model", None)
             or os.environ.get("SCOUT_GROQ_MODEL")
             or GROQ_MODEL_DEFAULT)
    cache = {} if recategorize else _load_cache(path)
    if recategorize:
        print("  Categorizer: --recategorize — clearing cache, fresh Groq pass.")

    todo = []                                   # (rec, signals, hash) needing Groq
    for r in active:
        sig = campaign_signals(r)
        h = content_hash(sig)
        entry = cache.get(h)
        if entry:
            _apply(r, validate_result(entry), source="cache")
        else:
            todo.append((r, sig, h))

    print(f"  Categorizer: {len(active)} active · {len(active) - len(todo)} from cache · "
          f"{len(todo)} to categorize.")

    if todo:
        client = client if client is not None else _groq_client()
        if client is None:
            reason = _unavailable_reason()
            print(f"  Categorizer: Groq unavailable ({reason}) — keyword fallback for "
                  f"{len(todo)} campaign(s).")
            for r, _sig, _h in todo:
                _apply(r, keyword_result(r), source="keyword_fallback")
        else:
            # Per-run cap: only send up to N NEW campaigns to Groq this run so a first big fill
            # can't exceed the free-tier DAILY budget. The rest keep keyword_fallback (NOT
            # cached) and are picked up on the next run — Scout runs every few days, so the
            # board fills in over a couple runs without ever blowing the quota.
            keyc = client.key_count() if hasattr(client, "key_count") else 1
            print(f"  Categorizer: Groq ready — {keyc} key(s) rotating, model {model}.")
            cap = getattr(cfg, "category_max_new_per_run", 120)
            to_groq, over_cap = todo, []
            if cap and cap > 0 and len(todo) > cap:
                to_groq, over_cap = todo[:cap], todo[cap:]
                print(f"  Categorizer: per-run cap {cap} — categorizing {len(to_groq)} new now, "
                      f"deferring {len(over_cap)} to a later run (daily-budget friendly).")
            for r, _sig, _h in over_cap:
                _apply(r, keyword_result(r), source="keyword_fallback")

            bs = max(1, getattr(cfg, "category_batch_size", 20))
            pause = max(0.0, getattr(cfg, "category_batch_pause", 2.0))
            batch_retries = max(0, getattr(cfg, "category_batch_retries", 1))
            chunks = [to_groq[i:i + bs] for i in range(0, len(to_groq), bs)]
            done, transient_fb, daily_hit, daily_msg, stopped_at = 0, 0, False, "", None

            for bi, chunk in enumerate(chunks):
                try:                                    # per-item results (413 → auto-split)
                    item_results = _categorize_chunk(
                        client, [s for _r, s, _h in chunk], model, batch_retries=batch_retries)
                except GroqDailyLimit as e:             # daily quota gone — stop calling Groq
                    daily_hit, daily_msg, stopped_at = True, str(e), bi
                    break
                got = 0
                for j, (r, _sig, h) in enumerate(chunk):
                    res = item_results[j]
                    if res is None:                     # failed / too-large — defer to keyword
                        _apply(r, keyword_result(r), source="keyword_fallback")
                        transient_fb += 1
                    else:
                        cache[h] = res                  # cache ONLY real Groq results
                        _apply(r, res, source="groq")
                        got += 1
                done += len(chunk)
                print(f"    categorized {done}/{len(to_groq)} ({got}/{len(chunk)} groq)")
                if pause and bi < len(chunks) - 1:      # pace to stay under per-minute RPM
                    time.sleep(pause)

            # Everything Groq didn't reach (daily stop) → keyword_fallback, deferred to next run.
            deferred_daily = 0
            if stopped_at is not None:
                for chunk in chunks[stopped_at:]:
                    for r, _sig, _h in chunk:
                        _apply(r, keyword_result(r), source="keyword_fallback")
                        deferred_daily += 1
            if daily_hit:
                print(f"  Groq daily limit likely reached ({daily_msg[:100]}) — "
                      f"{deferred_daily} campaign(s) deferred to next run. NOT retrying (the "
                      f"quota is per-DAY, not per-minute).")
            deferred_total = len(over_cap) + deferred_daily
            if deferred_total or transient_fb:
                print(f"  Categorizer: deferred {deferred_total} (cap {len(over_cap)} + daily "
                      f"{deferred_daily}) + {transient_fb} transient fallback(s) → next run "
                      f"(uncached, will retry; cached ones skip Groq).")

    _write_cache(path, cache, model)
    _print_sample(active, sample_n)
    _print_source_counts(active)
    return summarize(active)


def _print_source_counts(active):
    """Per-source tally (groq/cache/keyword_fallback) + the brand_product count, so a run can be
    eyeballed for fallback rate and brand_product bloat at a glance."""
    src = {}
    for c in active:
        s = c.get("category_source") or "?"
        src[s] = src.get(s, 0) + 1
    total = len(active) or 1
    fb = src.get("keyword_fallback", 0)
    print("  category_source counts: " + " · ".join(
        f"{k}={src[k]}" for k in ("groq", "cache", "keyword_fallback") if k in src)
        + f"  (fallback {100 * fb / total:.0f}%)")
    bp = sum(1 for c in active if c.get("category") == "brand_product")
    oth = sum(1 for c in active if c.get("category") == "other")
    print(f"  brand_product={bp} · other={oth} (of {len(active)} active)")
