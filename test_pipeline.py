#!/usr/bin/env python3
"""Tests for the dedupe and scoring logic.

Runs offline against fixtures, so it works in CI without touching the network.

    python test_pipeline.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import yaml

import pipeline

NOW = datetime.now(timezone.utc)
FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual == expected:
        print(f"  pass  {label}")
    else:
        print(f"  FAIL  {label}\n          expected {expected!r}\n          actual   {actual!r}")
        FAILURES.append(label)


def check_true(label: str, condition) -> None:
    check(label, bool(condition), True)


def item(title, url, source, source_type="blog", tier="lab", weight=1.0,
         hours_ago=2, engagement=0, summary=""):
    return {
        "title": title,
        "url": url,
        "summary": summary,
        "source": source,
        "source_type": source_type,
        "tier": tier,
        "weight": weight,
        "published": NOW - timedelta(hours=hours_ago),
        "engagement": engagement,
        "engagement_label": "",
    }


print("\ncanonical_url")
check(
    "strips utm parameters",
    pipeline.canonical_url("https://openai.com/index/introducing-openai-privacy-filter?utm_source=x&utm_medium=social"),
    "https://openai.com/index/introducing-openai-privacy-filter",
)
check(
    "strips www, trailing slash and fragment",
    pipeline.canonical_url("http://www.ai.meta.com/research/sam2/#results"),
    "https://ai.meta.com/research/sam2",
)
check(
    "keeps meaningful query parameters",
    pipeline.canonical_url("https://news.ycombinator.com/item?id=49227675&utm_source=rss"),
    "https://news.ycombinator.com/item?id=49227675",
)
check(
    "two tracking variants collapse to one identity",
    pipeline.canonical_url("https://ai.meta.com/research/sam2/?fbclid=abc"),
    pipeline.canonical_url("https://www.ai.meta.com/research/sam2"),
)

print("\nwrapper rejection")
check_true(
    "microsoft safelinks is rejected",
    pipeline.is_wrapper("https://statics.teams.cdn.office.net/evergreen-assets/safelinks/2/atp-safelinks.html"),
)
check_true("lnkd.in is rejected", pipeline.is_wrapper("https://lnkd.in/abc123"))
check_true("t.co is rejected", pipeline.is_wrapper("https://t.co/xyz"))
check_true(
    "a real vendor page is not rejected",
    not pipeline.is_wrapper("https://azure.microsoft.com/en-us/products/ai-foundry/tools/document-intelligence"),
)

print("\nword-boundary keyword matching")
rag = pipeline.compile_terms(["RAG"])
check("RAG does not match 'storage'", pipeline.count_hits(rag, "cheap storage tiers"), [])
check("RAG does not match 'dragging'", pipeline.count_hits(rag, "dragging the slider"), [])
check("RAG matches standalone RAG", pipeline.count_hits(rag, "a RAG pipeline"), ["RAG"])
check("RAG matches case-insensitively", pipeline.count_hits(rag, "hybrid rag retrieval"), ["RAG"])

check("RAG matches inside a hyphenated compound", pipeline.count_hits(rag, "a RAG-based assistant"), ["RAG"])

llm = pipeline.compile_terms(["LLM", "model", "agent"])
check("LLM matches in 'LLM-as-judge'", pipeline.count_hits(llm, "an LLM-as-judge rubric"), ["LLM"])
check("plural 'models' matches 'model'", pipeline.count_hits(llm, "small models beat big ones"), ["model"])
check("plural 'LLMs' matches 'LLM'", pipeline.count_hits(llm, "serving LLMs cheaply"), ["LLM"])
check("plural 'agents' matches 'agent'", pipeline.count_hits(llm, "multi-step agents"), ["agent"])
check("'model' does not match 'modem'", pipeline.count_hits(llm, "a modem restart"), [])

multiword = pipeline.compile_terms(["fine-tuning", "vector database"])
check(
    "hyphenated term matches a spaced variant",
    pipeline.count_hits(multiword, "we tried fine tuning it"),
    ["fine-tuning"],
)
check(
    "multi-word term matches",
    pipeline.count_hits(multiword, "a vector database index"),
    ["vector database"],
)

print("\ndeduplication")
raw = [
    item("Introducing SAM 2: Segment Anything in Images and Videos",
         "https://ai.meta.com/research/sam2/", "Meta AI", weight=1.8),
    item("Introducing SAM 2: Segment Anything in Images and Videos",
         "https://ai.meta.com/research/sam2/?utm_source=twitter", "Meta AI", weight=1.8),
    item("SAM 2: Segment Anything in Images and Videos",
         "https://news.ycombinator.com/item?id=41000001", "Hacker News",
         source_type="social", tier="community", weight=1.4, engagement=340),
    item("Segment Anything in Images and Videos (SAM 2)",
         "http://arxiv.org/abs/2408.00714", "arXiv",
         source_type="paper", tier="academic", weight=1.0),
    item("OpenAI introduces a privacy filter for model inputs",
         "https://openai.com/index/introducing-openai-privacy-filter",
         "OpenAI", weight=2.0),
]
merged = pipeline.dedupe(raw)
check("five raw items collapse to two stories", len(merged), 2)

sam = next(i for i in merged if "SAM 2" in i["title"] or "Segment Anything" in i["title"])
check("the lab's own post becomes the primary", sam["source"], "Meta AI")
check("three distinct sources are recorded", sam["source_count"], 3)
check("engagement is summed across sources", sam["engagement"], 340)

print("\nfilter and score")
with open("config.yml", encoding="utf-8") as handle:
    config = yaml.safe_load(handle)

candidates = [
    item("Introducing SAM 2: Segment Anything in Images and Videos",
         "https://ai.meta.com/research/sam2/", "Meta AI", weight=1.8,
         summary="A unified model for promptable visual segmentation in images and video."),
    item("Nvidia stock price jumps on earnings call",
         "https://example.com/nvidia-stock", "Example", weight=1.0,
         summary="Shares rose after the earnings call."),
    item("Ten tips for better sourdough bread",
         "https://example.com/sourdough", "Example", weight=1.0,
         summary="Hydration and proofing."),
    item("Safelinks wrapper that should never appear",
         "https://statics.teams.cdn.office.net/evergreen-assets/safelinks/2/atp-safelinks.html",
         "Example", weight=1.0, summary="An LLM agent article behind a wrapper."),
    item("Azure AI Foundry adds Document Intelligence tooling",
         "https://azure.microsoft.com/en-us/products/ai-foundry/tools/document-intelligence",
         "Azure AI", weight=1.2,
         summary="Extract structured data from documents with a vision model."),
]
processed = pipeline.process(pipeline.dedupe(candidates), config)
titles = [i["title"] for i in processed]

check_true("the SAM 2 announcement survives", any("SAM 2" in t for t in titles))
check_true("the Azure tooling page survives", any("Azure AI Foundry" in t for t in titles))
check_true("the stock story is excluded", not any("stock price" in t for t in titles))
check_true("the sourdough post is filtered as irrelevant", not any("sourdough" in t for t in titles))
check_true("the safelinks wrapper is dropped", not any("Safelinks" in t for t in titles))

sam_scored = next(i for i in processed if "SAM 2" in i["title"])
check_true("topics are tagged", "multimodal" in sam_scored["topics"])
check_true("a score is assigned", sam_scored["score"] > 0)
check_true("an id is assigned", len(sam_scored["id"]) == 12)

print("\nrecency and corroboration")
old_corroborated = pipeline.dedupe([
    item("Llama 4 released with a new mixture of experts architecture",
         "https://ai.meta.com/blog/llama4", "Meta AI", weight=1.8, hours_ago=60),
    item("Llama 4 released with new mixture of experts architecture",
         "https://news.ycombinator.com/item?id=42", "Hacker News",
         source_type="social", weight=1.4, hours_ago=59, engagement=500),
])
fresh_single = [
    item("A new LLM inference benchmark for agent workloads",
         "https://example.com/bench", "Example", weight=1.0, hours_ago=1,
         summary="Benchmarking inference latency for agent workloads."),
]
ranked = pipeline.process(old_corroborated + fresh_single, config)
check_true(
    "a corroborated two-day-old story outranks a fresh single-source post",
    ranked[0]["source_count"] > 1,
)

print()
if FAILURES:
    print(f"{len(FAILURES)} test(s) failed: {', '.join(FAILURES)}")
    sys.exit(1)
print("all tests passed")
