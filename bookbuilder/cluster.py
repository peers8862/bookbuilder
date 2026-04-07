"""
Cluster stage: generate embeddings for analyzed items, run HDBSCAN, name clusters
with Claude, and write results to system/clusters.json.

Each item's cluster_id is updated in its knowledge/items/{id}.json.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .config import load_config
from .store import iter_items, write_item


# ── Embeddings ───────────────────────────────────────────────────────────────

def _item_embedding_text(item) -> str:
    """Compose a short text representative of the item for embedding."""
    parts = []
    if item.analysis:
        if item.analysis.summary:
            parts.append(item.analysis.summary)
        if item.analysis.tags:
            parts.append(" ".join(item.analysis.tags))
        if item.analysis.entities.concepts:
            parts.append(" ".join(item.analysis.entities.concepts))
    if not parts:
        parts.append(item.text[:300])
    return " ".join(parts)[:1000]


def _load_cached_embeddings(cache_path: Path) -> dict[str, list[float]]:
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    return {}


def _save_cached_embeddings(cache_path: Path, embeddings: dict[str, list[float]]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(embeddings), encoding="utf-8")


def _generate_embeddings(
    items: list,
    cache_path: Path,
    model: str,
    api_key: str,
) -> dict[str, list[float]]:
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    cached = _load_cached_embeddings(cache_path)

    to_embed = [(item.id, _item_embedding_text(item)) for item in items if item.id not in cached]
    if not to_embed:
        print(f"  all {len(items)} embeddings cached")
        return cached

    print(f"  generating {len(to_embed)} embeddings (model={model})")
    batch_size = 100
    for i in range(0, len(to_embed), batch_size):
        batch = to_embed[i:i + batch_size]
        ids, texts = zip(*batch)
        resp = client.embeddings.create(model=model, input=list(texts))
        for item_id, emb_obj in zip(ids, resp.data):
            cached[item_id] = emb_obj.embedding
        _save_cached_embeddings(cache_path, cached)
        print(f"  embedded {min(i + batch_size, len(to_embed))}/{len(to_embed)}")
        time.sleep(0.2)

    return cached


# ── Clustering ───────────────────────────────────────────────────────────────

def _run_hdbscan(
    embeddings: dict[str, list[float]],
    min_cluster_size: int,
    min_samples: int,
) -> dict[str, int]:
    """Return {item_id: cluster_label} where label -1 means noise."""
    import hdbscan

    item_ids = list(embeddings.keys())
    matrix = np.array([embeddings[iid] for iid in item_ids], dtype=np.float32)

    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        metric="euclidean",
    )
    labels = clusterer.fit_predict(matrix)
    return {item_ids[i]: int(labels[i]) for i in range(len(item_ids))}


# ── Cluster naming ────────────────────────────────────────────────────────────

def _name_cluster(client, items_sample: list, model: str) -> str:
    """Ask Claude to name a cluster given a sample of item summaries."""
    summaries = "\n".join(
        f"- {item.analysis.summary}" if item.analysis else f"- {item.text[:100]}"
        for item in items_sample[:10]
    )
    prompt = (
        "Give a short (2–5 word) label for this cluster of related tweets/articles.\n"
        "Return ONLY the label, nothing else.\n\n"
        f"Items:\n{summaries}"
    )
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=20,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip().strip('"').strip("'")
    except Exception:
        return "Unlabelled Cluster"


# ── Load/save clusters.json ──────────────────────────────────────────────────

def _load_clusters(root: Path) -> dict:
    path = root / "system" / "clusters.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _save_clusters(root: Path, clusters: dict) -> None:
    path = root / "system" / "clusters.json"
    path.write_text(json.dumps(clusters, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Main cluster runner ───────────────────────────────────────────────────────

def run_cluster(root: Path, force: bool = False) -> int:
    """
    Cluster all analyzed items. Returns number of clusters found.
    """
    cfg = load_config(root)
    ai_cfg      = cfg.get("ai", {})
    cluster_cfg = cfg.get("cluster", {})

    emb_model   = ai_cfg.get("embedding_model", "text-embedding-3-small")
    claude_model = ai_cfg.get("model", "claude-sonnet-4-6")
    min_cluster_size = int(cluster_cfg.get("min_cluster_size", 8))
    min_samples      = int(cluster_cfg.get("min_samples", 3))
    recluster        = bool(cluster_cfg.get("recluster_on_rerun", False))

    oai_key = os.environ.get(cfg.get("api_keys", {}).get("openai_env", "OPENAI_API_KEY"), "")
    ant_key = os.environ.get(cfg.get("api_keys", {}).get("anthropic_env", "ANTHROPIC_API_KEY"), "")

    if not oai_key:
        print("ERROR: OPENAI_API_KEY not set (needed for embeddings)")
        return 0

    # Load items that have been analyzed
    all_items = [
        item for item in iter_items(root)
        if item.analysis is not None
        and (force or recluster or item.analysis.cluster_id is None)
    ]
    if not all_items:
        print("  no items need clustering")
        return 0

    print(f"  clustering {len(all_items)} items")

    cache_path = root / "cache" / "embeddings" / "items.json"
    embeddings = _generate_embeddings(all_items, cache_path, emb_model, oai_key)

    # Filter to items we have embeddings for
    embeddable = [item for item in all_items if item.id in embeddings]
    if len(embeddable) < min_cluster_size:
        print(f"  too few items ({len(embeddable)}) for clustering (min={min_cluster_size})")
        return 0

    labels = _run_hdbscan(
        {item.id: embeddings[item.id] for item in embeddable},
        min_cluster_size,
        min_samples,
    )

    # Group by label
    from collections import defaultdict
    groups: dict[int, list] = defaultdict(list)
    for item in embeddable:
        groups[labels.get(item.id, -1)].append(item)

    existing_clusters = _load_clusters(root)
    now = datetime.now(timezone.utc).isoformat()
    import anthropic
    ant_client = anthropic.Anthropic(api_key=ant_key) if ant_key else None

    new_clusters: dict[str, dict] = {}
    for label, items in sorted(groups.items()):
        if label == -1:
            cluster_id = "_noise"
        else:
            cluster_id = f"cluster_{label:03d}"

        # Name new clusters (skip noise)
        if label != -1 and ant_client:
            cluster_name = _name_cluster(ant_client, items, claude_model)
        else:
            cluster_name = "Noise / Outliers" if label == -1 else f"Cluster {label}"

        # Infer dominant category from items
        from collections import Counter
        cats = Counter()
        for item in items:
            for c in (item.analysis.categories if item.analysis else []):
                cats[c.split("/")[0]] += 1
        parent = cats.most_common(1)[0][0] if cats else "_uncategorized"

        new_clusters[cluster_id] = {
            "label": cluster_name,
            "auto": True,
            "parent": parent,
            "item_count": len(items),
            "updated_at": now,
        }

        # Write cluster_id back to each item
        for item in items:
            if item.analysis:
                item.analysis.cluster_id = cluster_id
                write_item(root, item)

    # Merge with existing (preserve manually renamed clusters)
    for cid, existing in existing_clusters.items():
        if cid not in new_clusters:
            existing["stale"] = True
            new_clusters[cid] = existing
        elif not existing.get("auto", True):
            # Preserve manual label override
            new_clusters[cid]["label"] = existing["label"]

    _save_clusters(root, new_clusters)
    real_clusters = sum(1 for cid in new_clusters if cid != "_noise")
    print(f"  found {real_clusters} clusters  noise={len(groups.get(-1, []))} items")
    return real_clusters
