"""Source fetchers.

Every fetcher returns a list of dicts in one common shape and never raises:
a dead feed or a missing credential degrades to an empty list plus a warning,
so one broken source can't take down the whole run.

Common item shape:
    title, url, summary, source, source_type, tier, weight,
    published (timezone-aware datetime), engagement (int), engagement_label (str)
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests

log = logging.getLogger("sources")

USER_AGENT = "ai-news-radar/1.0 (personal news aggregator; +https://github.com/)"
TIMEOUT = 25


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating a trailing Z."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _from_struct(struct) -> datetime | None:
    """Convert a feedparser time struct to an aware datetime."""
    if not struct:
        return None
    try:
        return datetime.fromtimestamp(time.mktime(struct), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _strip_html(text: str) -> str:
    """Cheap tag stripper. Feed summaries are HTML; we only want a preview."""
    if not text:
        return ""
    out, depth = [], 0
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return " ".join("".join(out).split())


def _item(**kwargs) -> dict:
    base = {
        "title": "",
        "url": "",
        "summary": "",
        "source": "",
        "source_type": "",
        "tier": "other",
        "weight": 1.0,
        "published": None,
        "engagement": 0,
        "engagement_label": "",
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# RSS / Atom — the vendor and lab blogs
# ---------------------------------------------------------------------------

def fetch_rss(feeds: list[dict], max_age_days: int = 21) -> list[dict]:
    items: list[dict] = []
    cutoff = _now() - timedelta(days=max_age_days)

    for feed in feeds:
        name, url = feed.get("name", "?"), feed.get("url", "")
        try:
            resp = requests.get(
                url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}
            )
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except Exception as exc:
            log.warning("rss %s failed: %s", name, exc)
            continue

        if parsed.bozo and not parsed.entries:
            log.warning("rss %s returned no parseable entries", name)
            continue

        kept = 0
        for entry in parsed.entries[:40]:
            link = entry.get("link", "")
            title = _strip_html(entry.get("title", ""))
            if not link or not title:
                continue

            published = _from_struct(
                entry.get("published_parsed") or entry.get("updated_parsed")
            )
            # Undated entries are treated as fresh rather than dropped —
            # a few feeds omit dates entirely and they're worth keeping.
            if published and published < cutoff:
                continue

            summary = _strip_html(
                entry.get("summary", "") or entry.get("description", "")
            )
            items.append(
                _item(
                    title=title,
                    url=link,
                    summary=summary[:400],
                    source=name,
                    source_type="blog",
                    tier=feed.get("tier", "other"),
                    weight=float(feed.get("weight", 1.0)),
                    published=published or _now(),
                )
            )
            kept += 1

        log.info("rss %-20s %3d items", name, kept)

    return items


def check_feeds(feeds: list[dict]) -> list[dict]:
    """Diagnose every configured feed. Vendor blogs rename their RSS paths
    without warning, so this is the maintenance tool: it separates "dead URL"
    from "alive but nothing published recently", which look identical in the
    normal run's item counts.
    """
    report = []
    for feed in feeds:
        name, url = feed.get("name", "?"), feed.get("url", "")
        row = {"name": name, "url": url, "status": "", "entries": 0, "newest": None}
        try:
            resp = requests.get(
                url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}
            )
            if resp.status_code != 200:
                row["status"] = f"HTTP {resp.status_code}"
                report.append(row)
                continue
            parsed = feedparser.parse(resp.content)
            row["entries"] = len(parsed.entries)
            dates = [
                d for d in (
                    _from_struct(
                        e.get("published_parsed") or e.get("updated_parsed")
                    )
                    for e in parsed.entries
                ) if d
            ]
            if dates:
                row["newest"] = max(dates)
            if not parsed.entries:
                row["status"] = "no entries (wrong URL or not a feed?)"
            elif row["newest"] and (_now() - row["newest"]).days > 60:
                row["status"] = f"stale — newest is {(_now() - row['newest']).days}d old"
            else:
                row["status"] = "ok"
        except Exception as exc:
            row["status"] = f"error: {type(exc).__name__}"
        report.append(row)
    return report


# ---------------------------------------------------------------------------
# Hacker News via Algolia — free, no auth, no key
# ---------------------------------------------------------------------------

def fetch_hackernews(cfg: dict) -> list[dict]:
    if not cfg.get("enabled", True):
        return []

    endpoint = "https://hn.algolia.com/api/v1/search_by_date"
    min_points = int(cfg.get("min_points", 25))
    weight = float(cfg.get("weight", 1.4))
    since = int((_now() - timedelta(days=4)).timestamp())
    items: list[dict] = []

    for query in cfg.get("queries", []):
        params = {
            "query": query,
            "tags": "story",
            "hitsPerPage": 40,
            # Quoted queries plus advancedSyntax force exact phrase matching.
            # Without this Algolia's typo tolerance matches "LLM" against
            # "limiting" and the results become unusable.
            "advancedSyntax": "true",
            "numericFilters": f"points>={min_points},created_at_i>{since}",
        }
        try:
            resp = requests.get(
                endpoint, params=params, timeout=TIMEOUT,
                headers={"User-Agent": USER_AGENT},
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", [])
        except Exception as exc:
            log.warning("hn query %s failed: %s", query, exc)
            continue

        for hit in hits:
            title = _strip_html(hit.get("title") or "")
            if not title:
                continue
            object_id = hit.get("objectID", "")
            discussion = f"https://news.ycombinator.com/item?id={object_id}"
            # Prefer the article itself; Ask/Show HN posts have no outbound URL.
            url = hit.get("url") or discussion
            points = int(hit.get("points") or 0)
            comments = int(hit.get("num_comments") or 0)

            published = None
            if hit.get("created_at_i"):
                published = datetime.fromtimestamp(
                    hit["created_at_i"], tz=timezone.utc
                )

            items.append(
                _item(
                    title=title,
                    url=url,
                    summary=_strip_html(hit.get("story_text") or "")[:400],
                    source="Hacker News",
                    source_type="social",
                    tier="community",
                    weight=weight,
                    published=published or _now(),
                    engagement=points,
                    engagement_label=f"{points} points · {comments} comments",
                )
            )

    log.info("hn %d items across %d queries", len(items), len(cfg.get("queries", [])))
    return items


# ---------------------------------------------------------------------------
# arXiv — free, no auth
# ---------------------------------------------------------------------------

def fetch_arxiv(cfg: dict) -> list[dict]:
    if not cfg.get("enabled", True):
        return []

    categories = cfg.get("categories", ["cs.CL"])
    search = "+OR+".join(f"cat:{c}" for c in categories)
    url = (
        "http://export.arxiv.org/api/query"
        f"?search_query={search}"
        "&sortBy=submittedDate&sortOrder=descending"
        f"&max_results={int(cfg.get('max_results', 60))}"
    )

    try:
        resp = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception as exc:
        log.warning("arxiv failed: %s", exc)
        return []

    items = []
    for entry in parsed.entries:
        title = _strip_html(entry.get("title", ""))
        link = entry.get("link", "")
        if not title or not link:
            continue
        authors = ", ".join(
            a.get("name", "") for a in (entry.get("authors") or [])[:3]
        )
        items.append(
            _item(
                title=title,
                url=link,
                summary=_strip_html(entry.get("summary", ""))[:400],
                source="arXiv",
                source_type="paper",
                tier="academic",
                weight=float(cfg.get("weight", 1.0)),
                published=_from_struct(entry.get("published_parsed")) or _now(),
                engagement_label=authors,
            )
        )

    log.info("arxiv %d items", len(items))
    return items


# ---------------------------------------------------------------------------
# Reddit — free for non-commercial use, but needs an OAuth app
# ---------------------------------------------------------------------------

def _reddit_token() -> str | None:
    """Application-only OAuth. Returns None if credentials aren't configured."""
    client_id = os.environ.get("REDDIT_CLIENT_ID", "").strip()
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None

    username = os.environ.get("REDDIT_USERNAME", "").strip()
    password = os.environ.get("REDDIT_PASSWORD", "").strip()
    if username and password:
        data = {"grant_type": "password", "username": username, "password": password}
    else:
        data = {"grant_type": "client_credentials"}

    try:
        resp = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            data=data,
            auth=(client_id, client_secret),
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("access_token")
    except Exception as exc:
        log.warning("reddit auth failed: %s", exc)
        return None


