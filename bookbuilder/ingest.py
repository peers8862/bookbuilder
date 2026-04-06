"""
Ingest stage: parse input JSON files, normalise to RawItem, deduplicate,
write/update state/items.jsonl.

Handles two schema variants:
  - New (scraper merge() output):  links[], cardUrl, cardTitle, cardDesc
  - Old (earlier scraper version): textLinks[], extraLinks[], cardUrl, cardTitle, cardDesc
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .models import ItemState, QuoteRef, RawItem


# ── Schema normalisation ─────────────────────────────────────────────────────

def _extract_id(url: str) -> str:
    """Extract tweet ID from an x.com or twitter.com status URL."""
    m = re.search(r"/status/(\d+)", url)
    return m.group(1) if m else ""


def _normalise_links(raw: dict) -> list[str]:
    """
    Accept either format and return a single flat list of resolved URLs.
    Old format: textLinks + extraLinks
    New format: links
    """
    if "links" in raw:
        return [u for u in raw["links"] if u]
    text_links  = raw.get("textLinks") or []
    extra_links = raw.get("extraLinks") or []
    return [u for u in (text_links + extra_links) if u]


def _normalise_quote(q: dict | None) -> QuoteRef | None:
    if not q:
        return None
    return QuoteRef(
        url=q.get("url", ""),
        text=q.get("text", ""),
        author_raw=q.get("author", ""),
        timestamp=q.get("timestamp", ""),
        links=_normalise_links(q),
        card_url=q.get("cardUrl", ""),
        card_title=q.get("cardTitle", ""),
    )


def _content_hash(url: str, text: str) -> str:
    raw = (url + "|" + text).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def normalise(raw: dict, source: str) -> RawItem | None:
    """Convert one raw JSON record to a RawItem. Returns None if URL is missing."""
    url = raw.get("url", "").strip()
    if not url:
        return None
    tweet_id = _extract_id(url)
    if not tweet_id:
        return None

    text = raw.get("text", "").strip()
    return RawItem(
        id=tweet_id,
        source=source,
        url=url,
        text=text,
        author_raw=raw.get("author", ""),
        timestamp=raw.get("timestamp", ""),
        links=_normalise_links(raw),
        card_url=raw.get("cardUrl", ""),
        card_title=raw.get("cardTitle", ""),
        card_desc=raw.get("cardDesc", ""),
        quote=_normalise_quote(raw.get("quote")),
        content_hash=_content_hash(url, text),
    )


# ── File parsing ─────────────────────────────────────────────────────────────

def _detect_source(path: Path) -> str:
    """Guess source from filename if not provided."""
    name = path.stem.lower()
    if "bookmark" in name:
        return "bookmark"
    if "like" in name:
        return "like"
    return "unknown"


def iter_raw_items(path: Path, source: str | None = None) -> Iterator[RawItem]:
    """
    Yield normalised RawItems from a JSON file.
    The file may be a JSON array or a JSON object with a top-level list value.
    """
    src = source or _detect_source(path)
    data = json.loads(path.read_text(encoding="utf-8"))

    # Unwrap {"bookmarks": [...]} or {"data": [...]} style wrappers
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                data = v
                break
        else:
            data = []

    for raw in data:
        item = normalise(raw, src)
        if item:
            yield item


# ── State tracking ────────────────────────────────────────────────────────────

def load_state(state_path: Path) -> dict[str, ItemState]:
    """Load state/items.jsonl into a dict keyed by item_id."""
    states: dict[str, ItemState] = {}
    if not state_path.exists():
        return states
    for line in state_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        s = ItemState(**d)
        states[s.item_id] = s
    return states


def save_state(states: dict[str, ItemState], state_path: Path) -> None:
    """Write all states to state/items.jsonl (overwrites)."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(s.__dict__) for s in states.values()]
    state_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── Main ingest logic ─────────────────────────────────────────────────────────

def run_ingest(input_paths: list[Path], state_dir: Path, root: Path) -> tuple[int, int, int]:
    """
    Ingest all input files into state and write base item JSON files.
    Returns (new_items, updated_items, skipped_items).
    """
    from .models import Item
    from .store import write_item

    state_path = state_dir / "items.jsonl"
    states = load_state(state_path)
    now = datetime.now(timezone.utc).isoformat()

    new_count = updated_count = skipped_count = 0
    seen_hashes: dict[str, str] = {s.content_hash: s.item_id for s in states.values()}

    for path in input_paths:
        source = _detect_source(path)
        print(f"  ingesting {path.name} (source={source})")
        for raw in iter_raw_items(path):

            # Dedup by tweet ID
            if raw.id in states:
                existing = states[raw.id]
                if existing.content_hash == raw.content_hash:
                    skipped_count += 1
                    continue
                # Content changed — reset downstream stages
                existing.content_hash = raw.content_hash
                existing.fetch_status = "pending"
                existing.analyze_status = "pending"
                existing.fetched_at = ""
                existing.analyzed_at = ""
                updated_count += 1
                continue

            # Dedup by content hash across sources (same tweet bookmarked + liked)
            if raw.content_hash in seen_hashes:
                dup_id = seen_hashes[raw.content_hash]
                if dup_id in states and states[dup_id].source != "both":
                    states[dup_id].source = "both"
                skipped_count += 1
                continue

            # New item — create state record + base item JSON
            state = ItemState(
                item_id=raw.id,
                content_hash=raw.content_hash,
                source=raw.source,
                ingested_at=now,
                fetch_status="pending",
                analyze_status="pending",
            )
            states[raw.id] = state
            seen_hashes[raw.content_hash] = raw.id

            item = Item(
                id=raw.id,
                source=raw.source,
                url=raw.url,
                text=raw.text,
                author_name=raw.author_name,
                author_handle=raw.author_handle,
                timestamp=raw.timestamp,
                links=raw.links,
                card_url=raw.card_url,
                card_title=raw.card_title,
                card_desc=raw.card_desc,
                quote=raw.quote,
                state=state,
            )
            write_item(root, item)
            new_count += 1

    save_state(states, state_path)
    return new_count, updated_count, skipped_count
