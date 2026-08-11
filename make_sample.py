#!/usr/bin/env python3
"""Generate placeholder data so the dashboard renders before the first real run.

Runs the fixtures through the real dedupe and scoring path, so the shape of
the output is guaranteed to match what collect.py produces. The payload is
flagged with `sample: true` and the page shows a banner saying so.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import collect
import pipeline

ROOT = Path(__file__).resolve().parent
NOW = datetime.now(timezone.utc)

SEED = [
    ("Introducing SAM 2: segment anything in images and videos",
     "https://ai.meta.com/research/sam2/", "Meta AI", "blog", "lab", 1.8, 5, 0,
     "A unified model for promptable visual segmentation across images and video, with real-time inference."),
    ("SAM 2: segment anything in images and videos",
     "https://news.ycombinator.com/item?id=41000001", "Hacker News", "social", "community", 1.4, 6, 412,
     ""),
    ("Introducing the OpenAI privacy filter",
     "https://openai.com/index/introducing-openai-privacy-filter", "OpenAI", "blog", "lab", 2.0, 11, 0,
     "A redaction layer that strips personal data from model inputs before inference."),
    ("Document Intelligence in Azure AI Foundry",
     "https://azure.microsoft.com/en-us/products/ai-foundry/tools/document-intelligence",
     "Azure AI", "blog", "cloud", 1.2, 19, 0,
     "Extract structured fields, tables and key-value pairs from unstructured documents with a vision model."),
    ("Quantization-aware distillation for small language models",
     "http://arxiv.org/abs/2408.11111", "arXiv", "paper", "academic", 1.0, 14, 0,
     "We show INT4 group-wise quantization with distillation recovers most of the accuracy lost to naive post-training quantization."),
    ("Running a 70B model on a single consumer GPU with speculative decoding",
     "https://r.example.dev/70b-single-gpu", "r/LocalLLaMA", "social", "community", 1.2, 9, 287,
     "Throughput numbers for a KV cache offloading setup with a draft model."),
    ("A practical guide to evaluating RAG systems",
     "https://huggingface.co/blog/rag-evaluation", "Hugging Face", "blog", "platform", 1.5, 27, 0,
     "Golden datasets, groundedness scoring and LLM-as-judge rubrics for retrieval pipelines."),
    ("Anthropic publishes an interpretability update on feature steering",
     "https://www.anthropic.com/news/feature-steering", "Anthropic", "blog", "lab", 2.0, 33, 0,
     "New results on steering model behaviour through learned sparse features."),
    ("Mixture-of-experts routing collapse and how to avoid it",
     "http://arxiv.org/abs/2408.22222", "arXiv", "paper", "academic", 1.0, 40, 0,
     "Analysis of expert imbalance during pretraining of sparse transformer models."),
    ("vLLM 0.7 ships paged attention improvements and better tool-call parsing",
     "https://github.com/vllm-project/vllm/releases/tag/v0.7.0", "Hacker News", "social", "community", 1.4, 46, 198,
     "Inference throughput gains for long-context serving workloads."),
    ("Google DeepMind on long-context retrieval without a vector database",
     "https://deepmind.google/blog/long-context-retrieval", "Google DeepMind", "blog", "lab", 1.8, 52, 0,
     "Comparing in-context retrieval against embedding search at one million tokens."),
    ("What changed in agent orchestration frameworks this quarter",
     "https://simonwillison.net/2026/agent-orchestration-roundup/", "Simon Willison", "blog", "writer", 1.3, 58, 0,
     "A survey of planner-executor patterns, tool registries and human-in-the-loop gates."),
    ("Clinical NLP for structured extraction from discharge summaries",
     "http://arxiv.org/abs/2408.33333", "arXiv", "paper", "academic", 1.0, 66, 0,
     "A benchmark for extracting medication and diagnosis entities from unstructured clinical notes."),
    ("NVIDIA publishes inference latency benchmarks for agentic workloads",
     "https://blogs.nvidia.com/blog/agentic-inference-benchmarks/", "NVIDIA AI", "blog", "platform", 1.3, 74, 0,
     "Token throughput and time-to-first-token across batch sizes for multi-step agent traces."),
    ("Fine-tuning beats prompting for narrow classification, revisited",
     "https://magazine.sebastianraschka.com/p/finetuning-vs-prompting", "Sebastian Raschka", "blog", "writer", 1.3, 88, 0,
     "LoRA adapters on small models versus few-shot prompting on frontier models."),
    ("Mistral releases a multimodal model with native OCR support",
     "https://mistral.ai/news/multimodal-ocr", "Mistral AI", "blog", "lab", 1.5, 96, 0,
     "Vision-language model tuned for document understanding and chart reasoning."),
    ("Guardrails that actually hold: policy enforcement in production agents",
     "https://www.microsoft.com/en-us/research/blog/agent-guardrails", "Microsoft Research", "blog", "lab", 1.4, 104, 0,
     "Evaluation of policy layers and jailbreak resistance in deployed agent systems."),
    ("Embedding model leaderboards are measuring the wrong thing",
     "https://news.ycombinator.com/item?id=41000456", "Hacker News", "social", "community", 1.4, 112, 156,
     "Retrieval hit rate on real corpora diverges sharply from MTEB rankings."),
]


def main() -> None:
    with open(ROOT / "config.yml", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)

    raw = []
    for title, url, source, stype, tier, weight, hours, engagement, summary in SEED:
        label = ""
        if engagement and stype == "social":
            label = f"{engagement} points"
        raw.append({
            "title": title, "url": url, "summary": summary,
            "source": source, "source_type": stype, "tier": tier,
            "weight": weight,
            "published": NOW - timedelta(hours=hours),
            "engagement": engagement, "engagement_label": label,
        })

    items = pipeline.process(pipeline.dedupe(raw), config)
    new_ids = {i["id"] for i in items[:4]}
    for item in items:
        item["is_new"] = item["id"] in new_ids

    payload = {
        "generated_at": NOW.isoformat(),
        "sample": True,
        "stats": collect.summarise(items, new_ids, config),
        "items": items,
    }

    data_dir = ROOT / "docs" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    with open(data_dir / "items.json", "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)

    history = collect.build_history(items, sorted(config.get("topics", {})))
    with open(data_dir / "history.json", "w", encoding="utf-8") as handle:
        json.dump(history, handle, ensure_ascii=False, indent=1)

    print(f"wrote {len(items)} sample items")
    for i in items[:8]:
        badge = f"x{i['source_count']}" if i["source_count"] > 1 else "  "
        print(f"  {i['score']:7.1f} {badge} [{i['source']:<18}] {i['title'][:64]}")


if __name__ == "__main__":
    main()