def fetch_reddit(cfg: dict) -> list[dict]:
    if not cfg.get("enabled", True):
        return []

    token = _reddit_token()
    if not token:
        log.info("reddit skipped — no credentials configured")
        return []

    headers = {"Authorization": f"bearer {token}", "User-Agent": USER_AGENT}
    listing = cfg.get("listing", "top")
    timeframe = cfg.get("timeframe", "day")
    limit = int(cfg.get("limit", 25))
    min_score = int(cfg.get("min_score", 40))
    weight = float(cfg.get("weight", 1.2))
    items: list[dict] = []

    for sub in cfg.get("subreddits", []):
        url = f"https://oauth.reddit.com/r/{sub}/{listing}"
        try:
            resp = requests.get(
                url,
                params={"t": timeframe, "limit": limit},
                headers=headers,
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            children = resp.json().get("data", {}).get("children", [])
        except Exception as exc:
            log.warning("reddit r/%s failed: %s", sub, exc)
            continue

        for child in children:
            post = child.get("data", {})
            score = int(post.get("score") or 0)
            if score < min_score or post.get("stickied"):
                continue

            permalink = "https://reddit.com" + post.get("permalink", "")
            # Link posts point outward; self posts point at the discussion.
            url_out = permalink if post.get("is_self") else (post.get("url") or permalink)

            items.append(
                _item(
                    title=_strip_html(post.get("title", "")),
                    url=url_out,
                    summary=_strip_html(post.get("selftext", ""))[:400],
                    source=f"r/{sub}",
                    source_type="social",
                    tier="community",
                    weight=weight,
                    published=datetime.fromtimestamp(
                        post.get("created_utc", 0), tz=timezone.utc
                    ),
                    engagement=score,
                    engagement_label=(
                        f"{score} upvotes · {post.get('num_comments', 0)} comments"
                    ),
                )
            )
        time.sleep(0.7)  # stay well inside the 100 QPM ceiling

    log.info("reddit %d items", len(items))
    return items


# ---------------------------------------------------------------------------
# Bluesky — free and unlimited on public data; search needs a session
# ---------------------------------------------------------------------------

def _bluesky_token() -> str | None:
    handle = os.environ.get("BLUESKY_HANDLE", "").strip()
    app_password = os.environ.get("BLUESKY_APP_PASSWORD", "").strip()
    if not handle or not app_password:
        return None
    try:
        resp = requests.post(
            "https://bsky.social/xrpc/com.atproto.server.createSession",
            json={"identifier": handle, "password": app_password},
            timeout=TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        return resp.json().get("accessJwt")
    except Exception as exc:
        log.warning("bluesky auth failed: %s", exc)
        return None


def fetch_bluesky(cfg: dict) -> list[dict]:
    if not cfg.get("enabled", True):
        return []

    token = _bluesky_token()
    if not token:
        log.info("bluesky skipped — no credentials configured")
        return []

    headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
    min_likes = int(cfg.get("min_likes", 25))
    weight = float(cfg.get("weight", 0.9))
    items: list[dict] = []

    # Curated handles are searched as `from:` queries and held to a much
    # lower like threshold: the account is the quality filter, so a link from
    # someone who ships models is worth more than 25 likes on a hot take.
    handle_min_likes = int(cfg.get("handle_min_likes", 3))
    plan = [(q, min_likes) for q in cfg.get("queries", [])]
    plan += [(f"from:{h}", handle_min_likes) for h in cfg.get("handles", [])]

    for query, threshold in plan:
        try:
            resp = requests.get(
                "https://bsky.social/xrpc/app.bsky.feed.searchPosts",
                params={
                    "q": query,
                    "limit": int(cfg.get("limit", 40)),
                    "sort": "top",
                },
                headers=headers,
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            posts = resp.json().get("posts", [])
        except Exception as exc:
            log.warning("bluesky query %s failed: %s", query, exc)
            continue

        for post in posts:
            likes = int(post.get("likeCount") or 0)
            if likes < threshold:
                continue

            record = post.get("record", {}) or {}
            text = _strip_html(record.get("text", ""))
            if not text:
                continue

            # A post is only interesting to us if it carries a link out.
            external = ""
            embed = post.get("embed") or {}
            if embed.get("external", {}).get("uri"):
                external = embed["external"]["uri"]
            else:
                for facet in record.get("facets", []) or []:
                    for feature in facet.get("features", []) or []:
                        if feature.get("uri"):
                            external = feature["uri"]
                            break
                    if external:
                        break
            if not external:
                continue

            handle = (post.get("author") or {}).get("handle", "bluesky")
            rkey = (post.get("uri") or "").rsplit("/", 1)[-1]
            published = None
            created = record.get("createdAt", "")
            if created:
                try:
                    published = datetime.fromisoformat(
                        created.replace("Z", "+00:00")
                    )
                except ValueError:
                    published = None

            items.append(
                _item(
                    title=text[:180],
                    url=external,
                    summary=f"via @{handle} — https://bsky.app/profile/{handle}/post/{rkey}",
                    source="Bluesky",
                    source_type="social",
                    tier="community",
                    weight=weight,
                    published=published or _now(),
                    engagement=likes,
                    engagement_label=f"{likes} likes · @{handle}",
                )
            )

    log.info("bluesky %d items", len(items))
    return items


# ---------------------------------------------------------------------------
# Hugging Face model releases — free, no auth
# ---------------------------------------------------------------------------

def fetch_hf_models(cfg: dict) -> list[dict]:
    """New model repos from the labs that ship open weights.

    DeepSeek, Moonshot (Kimi), MiniMax, Z.ai and Alibaba publish no usable
    RSS feed, but all of them push weights to the Hub the moment a model is
    announced, so the Hub API is the only reliable release signal we have for
    them. It doubles as the earliest signal for Meta, Mistral, Qwen and
    Google, whose blog posts often trail the upload by hours or days.
    """
    if not cfg.get("enabled", True):
        return []

    cutoff = _now() - timedelta(days=int(cfg.get("max_age_days", 90)))
    min_likes = int(cfg.get("min_likes", 5))
    weight = float(cfg.get("weight", 2.0))
    limit = int(cfg.get("limit", 30))
    items: list[dict] = []

    for author in cfg.get("authors", []):
        try:
            resp = requests.get(
                "https://huggingface.co/api/models",
                params={
                    "author": author,
                    "sort": "createdAt",
                    "direction": -1,
                    "limit": limit,
                },
                timeout=TIMEOUT,
                headers={"User-Agent": USER_AGENT},
            )
            resp.raise_for_status()
            models = resp.json()
        except Exception as exc:
            log.warning("hf models %s failed: %s", author, exc)
            continue

        for model in models:
            repo = model.get("id") or ""
            created = _parse_iso(model.get("createdAt"))
            if not repo or not created or created < cutoff:
                continue

            likes = int(model.get("likes") or 0)
            downloads = int(model.get("downloads") or 0)
            if likes < min_likes:
                continue

            name = repo.split("/")[-1]
            label = f"{likes:,} likes"
            if downloads:
                label += f" · {downloads:,} downloads"

            items.append(
                _item(
                    # "model weights" keeps the item past the `required`
                    # relevance filter, which repo names alone would fail.
                    title=f"{name} — open model weights released by {author}",
                    url=f"https://huggingface.co/{repo}",
                    summary=" ".join(
                        str(t) for t in (model.get("tags") or [])
                        if isinstance(t, str) and not t.startswith(("license:", "region:"))
                    )[:300],
                    source=f"Hugging Face · {author}",
                    source_type="release",
                    tier="lab",
                    weight=weight,
                    published=created,
                    engagement=likes,
                    engagement_label=label,
                )
            )

    log.info("hf models %d items across %d authors", len(items), len(cfg.get("authors", [])))
    return items


# ---------------------------------------------------------------------------
# GitHub releases — free, no auth, plain Atom
# ---------------------------------------------------------------------------

def _release_title(label: str, raw: str, blurb: str) -> str:
    """Turn a GitHub release title into something readable in a feed.

    Release names are wildly inconsistent: bare versions ("v0.27.0"), package
    pins ("langchain-openai==1.4.3"), and full headlines with commit noise
    ("v0.27.0: [Docs] Fix two docs build warnings (#51014)"). Anything that
    still looks like a version gets "released" appended; a real headline is
    kept as-is, because "DeepEval Flaky tests? released" reads like a bug.
    """
    head = raw.split(":", 1)[0].strip()
    is_version = bool(re.fullmatch(r"v?\d[\w.\-+]*", head)) or "==" in head
    if is_version:
        title = f"{label} {head} released"
    else:
        title = f"{label}: {raw[:90].strip()}"
    return title + (f" — {blurb}" if blurb else "")


def fetch_github_releases(cfg: dict) -> list[dict]:
    """Library releases from the repos this feed cares about.

    LangChain, LangGraph, Langfuse and LlamaIndex either publish no working
    blog feed or an empty one, but every project tags releases on GitHub and
    every repo exposes `releases.atom` without a key. Release titles are bare
    version strings, so the repo label is prepended to make them readable —
    and `source_type: release` exempts them from the relevance filter, which
    a title like "v0.27.0" could never pass.
    """
    if not cfg.get("enabled", True):
        return []

    cutoff = _now() - timedelta(days=int(cfg.get("max_age_days", 90)))
    per_repo = int(cfg.get("per_repo", 5))
    weight = float(cfg.get("weight", 1.2))
    items: list[dict] = []

    for repo in cfg.get("repos", []):
        path = repo.get("repo") if isinstance(repo, dict) else repo
        if not path:
            continue
        label = repo.get("label", path.split("/")[-1]) if isinstance(repo, dict) else path.split("/")[-1]
        blurb = repo.get("blurb", "") if isinstance(repo, dict) else ""

        try:
            resp = requests.get(
                f"https://github.com/{path}/releases.atom",
                timeout=TIMEOUT, headers={"User-Agent": USER_AGENT},
            )
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except Exception as exc:
            log.warning("github releases %s failed: %s", path, exc)
            continue

        kept = 0
        for entry in parsed.entries:
            if kept >= per_repo:
                break
            version = _strip_html(entry.get("title", "")).strip()
            link = entry.get("link", "")
            published = _from_struct(
                entry.get("published_parsed") or entry.get("updated_parsed")
            )
            if not version or not link or not published or published < cutoff:
                continue

            summary = _strip_html(entry.get("content", [{}])[0].get("value", "")
                                  if entry.get("content") else entry.get("summary", ""))
            items.append(
                _item(
                    title=_release_title(label, version, blurb),
                    url=link,
                    summary=summary[:300],
                    source=f"GitHub · {label}",
                    source_type="release",
                    tier="tooling",
                    weight=weight,
                    published=published,
                )
            )
            kept += 1

    log.info("github releases %d items across %d repos", len(items), len(cfg.get("repos", [])))
    return items
