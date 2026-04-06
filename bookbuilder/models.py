"""
Canonical data models for bookbuilder.

RawItem      — normalised from any input format (bookmarks or likes, old or new schema)
FetchedPage  — result of fetching a linked URL
Analysis     — AI analysis output
ItemState    — pipeline processing state per item
Item         — fully assembled record written to knowledge/items/{id}.json
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import re


# ── Input normalisation ──────────────────────────────────────────────────────

@dataclass
class QuoteRef:
    url: str = ""
    text: str = ""
    author_raw: str = ""
    timestamp: str = ""
    links: list[str] = field(default_factory=list)
    card_url: str = ""
    card_title: str = ""


@dataclass
class RawItem:
    """Unified representation of a single tweet, regardless of input schema version."""
    id: str                        # extracted from URL
    source: str                    # "bookmark" | "like" | "both"
    url: str
    text: str
    author_raw: str                # raw string e.g. "andy nguyen\n@kevinnguyendn\n·\nApr 3"
    timestamp: str                 # ISO-8601 string
    links: list[str]               # resolved non-t.co URLs
    card_url: str
    card_title: str
    card_desc: str
    quote: Optional[QuoteRef]
    content_hash: str              # sha256(url + text) for dedup

    @property
    def author_name(self) -> str:
        parts = self.author_raw.split("\n")
        return parts[0].strip() if parts else ""

    @property
    def author_handle(self) -> str:
        for part in self.author_raw.split("\n"):
            p = part.strip()
            if p.startswith("@"):
                return p.lstrip("@")
        return ""

    @property
    def all_urls(self) -> list[str]:
        """All fetchable URLs associated with this item."""
        urls = list(self.links)
        if self.card_url and self.card_url not in urls:
            urls.append(self.card_url)
        return urls


# ── Fetched content ──────────────────────────────────────────────────────────

@dataclass
class FetchedPage:
    url: str
    title: str = ""
    text_path: str = ""            # path to cache/pages/{hash}.txt
    word_count: int = 0
    fetched_at: str = ""
    status: str = "ok"             # ok | skipped | error | timeout | paywall


@dataclass
class FetchedImage:
    url: str
    path: str                      # cache/images/{item_id}_{n}.jpg
    fetched_at: str = ""
    status: str = "ok"


# ── AI analysis ──────────────────────────────────────────────────────────────

@dataclass
class TechRefs:
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)
    repos: list[str] = field(default_factory=list)
    hardware: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)

    def all_refs(self) -> list[str]:
        return (
            self.languages + self.frameworks + self.tools +
            self.packages + self.repos + self.hardware + self.platforms
        )

    def is_empty(self) -> bool:
        return not any([
            self.languages, self.frameworks, self.tools,
            self.packages, self.repos, self.hardware, self.platforms
        ])


@dataclass
class Entities:
    people: list[str] = field(default_factory=list)
    orgs: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)
    places: list[str] = field(default_factory=list)


@dataclass
class Analysis:
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    entities: Entities = field(default_factory=Entities)
    tech_refs: TechRefs = field(default_factory=TechRefs)
    categories: list[str] = field(default_factory=list)   # e.g. ["ai_ml/ai_tools"]
    quality_score: float = 0.0
    cluster_id: Optional[str] = None
    analyzed_at: str = ""
    model: str = ""


# ── Processing state ─────────────────────────────────────────────────────────

@dataclass
class ItemState:
    item_id: str
    content_hash: str
    source: str
    ingested_at: str = ""
    fetched_at: str = ""
    analyzed_at: str = ""
    clustered_at: str = ""
    pipeline_version: str = "1.0"
    # Stage status values: pending | ok | error | skipped
    fetch_status: str = "pending"
    analyze_status: str = "pending"


# ── Assembled item (written to knowledge/items/) ─────────────────────────────

@dataclass
class Item:
    # Core
    id: str
    source: str
    url: str
    text: str
    author_name: str
    author_handle: str
    timestamp: str
    links: list[str]
    card_url: str
    card_title: str
    card_desc: str
    quote: Optional[QuoteRef]

    # Enriched
    fetched_pages: list[FetchedPage] = field(default_factory=list)
    images: list[FetchedImage] = field(default_factory=list)
    analysis: Optional[Analysis] = None
    state: Optional[ItemState] = None

    @property
    def primary_category(self) -> str:
        if self.analysis and self.analysis.categories:
            return self.analysis.categories[0]
        return "_uncategorized"

    @property
    def has_tech(self) -> bool:
        return self.analysis is not None and not self.analysis.tech_refs.is_empty()
