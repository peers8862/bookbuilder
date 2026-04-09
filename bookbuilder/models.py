from __future__ import annotations

from dataclasses import dataclass, field


# ── Raw ingest models ─────────────────────────────────────────────────────────

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
    id: str
    source: str
    url: str
    text: str
    author_raw: str
    timestamp: str
    links: list[str]
    card_url: str
    card_title: str
    card_desc: str
    quote: QuoteRef | None
    content_hash: str

    @property
    def author_name(self) -> str:
        parts = self.author_raw.split("@")
        return parts[0].strip() if parts else self.author_raw

    @property
    def author_handle(self) -> str:
        if "@" in self.author_raw:
            return self.author_raw.split("@")[-1].strip()
        return ""


# ── Pipeline state ────────────────────────────────────────────────────────────

@dataclass
class ItemState:
    item_id: str
    content_hash: str
    source: str
    ingested_at: str
    fetch_status: str = "pending"
    analyze_status: str = "pending"
    fetched_at: str = ""
    analyzed_at: str = ""
    read_status: str = "unread"   # unread | read | want_to_read | archived
    read_at: str = ""


# ── Fetch models ──────────────────────────────────────────────────────────────

@dataclass
class FetchedPage:
    url: str
    title: str = ""
    text_path: str = ""
    word_count: int = 0
    fetched_at: str = ""
    status: str = "pending"


@dataclass
class FetchedImage:
    url: str
    path: str = ""
    fetched_at: str = ""
    status: str = "pending"


# ── Analysis models ───────────────────────────────────────────────────────────

@dataclass
class Entities:
    people: list[str] = field(default_factory=list)
    orgs: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)
    places: list[str] = field(default_factory=list)


@dataclass
class TechRefs:
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)
    repos: list[str] = field(default_factory=list)
    hardware: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any([
            self.languages, self.frameworks, self.tools,
            self.packages, self.repos, self.hardware, self.platforms,
        ])

    def all_refs(self) -> list[str]:
        return (
            self.languages + self.frameworks + self.tools +
            self.packages + self.repos + self.hardware + self.platforms
        )


@dataclass
class Analysis:
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    entities: Entities = field(default_factory=Entities)
    tech_refs: TechRefs = field(default_factory=TechRefs)
    categories: list[str] = field(default_factory=list)
    quality_score: float = 0.0
    cluster_id: str | None = None
    analyzed_at: str = ""
    model: str = ""


# ── Core item ─────────────────────────────────────────────────────────────────

@dataclass
class Item:
    id: str
    source: str
    url: str
    text: str
    author_name: str
    author_handle: str
    timestamp: str
    links: list[str] = field(default_factory=list)
    card_url: str = ""
    card_title: str = ""
    card_desc: str = ""
    quote: QuoteRef | None = None
    fetched_pages: list[FetchedPage] = field(default_factory=list)
    images: list[FetchedImage] = field(default_factory=list)
    analysis: Analysis | None = None
    state: ItemState | None = None

    @property
    def all_urls(self) -> list[str]:
        urls = list(self.links)
        if self.card_url:
            urls.append(self.card_url)
        return list(dict.fromkeys(u for u in urls if u))
