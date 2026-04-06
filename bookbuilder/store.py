"""
Read and write Item records to knowledge/items/{id}.json.

Each file is the single source of truth for a processed item — ingest writes
the base record, and each subsequent pipeline stage enriches it in place.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Iterator

from .models import (
    Analysis, Entities, FetchedImage, FetchedPage,
    Item, ItemState, QuoteRef, TechRefs,
)


# ── Serialise ────────────────────────────────────────────────────────────────

def _item_to_dict(item: Item) -> dict:
    def _opt(obj) -> dict | None:
        return asdict(obj) if obj is not None else None

    return {
        "id":            item.id,
        "source":        item.source,
        "url":           item.url,
        "text":          item.text,
        "author_name":   item.author_name,
        "author_handle": item.author_handle,
        "timestamp":     item.timestamp,
        "links":         item.links,
        "card_url":      item.card_url,
        "card_title":    item.card_title,
        "card_desc":     item.card_desc,
        "quote":         _opt(item.quote),
        "fetched_pages": [asdict(p) for p in item.fetched_pages],
        "images":        [asdict(img) for img in item.images],
        "analysis":      _analysis_to_dict(item.analysis),
        "state":         _opt(item.state),
    }


def _analysis_to_dict(a: Analysis | None) -> dict | None:
    if a is None:
        return None
    return {
        "summary":       a.summary,
        "tags":          a.tags,
        "entities":      asdict(a.entities),
        "tech_refs":     asdict(a.tech_refs),
        "categories":    a.categories,
        "quality_score": a.quality_score,
        "cluster_id":    a.cluster_id,
        "analyzed_at":   a.analyzed_at,
        "model":         a.model,
    }


# ── Deserialise ──────────────────────────────────────────────────────────────

def _analysis_from_dict(d: dict | None) -> Analysis | None:
    if not d:
        return None
    ent = d.get("entities") or {}
    tech = d.get("tech_refs") or {}
    return Analysis(
        summary=d.get("summary", ""),
        tags=d.get("tags", []),
        entities=Entities(**ent) if ent else Entities(),
        tech_refs=TechRefs(**tech) if tech else TechRefs(),
        categories=d.get("categories", []),
        quality_score=float(d.get("quality_score", 0.0)),
        cluster_id=d.get("cluster_id"),
        analyzed_at=d.get("analyzed_at", ""),
        model=d.get("model", ""),
    )


def _item_from_dict(d: dict) -> Item:
    quote_d = d.get("quote")
    quote = QuoteRef(**quote_d) if quote_d else None

    fetched = [FetchedPage(**p) for p in (d.get("fetched_pages") or [])]
    images  = [FetchedImage(**img) for img in (d.get("images") or [])]
    state_d = d.get("state")
    state   = ItemState(**state_d) if state_d else None

    return Item(
        id=d["id"],
        source=d.get("source", "unknown"),
        url=d.get("url", ""),
        text=d.get("text", ""),
        author_name=d.get("author_name", ""),
        author_handle=d.get("author_handle", ""),
        timestamp=d.get("timestamp", ""),
        links=d.get("links", []),
        card_url=d.get("card_url", ""),
        card_title=d.get("card_title", ""),
        card_desc=d.get("card_desc", ""),
        quote=quote,
        fetched_pages=fetched,
        images=images,
        analysis=_analysis_from_dict(d.get("analysis")),
        state=state,
    )


# ── Public API ───────────────────────────────────────────────────────────────

def items_dir(root: Path) -> Path:
    d = root / "knowledge" / "items"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_item(root: Path, item: Item) -> None:
    path = items_dir(root) / f"{item.id}.json"
    path.write_text(json.dumps(_item_to_dict(item), indent=2, ensure_ascii=False), encoding="utf-8")


def read_item(root: Path, item_id: str) -> Item | None:
    path = items_dir(root) / f"{item_id}.json"
    if not path.exists():
        return None
    return _item_from_dict(json.loads(path.read_text(encoding="utf-8")))


def iter_items(root: Path) -> Iterator[Item]:
    for path in sorted(items_dir(root).glob("*.json")):
        try:
            yield _item_from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"  [store] error reading {path.name}: {e}")
