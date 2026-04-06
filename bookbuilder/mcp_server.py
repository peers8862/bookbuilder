"""
MCP server exposing the bookbuilder knowledge base as Claude-callable tools.

Start with:  bookbuilder mcp
Then configure Claude Desktop / Claude Code to point at this server via stdio.

Tools:
  search_knowledge   — ranked full-text search across summaries, tags, tweet text
  get_item           — full details for one item by ID
  list_clusters      — all named topic clusters
  find_by_tech       — items referencing a specific technology
  find_by_author     — items from a specific Twitter/X handle
  find_by_category   — items in a taxonomy category (supports prefix matching)
"""

from __future__ import annotations

import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .models import Item
from .store import iter_items, read_item


mcp = FastMCP("bookbuilder")

# Set at server startup via run_server()
_root: Path | None = None


def _get_root() -> Path:
    if _root is None:
        raise RuntimeError("MCP server not initialized — call run_server(root) first")
    return _root


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


# ── Search scoring ────────────────────────────────────────────────────────────

def _score_item(item: Item, query_words: set[str]) -> float:
    """Score an item against query words; higher = more relevant."""
    a = item.analysis
    score = 0.0

    text_lower    = item.text.lower()
    summary_lower = a.summary.lower() if a else ""
    card_lower    = item.card_title.lower() if item.card_title else ""

    for word in query_words:
        if word in text_lower:
            score += 1.0
        if word in summary_lower:
            score += 2.0
        if a and any(word in tag.lower() for tag in a.tags):
            score += 2.0
        if word in card_lower:
            score += 1.5
        if a and any(word in cat.lower() for cat in a.categories):
            score += 1.0
        if a:
            for concept in a.entities.concepts:
                if word in concept.lower():
                    score += 0.5

    return score


# ── MCP Tools ────────────────────────────────────────────────────────────────

@mcp.tool()
def search_knowledge(query: str, limit: int = 10) -> list[dict]:
    """Search the knowledge base for items matching a query.

    Searches across tweet text, AI-generated summaries, tags, card titles, and
    entity concepts. Results are ranked by relevance then quality score.

    Args:
        query: Space-separated search terms (case-insensitive, all terms scored)
        limit: Maximum results to return (default 10, capped at 50)
    """
    root = _get_root()
    limit = min(max(1, limit), 50)
    query_words = set(query.lower().split())
    if not query_words:
        return []

    scored = []
    for item in iter_items(root):
        s = _score_item(item, query_words)
        if s > 0:
            scored.append((s, item))

    scored.sort(key=lambda x: (-x[0], -(x[1].analysis.quality_score if x[1].analysis else 0.0)))
    return [_item_summary(item) for _, item in scored[:limit]]


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

    Matches against all tech_refs subcategories: languages, frameworks, tools,
    packages, repos, hardware, and platforms. Matching is case-insensitive and
    substring-based (e.g. "torch" matches "pytorch").

    Args:
        tech:  Technology name to search for (e.g. "python", "react", "langchain")
        limit: Maximum results (default 10, capped at 50)
    """
    root = _get_root()
    limit = min(max(1, limit), 50)
    tech_lower = tech.lower()

    results = []
    for item in iter_items(root):
        if not item.analysis:
            continue
        refs = [r.lower() for r in item.analysis.tech_refs.all_refs()]
        if any(tech_lower in ref or ref in tech_lower for ref in refs):
            results.append(item)

    results.sort(key=lambda i: -(i.analysis.quality_score if i.analysis else 0.0))
    return [_item_summary(item) for item in results[:limit]]


@mcp.tool()
def find_by_author(handle: str, limit: int = 20) -> list[dict]:
    """Find all saved items from a specific Twitter/X author.

    Looks up items by exact handle match (case-insensitive). The leading '@'
    is stripped automatically if provided.

    Args:
        handle: Twitter/X handle, with or without '@' (e.g. "karpathy" or "@karpathy")
        limit:  Maximum results (default 20, capped at 100)
    """
    root = _get_root()
    limit = min(max(1, limit), 100)
    handle_lower = handle.lower().lstrip("@")

    results = [
        item for item in iter_items(root)
        if item.author_handle.lower() == handle_lower
    ]
    results.sort(key=lambda i: -(i.analysis.quality_score if i.analysis else 0.0))
    return [_item_summary(item) for item in results[:limit]]


@mcp.tool()
def find_by_category(category: str, limit: int = 20) -> list[dict]:
    """Find items belonging to a taxonomy category.

    Categories follow the hierarchical pattern "parent/child"
    (e.g. "ai_ml/llm_research", "devtools/debugging"). Passing just the parent
    prefix (e.g. "ai_ml") matches all subcategories beneath it. Matching is
    case-insensitive prefix-based.

    Args:
        category: Category path or parent prefix (e.g. "ai_ml", "ai_ml/llm_research")
        limit:    Maximum results (default 20, capped at 100)
    """
    root = _get_root()
    limit = min(max(1, limit), 100)
    cat_lower = category.lower()

    results = [
        item for item in iter_items(root)
        if item.analysis
        and any(c.lower().startswith(cat_lower) for c in item.analysis.categories)
    ]
    results.sort(key=lambda i: -(i.analysis.quality_score if i.analysis else 0.0))
    return [_item_summary(item) for item in results[:limit]]


# ── Entry point ───────────────────────────────────────────────────────────────

def run_server(root: Path) -> None:
    """Start the MCP server using stdio transport (for Claude Desktop / Claude Code)."""
    global _root
    _root = root
    mcp.run(transport="stdio")
