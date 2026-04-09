"""
MCP server exposing the bookbuilder knowledge base as Claude-callable tools.

Start with:  bookbuilder mcp
Then configure Claude Desktop / Claude Code to point at this server via stdio.

Tools:
  search_knowledge   — ranked full-text search across summaries, tags, tweet text
  semantic_search    — embedding-based similarity search
  get_item           — full details for one item by ID
  list_clusters      — all named topic clusters
  get_cluster        — full cluster analysis for one cluster
  find_by_tech       — items referencing a specific technology
  find_by_author     — items from a specific Twitter/X handle
  find_by_category   — items in a taxonomy category (prefix matching)
  get_connections    — cross-cluster connection analysis
  get_gaps           — knowledge gap analysis
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .models import Item
from .search import matches_author, matches_category, matches_tech, score_item, search_items, semantic_search
from .store import iter_items, read_item


mcp = FastMCP("bookbuilder")

# Set at server startup via run_server()
_root: Path | None = None

# In-memory index loaded from knowledge/index.json at startup
# List of compact dicts — same shape as _build_search_index() in build.py
_index: list[dict] | None = None


def _get_root() -> Path:
    if _root is None:
        raise RuntimeError("MCP server not initialized — call run_server(root) first")
    return _root


def _get_index() -> list[dict]:
    """Return the in-memory index, loading it if not yet loaded."""
    global _index
    if _index is None:
        idx_path = _get_root() / "knowledge" / "index.json"
        if idx_path.exists():
            _index = json.loads(idx_path.read_text(encoding="utf-8"))
        else:
            _index = []
    return _index


def _reload_index() -> int:
    """Force-reload the index from disk. Returns item count."""
    global _index
    idx_path = _get_root() / "knowledge" / "index.json"
    if idx_path.exists():
        _index = json.loads(idx_path.read_text(encoding="utf-8"))
    else:
        _index = []
    return len(_index)


# ── In-memory search ─────────────────────────────────────────────────────────

def _index_score(record: dict, query_words: set[str]) -> float:
    """Score an index record against query words. Mirrors search.score_item weights."""
    score = 0.0
    summary = (record.get("summary") or "").lower()
    text    = (record.get("text") or "").lower()
    tags    = [t.lower() for t in (record.get("tags") or [])]
    cats    = [c.lower() for c in (record.get("cats") or [])]
    for word in query_words:
        if word in summary:
            score += 2.0
        if any(word in tag for tag in tags):
            score += 2.0
        if word in text:
            score += 1.0
        if any(word in cat for cat in cats):
            score += 1.0
    return score


def _search_index(
    query: str,
    *,
    limit: int = 10,
    min_quality: float = 0.0,
    category: str | None = None,
    author: str | None = None,
    tech: str | None = None,
) -> list[dict]:
    """Search the in-memory index. Returns compact record dicts."""
    index = _get_index()
    query_words = set(query.lower().split()) if query.strip() else set()
    cat_lower    = category.lower() if category else None
    author_lower = author.lower().lstrip("@") if author else None
    tech_lower   = tech.lower() if tech else None

    results: list[tuple[float, dict]] = []
    for record in index:
        qs = float(record.get("score") or 0.0)
        if qs < min_quality:
            continue
        if cat_lower and not any(c.lower().startswith(cat_lower) for c in (record.get("cats") or [])):
            continue
        if author_lower and (record.get("handle") or "").lower() != author_lower:
            continue
        if tech_lower and not any(
            tech_lower in t.lower() or t.lower() in tech_lower
            for t in (record.get("tech") or [])
        ):
            continue

        if query_words:
            s = _index_score(record, query_words)
            if s == 0:
                continue
        else:
            s = qs

        results.append((s, record))

    results.sort(key=lambda x: (-x[0], -float(x[1].get("score") or 0)))
    return [r for _, r in results[:limit]]


# ── Result formatters ────────────────────────────────────────────────────────

def _item_summary(item: Item) -> dict:
    """Compact representation for list results."""
    a = item.analysis
    return {
        "id":            item.id,
        "url":           item.url,
        "author":        item.author_handle or item.author_name,
        "text_snippet":  item.text[:200],
        "summary":       a.summary if a else "",
        "tags":          a.tags if a else [],
        "categories":    a.categories if a else [],
        "quality_score": a.quality_score if a else 0.0,
        "cluster_id":    a.cluster_id if a else None,
        "read_status":   item.state.read_status if item.state else "unread",
        "timestamp":     item.timestamp,
    }


def _item_full(item: Item) -> dict:
    """Full representation of an item."""
    a = item.analysis
    return {
        "id":            item.id,
        "source":        item.source,
        "url":           item.url,
        "text":          item.text,
        "author_name":   item.author_name,
        "author_handle": item.author_handle,
        "timestamp":     item.timestamp,
        "card_title":    item.card_title,
        "card_desc":     item.card_desc,
        "card_url":      item.card_url,
        "links":         item.links,
        "analysis": {
            "summary":       a.summary,
            "tags":          a.tags,
            "categories":    a.categories,
            "quality_score": a.quality_score,
            "cluster_id":    a.cluster_id,
            "entities": {
                "people":   a.entities.people,
                "orgs":     a.entities.orgs,
                "concepts": a.entities.concepts,
                "places":   a.entities.places,
            },
            "tech_refs": {
                "languages":  a.tech_refs.languages,
                "frameworks": a.tech_refs.frameworks,
                "tools":      a.tech_refs.tools,
                "packages":   a.tech_refs.packages,
                "repos":      a.tech_refs.repos,
                "hardware":   a.tech_refs.hardware,
                "platforms":  a.tech_refs.platforms,
            },
        } if a else None,
        "fetched_pages": [
            {
                "url":        p.url,
                "title":      p.title,
                "word_count": p.word_count,
                "status":     p.status,
            }
            for p in item.fetched_pages
        ],
    }


# ── MCP Tools ────────────────────────────────────────────────────────────────

@mcp.tool()
def search_knowledge(query: str, limit: int = 10) -> list[dict]:
    """Search the knowledge base for items matching a query.

    Searches across summaries, tags, tweet text, and categories using an
    in-memory index for fast response. Results ranked by relevance then quality.

    Args:
        query: Space-separated search terms (case-insensitive, all terms scored)
        limit: Maximum results to return (default 10, capped at 50)
    """
    limit = min(max(1, limit), 50)
    return _search_index(query, limit=limit)


@mcp.tool()
def get_item(item_id: str) -> dict | None:
    """Retrieve the full details of a single knowledge base item by ID.

    Returns the complete item record including the tweet text, all AI analysis
    (summary, tags, entities, tech refs, quality score), fetched page metadata,
    and pipeline state. Returns null if the item does not exist.

    Args:
        item_id: The item ID (numeric string extracted from the tweet URL)
    """
    root = _get_root()
    item = read_item(root, item_id)
    return _item_full(item) if item else None


@mcp.tool()
def list_clusters() -> list[dict]:
    """List all named topic clusters in the knowledge base.

    Clusters are produced by the 'bookbuilder cluster' stage using HDBSCAN on
    item embeddings, then named by Claude. Returns each cluster's ID, label,
    dominant parent category, item count, and whether the label was manually
    overridden.

    Returns an empty list if clustering has not been run yet.
    """
    root = _get_root()
    clusters_path = root / "system" / "clusters.json"
    if not clusters_path.exists():
        return []

    data: dict = json.loads(clusters_path.read_text(encoding="utf-8"))
    result = []
    for cluster_id, info in data.items():
        result.append({
            "cluster_id":  cluster_id,
            "label":       info.get("label", cluster_id),
            "parent":      info.get("parent", ""),
            "item_count":  info.get("item_count", 0),
            "auto":        info.get("auto", True),
            "stale":       info.get("stale", False),
            "updated_at":  info.get("updated_at", ""),
        })
    result.sort(key=lambda x: -x["item_count"])
    return result


@mcp.tool()
def find_by_tech(tech: str, limit: int = 10) -> list[dict]:
    """Find items that reference a specific technology, tool, language, or package.

    Matches against tech_refs in the index. Case-insensitive substring match
    (e.g. "torch" matches "pytorch").

    Args:
        tech:  Technology name (e.g. "python", "react", "langchain")
        limit: Maximum results (default 10, capped at 50)
    """
    limit = min(max(1, limit), 50)
    return _search_index("", limit=limit, tech=tech)


@mcp.tool()
def find_by_author(handle: str, limit: int = 20) -> list[dict]:
    """Find all saved items from a specific Twitter/X author.

    Exact handle match (case-insensitive). Leading '@' stripped automatically.

    Args:
        handle: Twitter/X handle, with or without '@' (e.g. "karpathy")
        limit:  Maximum results (default 20, capped at 100)
    """
    limit = min(max(1, limit), 100)
    return _search_index("", limit=limit, author=handle)


@mcp.tool()
def find_by_category(category: str, limit: int = 20) -> list[dict]:
    """Find items belonging to a taxonomy category.

    Prefix matching — "ai_ml" matches all subcategories beneath it.

    Args:
        category: Category path or parent prefix (e.g. "ai_ml", "ai_ml/ai_tools")
        limit:    Maximum results (default 20, capped at 100)
    """
    limit = min(max(1, limit), 100)
    return _search_index("", limit=limit, category=category)


@mcp.tool()
def reload_index() -> dict:
    """Reload the in-memory search index from disk.

    Call this after running `bookbuilder build` or `bookbuilder agents` to pick
    up new items and analysis without restarting the server.

    Returns the number of items now in the index.
    """
    count = _reload_index()
    return {"items_loaded": count}


@mcp.tool()
def semantic_search_knowledge(query: str, limit: int = 10) -> list[dict]:
    """Search the knowledge base using embedding similarity.

    Uses cosine similarity against pre-computed item embeddings for semantic
    matching. Better than keyword search for conceptual queries.
    Requires the cluster stage to have been run (embeddings are cached there).

    Args:
        query: Natural language query (e.g. "how do transformers handle long context")
        limit: Maximum results to return (default 10, capped at 50)
    """
    root = _get_root()
    limit = min(max(1, limit), 50)
    results = semantic_search(root, query, limit=limit)
    return [_item_summary(item) for _, item in results]


@mcp.tool()
def get_cluster(cluster_id: str) -> dict | None:
    """Get the full analysis for a single cluster.

    Returns cluster metadata plus the AI-written synthesis from the
    cluster_analyst agent (if run). Returns null if not found.

    Args:
        cluster_id: Cluster ID (e.g. "cluster_001") — get IDs from list_clusters
    """
    root = _get_root()
    clusters_path = root / "system" / "clusters.json"
    if not clusters_path.exists():
        return None
    data = json.loads(clusters_path.read_text(encoding="utf-8"))
    info = data.get(cluster_id)
    if not info:
        return None
    result = dict(info)
    result["cluster_id"] = cluster_id
    analysis_path = root / "knowledge" / "clusters" / f"{cluster_id}.md"
    result["analysis"] = analysis_path.read_text(encoding="utf-8") if analysis_path.exists() else None
    return result


@mcp.tool()
def get_connections() -> str | None:
    """Get the cross-cluster connection analysis.

    Returns the full text of knowledge/connections.md produced by the
    connection_finder agent. Returns null if the agent hasn't been run.
    """
    root = _get_root()
    path = root / "knowledge" / "connections.md"
    return path.read_text(encoding="utf-8") if path.exists() else None


@mcp.tool()
def get_gaps() -> str | None:
    """Get the knowledge gap analysis.

    Returns the full text of knowledge/gaps.md produced by the gap_detector
    agent. Identifies thin categories, noisy clusters, and underrepresented
    technologies. Returns null if the agent hasn't been run.
    """
    root = _get_root()
    path = root / "knowledge" / "gaps.md"
    return path.read_text(encoding="utf-8") if path.exists() else None


# ── Entry point ───────────────────────────────────────────────────────────────

def run_server(root: Path) -> None:
    """Start the MCP server using stdio transport (for Claude Desktop / Claude Code)."""
    global _root
    _root = root
    # Pre-load the index so the first search call is fast
    count = _reload_index()
    import sys
    print(f"bookbuilder MCP server started — {count} items in index", file=sys.stderr)
    mcp.run(transport="stdio")
