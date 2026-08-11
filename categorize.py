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

GROQ_MODEL_DEFAULT = "llama-3.3-70b-versatile"
# Bump whenever the prompt logic changes (build_batch_prompt). A cache written under a
# different version is auto-discarded so old labels (e.g. the pre-fix brand_product bloat)
# don't persist — the fix takes effect on the next normal run, no --recategorize needed.
PROMPT_VERSION = 2
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
def groq_available():
    if os.environ.get("SCOUT_OFFLINE") == "1" or not os.environ.get("GROQ_API_KEY"):
        return False
    try:
        import groq  # noqa: F401
    except Exception:
        return False
    return True


def _groq_client():
    if not groq_available():
        return None
    try:
        from groq import Groq
        return Groq(api_key=os.environ["GROQ_API_KEY"])
    except Exception:
        return None


def _groq_chat(client, system, user, model, *, retries=8, max_tokens=2048):
    """One chat completion with rate-limit backoff. A 429 (RPM/TPM on the free tier) is
    transient: honor Groq's 'try again in Xs' hint exactly when present, else exponential
    backoff with jitter (capped). Returns the message string, or None on a non-transient
    failure / exhausted retries. Mirrors the sibling clipper's groq_chat, but more patient
    (higher retries + honored hints) since we fire many batches back-to-back."""
    for attempt in range(retries + 1):
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
            is_rate = "429" in msg or "rate" in msg.lower() or "tpm" in msg.lower()
            if is_rate and attempt < retries:
                m = re.search(r"try again in ([\d.]+)\s*s", msg)
                if m:
                    wait = float(m.group(1)) + 0.5                 # honor Groq's hint exactly
                else:
                    wait = min(2.0 ** attempt, 30.0) + random.uniform(0, 1.5)
                print(f"    Groq rate limit — waiting {wait:.1f}s "
                      f"(attempt {attempt + 1}/{retries})…")
                time.sleep(wait)
                continue
            print(f"    Groq call failed ({model}): {msg.splitlines()[0][:160]}")
            return None
    return None


def _categorize_batch(client, signals_list, model):
    """Categorize one batch via Groq. Returns validated results aligned to `signals_list`,
    or None if the call failed (caller decides how to fall back)."""
    system, user = build_batch_prompt(signals_list)
    raw = _groq_chat(client, system, user, model)
    if raw is None:
        return None
    return parse_batch_response(raw, len(signals_list))


def _categorize_batch_retry(client, signals_list, model, *, batch_retries=2, cooldown=12.0):
    """`_categorize_batch` plus WHOLE-BATCH retries: if a batch fails even after `_groq_chat`'s
    own per-call backoff (sustained rate-limit), cool down longer and try the whole batch again
    before giving up. This is what drives fallback toward zero — a batch only falls back to the
    keyword tagger after every retry is exhausted."""
    for attempt in range(batch_retries + 1):
        res = _categorize_batch(client, signals_list, model)
        if res is not None:
            return res
        if attempt < batch_retries:
            wait = cooldown * (attempt + 1)
            print(f"    Batch failed — cooling down {wait:.0f}s then retrying whole batch "
                  f"({attempt + 1}/{batch_retries})…")
            time.sleep(wait)
    return None


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
    if isinstance(data, dict) and "entries" in data:
        if data.get("prompt_version") != PROMPT_VERSION:
            print(f"  Categorizer: cache prompt_version {data.get('prompt_version')} != "
                  f"{PROMPT_VERSION} — discarding stale categories, re-categorizing.")
            return {}
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
    model = getattr(cfg, "category_model", None) or os.environ.get("GROQ_MODEL",
                                                                    GROQ_MODEL_DEFAULT)
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
            print("  Categorizer: Groq unavailable (no key/package/offline) — keyword "
                  f"fallback for {len(todo)} campaign(s). Set GROQ_API_KEY + "
                  "`pip install groq` for real categorization.")
            for r, _sig, _h in todo:
                _apply(r, keyword_result(r), source="keyword_fallback")
        else:
            bs = max(1, getattr(cfg, "category_batch_size", 20))
            pause = max(0.0, getattr(cfg, "category_batch_pause", 2.0))
            batch_retries = max(0, getattr(cfg, "category_batch_retries", 2))
            chunks = [todo[i:i + bs] for i in range(0, len(todo), bs)]
            done, fell_back = 0, 0
            for bi, chunk in enumerate(chunks):
                results = _categorize_batch_retry(
                    client, [s for _r, s, _h in chunk], model, batch_retries=batch_retries)
                for j, (r, _sig, h) in enumerate(chunk):
                    if results is None:                 # exhausted retries — keyword fallback
                        _apply(r, keyword_result(r), source="keyword_fallback")
                        fell_back += 1
                    else:
                        res = results[j]
                        cache[h] = res                  # cache ONLY real Groq results
                        _apply(r, res, source="groq")
                done += len(chunk)
                print(f"    categorized {done}/{len(todo)} "
                      f"({'groq' if results is not None else 'FALLBACK'})")
                if pause and bi < len(chunks) - 1:      # pace to stay under RPM (not after last)
                    time.sleep(pause)
            if fell_back:
                print(f"  Categorizer: {fell_back} campaign(s) still fell back to keyword after "
                      f"retries (Groq rate limits). Re-run to pick them up (cached ones skip).")

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
