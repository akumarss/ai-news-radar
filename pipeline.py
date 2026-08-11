"""Normalise, deduplicate, filter, tag and score raw items.

The interesting problem here is deduplication. A single announcement — SAM 2,
say — arrives as a Meta blog post, a Hacker News story, an arXiv paper and a
Bluesky link. Those are one story with four witnesses, not four stories. We
collapse them into one card and treat the corroboration as a ranking signal,
because something several independent communities picked up is usually the
thing you didn't want to miss.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

log = logging.getLogger("pipeline")

# Query parameters that identify a campaign, not a document.
TRACKING_PREFIXES = ("utm_", "mc_", "pk_", "hsa_", "_hs")
TRACKING_EXACT = {
    "fbclid", "gclid", "igshid", "mkt_tok", "ref", "referrer", "source",
    "cmpid", "ncid", "spm", "share_id", "si", "at_medium", "at_campaign",
    "__twitter_impression", "s", "context",
}

TITLE_STOPWORDS = {
    "a", "an", "and", "the", "for", "with", "of", "to", "in", "on", "at",
    "is", "are", "how", "why", "what", "new", "using", "from", "we", "our",
    "introducing", "announcing", "show", "hn", "ask",
}

# Domains whose links are wrappers rather than destinations. Microsoft's
# safelinks in particular shows up constantly when links are copied out of
# Teams or Outlook, and it points at a redirector, not an article.
WRAPPER_DOMAINS = {
    "statics.teams.cdn.office.net",
    "nam.safelinks.protection.outlook.com",
    "eur.safelinks.protection.outlook.com",
    "protect-eu.mimecast.com",
    "urldefense.com",
    "t.co",
    "lnkd.in",
    "bit.ly",
}


# ---------------------------------------------------------------------------
# URL canonicalisation
# ---------------------------------------------------------------------------

def canonical_url(url: str) -> str:
    """Reduce a URL to a comparable identity for the same document."""
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()

    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    host = host.split(":")[0]

    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k.lower() not in TRACKING_EXACT
        and not k.lower().startswith(TRACKING_PREFIXES)
    ]
    kept.sort()

    path = re.sub(r"/+$", "", parts.path) or "/"
    # Treat AMP variants as the same document.
    path = re.sub(r"/amp$", "", path)

    return urlunsplit(("https", host, path, urlencode(kept), ""))


def is_wrapper(url: str) -> bool:
    try:
        host = (urlsplit(url).netloc or "").lower()
    except ValueError:
        return False
    if host.startswith("www."):
        host = host[4:]
    return host in WRAPPER_DOMAINS


def domain_of(url: str) -> str:
    try:
        host = (urlsplit(url).netloc or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


# ---------------------------------------------------------------------------
# Title similarity
# ---------------------------------------------------------------------------

def title_tokens(title: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    return frozenset(w for w in words if w not in TITLE_STOPWORDS and len(w) > 2)


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


# ---------------------------------------------------------------------------
# Keyword matching
# ---------------------------------------------------------------------------

def compile_terms(terms: list[str]) -> list[tuple[str, re.Pattern]]:
    r"""Word-boundary patterns, so 'RAG' never matches 'storage'.

    Three things this has to get right, each learned from a false result:

    1. Spaces and hyphens inside a term are interchangeable, so "fine-tuning"
       also matches "fine tuning".
    2. The boundary guard is \w, not [\w-]. Using [\w-] meant "LLM" failed to
       match inside "LLM-as-judge", which silently dropped real articles.
    3. A trailing optional plural, so "model" matches "models" and "LLM"
       matches "LLMs". Without it, most plural headlines fell through.
    """
    separator = re.compile(r"(?:\\[ \-]|[ \-])")
    compiled = []
    for term in terms:
        flexible = separator.sub(lambda _: r"[\s\-]+", re.escape(term))
        compiled.append(
            (term, re.compile(rf"(?<!\w){flexible}(?:e?s)?(?!\w)", re.I))
        )
    return compiled


def count_hits(patterns: list[tuple[str, re.Pattern]], text: str) -> list[str]:
    if not text:
        return []
    return [term for term, pattern in patterns if pattern.search(text)]


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def dedupe(items: list[dict], title_threshold: float = 0.62) -> list[dict]:
    """Collapse items describing the same story.

    Pass 1 groups on canonical URL — exact and cheap. Pass 2 catches the cases
    where sources link to different URLs for one announcement (a blog post and
    its arXiv preprint) by comparing title token overlap.
    """
    by_url: dict[str, list[dict]] = {}
    for item in items:
        key = canonical_url(item.get("url", ""))
        if not key:
            continue
        by_url.setdefault(key, []).append(item)

    groups: list[list[dict]] = list(by_url.values())

    merged: list[list[dict]] = []
    token_cache: list[frozenset[str]] = []
    for group in groups:
        tokens = title_tokens(group[0].get("title", ""))
        placed = False
        for idx, existing_tokens in enumerate(token_cache):
            if jaccard(tokens, existing_tokens) >= title_threshold:
                merged[idx].extend(group)
                token_cache[idx] = existing_tokens | tokens
                placed = True
                break
        if not placed:
            merged.append(list(group))
            token_cache.append(tokens)

    out = []
    for group in merged:
        # The highest-weighted source becomes the canonical face of the story;
        # a lab's own announcement outranks someone linking to it.
        group.sort(key=lambda i: (i.get("weight", 1.0), i.get("engagement", 0)), reverse=True)
        primary = dict(group[0])

        seen_names, also_on = set(), []
        for member in group:
            name = member.get("source", "")
            if name and name not in seen_names:
                seen_names.add(name)
                also_on.append(
                    {
                        "source": name,
                        "url": member.get("url", ""),
                        "type": member.get("source_type", ""),
                        "engagement_label": member.get("engagement_label", ""),
                    }
                )

        # Engagement is summed: a story that got 200 points on HN and 300
        # upvotes on Reddit really did travel further than either alone.
        primary["engagement"] = sum(int(m.get("engagement") or 0) for m in group)
        primary["sources"] = also_on
        primary["source_count"] = len(also_on)
        # Prefer the earliest sighting — that's when the story actually broke.
        dated = [m["published"] for m in group if m.get("published")]
        if dated:
            primary["published"] = min(dated)
        out.append(primary)

    log.info("dedupe %d raw -> %d unique", len(items), len(out))
    return out


# ---------------------------------------------------------------------------
# Filter, tag, score
# ---------------------------------------------------------------------------

def process(items: list[dict], config: dict) -> list[dict]:
    scoring = config.get("scoring", {})
    half_life = float(scoring.get("half_life_hours", 36))
    title_bonus = float(scoring.get("title_keyword_bonus", 3.0))
    summary_bonus = float(scoring.get("summary_keyword_bonus", 0.8))
    corroboration_bonus = float(scoring.get("corroboration_bonus", 4.0))
    max_engagement = float(scoring.get("max_engagement_points", 12.0))
    release_bonus = float(scoring.get("release_bonus", 0.0))

    required = compile_terms(config.get("required", []))
    excluded = compile_terms(config.get("exclude", []))
    topic_patterns = {
        topic: compile_terms(terms)
        for topic, terms in (config.get("topics") or {}).items()
    }

    now = datetime.now(timezone.utc)
    retention = int(config.get("output", {}).get("retention_days", 21))
    cutoff = now - timedelta(days=retention)

    results = []
    dropped_irrelevant = dropped_excluded = dropped_wrapper = 0

    for item in items:
        url = item.get("url", "")
        if not url or is_wrapper(url):
            dropped_wrapper += 1
            continue

        published = item.get("published") or now
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        if published < cutoff:
            continue
        # Feeds occasionally carry future dates; clamp so they can't top the list.
        if published > now + timedelta(hours=6):
            published = now

        title = item.get("title", "")
        summary = item.get("summary", "")
        haystack = f"{title} {summary}"

        if count_hits(excluded, haystack):
            dropped_excluded += 1
            continue

        # arXiv is category-filtered upstream and releases come from a curated
        # repo/author list, so both are relevant by construction. Releases in
        # particular could never pass it: "v0.27.0" contains no keyword.
        if item.get("source_type") not in ("paper", "release") and not count_hits(required, haystack):
            dropped_irrelevant += 1
            continue

        topics = sorted(
            topic for topic, patterns in topic_patterns.items()
            if count_hits(patterns, haystack)
        )
        matched_terms = sorted(
            {
                term
                for patterns in topic_patterns.values()
                for term in count_hits(patterns, haystack)
            }
        )

        title_hits = sum(
            len(count_hits(patterns, title)) for patterns in topic_patterns.values()
        )
        summary_hits = sum(
            len(count_hits(patterns, summary)) for patterns in topic_patterns.values()
        )

        age_hours = max(0.0, (now - published).total_seconds() / 3600.0)
        recency = 0.5 ** (age_hours / half_life)

        relevance = min(title_hits, 6) * title_bonus + min(summary_hits, 8) * summary_bonus
        engagement_points = min(
            max_engagement, math.log1p(max(0, item.get("engagement", 0))) * 2.0
        )
        corroboration = (item.get("source_count", 1) - 1) * corroboration_bonus

        # An open-weight launch is what this feed exists to catch, so it gets
        # a flat bump rather than relying on keyword density alone — release
        # posts are often terse ("Qwen3-VL is here") and score badly on it.
        is_release = "releases" in topics or item.get("source_type") == "release"
        raw = (
            relevance + engagement_points + corroboration + 3.0
            + (release_bonus if is_release else 0.0)
        ) * float(item.get("weight", 1.0))
        # Recency dampens rather than dominates: an important paper from
        # three days ago should still outrank a shallow post from an hour ago.
        score = raw * (0.35 + 0.65 * recency)

        results.append(
            {
                "id": hashlib.sha1(
                    canonical_url(url).encode("utf-8")
                ).hexdigest()[:12],
                "title": title,
                "url": url,
                "domain": domain_of(url),
                "summary": summary,
                "source": item.get("source", ""),
                "source_type": item.get("source_type", ""),
                "tier": item.get("tier", "other"),
                "sources": item.get("sources", []),
                "source_count": item.get("source_count", 1),
                "published": published.isoformat(),
                "age_hours": round(age_hours, 1),
                "engagement": int(item.get("engagement", 0)),
                "engagement_label": item.get("engagement_label", ""),
                "topics": topics,
                "terms": matched_terms[:8],
                "score": round(score, 2),
                "is_release": is_release,
            }
        )

    results.sort(key=lambda i: i["score"], reverse=True)
    log.info(
        "process kept %d (dropped %d irrelevant, %d excluded, %d wrapper)",
        len(results), dropped_irrelevant, dropped_excluded, dropped_wrapper,
    )
    return results[: int(config.get("output", {}).get("max_items", 220))]
