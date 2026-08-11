#!/usr/bin/env python3
"""Entry point. Fetch every source, merge, score, and write the dashboard data.

    python collect.py              # normal run
    python collect.py --dry-run    # fetch and score, print, write nothing

Writes:
    docs/data/items.json    current ranked feed plus summary stats
    docs/data/history.json  daily topic counts, appended across runs
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import pipeline
import sources

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "docs" / "data"
ITEMS_PATH = DATA_DIR / "items.json"
HISTORY_PATH = DATA_DIR / "history.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-9s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("collect")


def load_config() -> dict:
    with open(ROOT / "config.yml", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_archive() -> list[dict]:
    """Every item from previous runs.

    The page is scrolled backwards through time, so items have to survive
    past the run that found them: a source stops listing a story long before
    it stops being worth reading. Sources are the input, this file is the
    record.
    """
    if not ITEMS_PATH.exists():
        return []
    try:
        with open(ITEMS_PATH, encoding="utf-8") as handle:
            stored = json.load(handle).get("items", [])
    except (json.JSONDecodeError, KeyError, OSError):
        return []
    return [i for i in stored if isinstance(i, dict) and i.get("id")]


def merge_archive(fresh: list[dict], archive: list[dict], config: dict) -> list[dict]:
    """Fold this run's items into the stored ones, newest wins.

    A re-fetched item replaces its stored copy so engagement counts and
    source badges stay current, but anything the sources have since dropped
    is carried forward untouched until it ages out.
    """
    retention = int(config.get("output", {}).get("retention_days", 90))
    max_items = int(config.get("output", {}).get("max_items", 3000))
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retention)

    merged: dict[str, dict] = {i["id"]: i for i in archive}
    merged.update({i["id"]: i for i in fresh})

    kept = []
    for item in merged.values():
        published = _parse_published(item.get("published"))
        if published is None or published < cutoff:
            continue
        # age_hours is a snapshot of the run that wrote it; recompute so a
        # carried-forward item doesn't stay "3h ago" forever.
        item["age_hours"] = round(max(0.0, (now - published).total_seconds() / 3600.0), 1)
        kept.append(item)

    kept.sort(key=lambda i: i.get("score", 0), reverse=True)
    return kept[:max_items]


def _parse_published(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def build_history(items: list[dict], topics: list[str]) -> list[dict]:
    """Append today's topic counts, keeping a rolling 30-day window.

    Counts are recomputed for today on every run rather than incremented, so
    running the workflow four times a day doesn't quadruple the numbers.
    """
    history = []
    if HISTORY_PATH.exists():
        try:
            with open(HISTORY_PATH, encoding="utf-8") as handle:
                history = json.load(handle)
        except (json.JSONDecodeError, OSError):
            history = []

    per_day: dict[str, Counter] = defaultdict(Counter)
    for item in items:
        day = item["published"][:10]
        for topic in item["topics"]:
            per_day[day][topic] += 1

    by_date = {row["date"]: row for row in history if isinstance(row, dict) and "date" in row}
    for day, counts in per_day.items():
        row = {"date": day}
        row.update({topic: counts.get(topic, 0) for topic in topics})
        by_date[day] = row

    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    return sorted(
        (row for date, row in by_date.items() if date >= cutoff),
        key=lambda r: r["date"],
    )


def summarise(items: list[dict], new_ids: set[str], config: dict) -> dict:
    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)
    week_ago = now - timedelta(days=7)

    def parsed(item):
        return datetime.fromisoformat(item["published"])

    last_24h = [i for i in items if parsed(i) >= day_ago]
    last_7d = [i for i in items if parsed(i) >= week_ago]

    topic_counts = Counter(t for i in items for t in i["topics"])
    source_counts = Counter(i["source"] for i in items)
    type_counts = Counter(i["source_type"] for i in items)
    domain_counts = Counter(i["domain"] for i in items if i["domain"])

    # Daily volume by source type, for the velocity chart.
    velocity: dict[str, Counter] = defaultdict(Counter)
    for item in last_7d:
        velocity[item["published"][:10]][item["source_type"]] += 1
    velocity_rows = [
        {"date": day, **dict(counts)} for day, counts in sorted(velocity.items())
    ]

    return {
        "total": len(items),
        "new": len(new_ids),
        "last_24h": len(last_24h),
        "last_7d": len(last_7d),
        "corroborated": sum(1 for i in items if i["source_count"] > 1),
        "topics": topic_counts.most_common(),
        "sources": source_counts.most_common(20),
        "types": type_counts.most_common(),
        "domains": domain_counts.most_common(12),
        "velocity": velocity_rows,
        "source_types": sorted(type_counts),
        "all_topics": sorted(config.get("topics", {})),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and rank, print results, write nothing")
    parser.add_argument("--check-feeds", action="store_true",
                        help="diagnose every configured RSS feed and exit")
    args = parser.parse_args()

    config = load_config()

    if args.check_feeds:
        rows = sources.check_feeds(config.get("feeds", []))
        print()
        print(f"{'FEED':<22} {'ENTRIES':>7}  {'NEWEST':<12} STATUS")
        print("-" * 78)
        for row in rows:
            newest = row["newest"].strftime("%Y-%m-%d") if row["newest"] else "-"
            print(f"{row['name']:<22} {row['entries']:>7}  {newest:<12} {row['status']}")
        broken = [r for r in rows if r["status"] != "ok"]
        print()
        if broken:
            print(f"{len(broken)} feed(s) need attention:")
            for row in broken:
                print(f"  {row['name']}: {row['url']}")
            print("\nFix or remove them in config.yml. A broken feed only warns;")
            print("it never fails the run.")
        else:
            print("All feeds healthy.")
        return 0
    archive = load_archive()
    seen = {i["id"] for i in archive}

    raw: list[dict] = []
    raw += sources.fetch_rss(
        config.get("feeds", []),
        max_age_days=int(config.get("output", {}).get("retention_days", 21)),
    )
    raw += sources.fetch_hackernews(config.get("hackernews", {}))
    raw += sources.fetch_hf_models(config.get("hf_models", {}))
    raw += sources.fetch_github_releases(config.get("gh_releases", {}))
    raw += sources.fetch_arxiv(config.get("arxiv", {}))
    raw += sources.fetch_reddit(config.get("reddit", {}))
    raw += sources.fetch_bluesky(config.get("bluesky", {}))

    if not raw:
        log.error("no items fetched from any source — refusing to overwrite data")
        return 1

    log.info("fetched %d raw items", len(raw))

    items = pipeline.process(pipeline.dedupe(raw), config)

    new_ids = {i["id"] for i in items} - seen if seen else set()
    for item in items:
        item["is_new"] = item["id"] in new_ids

    fetched = len(items)
    items = merge_archive(items, archive, config)
    for item in items:
        item.setdefault("is_new", False)
        if item["id"] not in new_ids:
            item["is_new"] = False

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": summarise(items, new_ids, config),
        "items": items,
    }

    if args.dry_run:
        print(json.dumps(payload["stats"], indent=2, default=str))
        for item in items[:15]:
            flag = "NEW " if item["is_new"] else "    "
            badge = f"x{item['source_count']}" if item["source_count"] > 1 else "  "
            print(f"{flag}{item['score']:7.1f} {badge} [{item['source']}] {item['title'][:78]}")
        return 0

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    history = build_history(items, sorted(config.get("topics", {})))

    with open(ITEMS_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
    with open(HISTORY_PATH, "w", encoding="utf-8") as handle:
        json.dump(history, handle, ensure_ascii=False, indent=1)

    log.info(
        "wrote %d items (%d fetched this run, %d new, %d carried from archive) to %s",
        len(items), fetched, len(new_ids), max(0, len(items) - fetched), ITEMS_PATH,
    )

    # Surface counts in the Actions run summary for at-a-glance health checks.
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(f"### AI news radar\n\n")
            handle.write(f"- {len(items)} items ({len(new_ids)} new)\n")
            handle.write(f"- {payload['stats']['corroborated']} corroborated by 2+ sources\n")
            handle.write(f"- sources: {', '.join(s for s, _ in payload['stats']['sources'][:8])}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
