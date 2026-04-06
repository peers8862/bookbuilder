"""
Core search logic for the bookbuilder knowledge base.

Used by both the CLI `bookbuilder search` command and the MCP server tools.
"""

from __future__ import annotations

from pathlib import Path

from .models import Item
from .store import iter_items


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_item(item: Item, query_words: set[str]) -> float:
    """Score an item against a set of query words. Higher = more relevant."""
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


# ── Filters ───────────────────────────────────────────────────────────────────

def matches_category(item: Item, category: str) -> bool:
    cat_lower = category.lower()
    return (
        item.analysis is not None
        and any(c.lower().startswith(cat_lower) for c in item.analysis.categories)
    )


def matches_author(item: Item, handle: str) -> bool:
    return item.author_handle.lower() == handle.lower().lstrip("@")


def matches_tech(item: Item, tech: str) -> bool:
    if not item.analysis:
        return False
    tech_lower = tech.lower()
    return any(
        tech_lower in ref.lower() or ref.lower() in tech_lower
        for ref in item.analysis.tech_refs.all_refs()
    )


# ── Main search entry point ───────────────────────────────────────────────────

def search_items(
    root: Path,
    query: str,
    *,
    limit: int = 10,
    min_quality: float = 0.0,
    category: str | None = None,
    author: str | None = None,
    tech: str | None = None,
) -> list[tuple[float, Item]]:
    """
    Search the knowledge base and return ranked (score, item) pairs.

    Args:
        root:        Project root (contains knowledge/items/).
        query:       Space-separated search terms. Empty string matches all items.
        limit:       Maximum results to return.
        min_quality: Minimum quality_score threshold (0.0 = no filter).
        category:    Restrict to items whose category path starts with this prefix.
        author:      Restrict to items from this Twitter/X handle.
        tech:        Restrict to items referencing this technology (substring match).
    """
    query_words = set(query.lower().split()) if query.strip() else set()
    results: list[tuple[float, Item]] = []

    for item in iter_items(root):
        # Quality gate
        qs = item.analysis.quality_score if item.analysis else 0.0
        if qs < min_quality:
            continue

        # Structural filters
        if category and not matches_category(item, category):
            continue
        if author and not matches_author(item, author):
            continue
        if tech and not matches_tech(item, tech):
            continue

        # Relevance score
        if query_words:
            s = score_item(item, query_words)
            if s == 0:
                continue
        else:
            # No query text — list mode, rank by quality
            s = qs

        results.append((s, item))

    results.sort(key=lambda x: (-x[0], -(x[1].analysis.quality_score if x[1].analysis else 0.0)))
    return results[:limit]
