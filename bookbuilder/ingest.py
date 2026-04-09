"""
Ingest stage: parse input files, normalise to RawItem, deduplicate,
write/update state/items.jsonl.

Supported sources:
  - Twitter/X JSON exports  (bookmark / like, two schema variants)
  - RSS / Atom feed XML
  - Pocket export HTML
  - Instapaper export CSV
  - Browser bookmark HTML (Netscape format)
  - Markdown files (.md)
  - Plain text files (.txt)
  - Word documents (.docx)
  - Single URL via ingest_url() (used by `bookbuilder add`)
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import csv
import urllib.request
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

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
    """Guess source type from filename and extension."""
    name = path.stem.lower()
    suffix = path.suffix.lower()
    if suffix in (".xml",):
        return "rss"
    if suffix == ".csv":
        return "instapaper"
    if suffix in (".md", ".markdown"):
        return "markdown"
    if suffix == ".txt":
        return "text"
    if suffix == ".docx":
        return "docx"
    if suffix == ".html" or suffix == ".htm":
        if "pocket" in name:
            return "pocket"
        return "bookmarks_html"
    if "bookmark" in name:
        return "bookmark"
    if "like" in name:
        return "like"
    return "unknown"


# ── URL-only item (for `bookbuilder add`) ─────────────────────────────────────

def _make_url_item(url: str) -> RawItem | None:
    url = url.strip()
    if not url:
        return None
    # Use a hash of the URL as the ID (no tweet ID available)
    item_id = hashlib.sha256(url.encode()).hexdigest()[:16]
    return RawItem(
        id=item_id,
        source="manual",
        url=url,
        text="",
        author_raw="",
        timestamp=datetime.now(timezone.utc).isoformat(),
        links=[],
        card_url="",
        card_title="",
        card_desc="",
        quote=None,
        content_hash=_content_hash(url, ""),
    )


# ── RSS / Atom parser ─────────────────────────────────────────────────────────

def _iter_rss(path: Path) -> Iterator[RawItem]:
    try:
        tree = ET.parse(path)
    except ET.ParseError as e:
        print(f"  [ingest] RSS parse error {path.name}: {e}")
        return
    root = tree.getroot()
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    # Atom feed
    for entry in root.findall(".//atom:entry", ns):
        url = ""
        for link in entry.findall("atom:link", ns):
            if link.get("rel", "alternate") == "alternate":
                url = link.get("href", "")
                break
        if not url:
            continue
        title = (entry.findtext("atom:title", "", ns) or "").strip()
        summary = (entry.findtext("atom:summary", "", ns) or "").strip()
        published = (entry.findtext("atom:published", "", ns) or "").strip()
        item_id = hashlib.sha256(url.encode()).hexdigest()[:16]
        yield RawItem(
            id=item_id, source="rss", url=url,
            text=f"{title}\n{summary}".strip(),
            author_raw="", timestamp=published,
            links=[], card_url="", card_title=title, card_desc=summary,
            quote=None, content_hash=_content_hash(url, title),
        )

    # RSS 2.0
    for item in root.findall(".//item"):
        url = (item.findtext("link") or "").strip()
        if not url:
            continue
        title = (item.findtext("title") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", item.findtext("description") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        item_id = hashlib.sha256(url.encode()).hexdigest()[:16]
        yield RawItem(
            id=item_id, source="rss", url=url,
            text=f"{title}\n{desc}".strip(),
            author_raw="", timestamp=pub,
            links=[], card_url="", card_title=title, card_desc=desc,
            quote=None, content_hash=_content_hash(url, title),
        )


# ── Pocket HTML parser ────────────────────────────────────────────────────────

class _PocketParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[RawItem] = []
        self._in_a = False
        self._current: dict = {}

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "a":
            d = dict(attrs)
            self._current = {
                "url": d.get("href", ""),
                "timestamp": d.get("time_added", ""),
                "tags": d.get("tags", ""),
            }
            self._in_a = True

    def handle_data(self, data: str) -> None:
        if self._in_a:
            self._current["title"] = data.strip()

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_a:
            url = self._current.get("url", "")
            if url:
                title = self._current.get("title", "")
                ts = self._current.get("timestamp", "")
                item_id = hashlib.sha256(url.encode()).hexdigest()[:16]
                self.items.append(RawItem(
                    id=item_id, source="pocket", url=url,
                    text=title, author_raw="", timestamp=ts,
                    links=[], card_url="", card_title=title, card_desc="",
                    quote=None, content_hash=_content_hash(url, title),
                ))
            self._in_a = False
            self._current = {}


def _iter_pocket(path: Path) -> Iterator[RawItem]:
    parser = _PocketParser()
    parser.feed(path.read_text(encoding="utf-8", errors="replace"))
    yield from parser.items


# ── Instapaper CSV parser ─────────────────────────────────────────────────────

def _iter_instapaper(path: Path) -> Iterator[RawItem]:
    # Columns: URL, Title, Selection, Folder
    with path.open(encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = (row.get("URL") or "").strip()
            if not url:
                continue
            title = (row.get("Title") or "").strip()
            selection = (row.get("Selection") or "").strip()
            item_id = hashlib.sha256(url.encode()).hexdigest()[:16]
            yield RawItem(
                id=item_id, source="instapaper", url=url,
                text=f"{title}\n{selection}".strip(),
                author_raw="", timestamp="",
                links=[], card_url="", card_title=title, card_desc=selection,
                quote=None, content_hash=_content_hash(url, title),
            )


# ── Browser bookmark HTML (Netscape format) ───────────────────────────────────

class _BookmarkHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[RawItem] = []
        self._in_a = False
        self._current: dict = {}

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "a":
            d = dict(attrs)
            self._current = {
                "url": d.get("href", ""),
                "timestamp": d.get("add_date", ""),
            }
            self._in_a = True

    def handle_data(self, data: str) -> None:
        if self._in_a:
            self._current["title"] = data.strip()

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_a:
            url = self._current.get("url", "")
            # Skip local/javascript/folder entries
            if url and url.startswith("http"):
                title = self._current.get("title", "")
                ts = self._current.get("timestamp", "")
                item_id = hashlib.sha256(url.encode()).hexdigest()[:16]
                self.items.append(RawItem(
                    id=item_id, source="bookmarks_html", url=url,
                    text=title, author_raw="", timestamp=ts,
                    links=[], card_url="", card_title=title, card_desc="",
                    quote=None, content_hash=_content_hash(url, title),
                ))
            self._in_a = False
            self._current = {}


def _iter_bookmarks_html(path: Path) -> Iterator[RawItem]:
    parser = _BookmarkHTMLParser()
    parser.feed(path.read_text(encoding="utf-8", errors="replace"))
    yield from parser.items


# ── Document parsers (.md, .txt, .docx) ──────────────────────────────────────
# Each file becomes one item. Headings are extracted and prepended as a
# structured outline so the AI sees document structure before body text.

def _md_heading_outline(text: str) -> str:
    """Extract markdown headings and return them as a compact outline string."""
    lines = []
    for line in text.splitlines():
        m = re.match(r'^(#{1,4})\s+(.*)', line)
        if m:
            depth = len(m.group(1)) - 1
            lines.append("  " * depth + m.group(2).strip())
    return "\n".join(lines)


def _make_doc_item(path: Path, source: str, title: str, outline: str, body: str,
                   max_chars: int = 20000) -> RawItem:
    """Build a RawItem from a local document file."""
    file_url = path.resolve().as_uri()
    item_id = hashlib.sha256(file_url.encode()).hexdigest()[:16]
    # Prepend outline so AI sees structure even if body is truncated
    full_text = (f"[Document outline]\n{outline}\n\n[Content]\n{body}".strip()
                 if outline else body)
    full_text = full_text[:max_chars]
    return RawItem(
        id=item_id, source=source, url=file_url,
        text=full_text, author_raw="",
        timestamp=datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).isoformat(),
        links=[], card_url="", card_title=title, card_desc="",
        quote=None, content_hash=_content_hash(file_url, full_text[:500]),
    )


def _iter_markdown(path: Path) -> Iterator[RawItem]:
    text = path.read_text(encoding="utf-8", errors="replace")
    # Title: first H1, or filename
    title_match = re.search(r'^#\s+(.+)', text, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else path.stem
    outline = _md_heading_outline(text)
    # Strip markdown heading markers from body for cleaner text
    body = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    yield _make_doc_item(path, "markdown", title, outline, body)


def _iter_text(path: Path) -> Iterator[RawItem]:
    text = path.read_text(encoding="utf-8", errors="replace")
    title = path.stem.replace("-", " ").replace("_", " ").title()
    yield _make_doc_item(path, "text", title, "", text)


def _iter_docx(path: Path) -> Iterator[RawItem]:
    try:
        from docx import Document  # python-docx
    except ImportError:
        print(f"  [ingest] python-docx not installed — skipping {path.name}")
        print("  Install with: pip install python-docx")
        return

    doc = Document(str(path))
    title = path.stem
    outline_lines: list[str] = []
    body_lines: list[str] = []

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style = para.style.name if para.style else ""
        if style.startswith("Heading"):
            # Extract heading level from style name ("Heading 1" → level 1)
            try:
                level = int(style.split()[-1]) - 1
            except (ValueError, IndexError):
                level = 0
            outline_lines.append("  " * level + text)
            body_lines.append(text)  # also include in body
        else:
            body_lines.append(text)
            # Use first non-heading paragraph as title if no heading found
            if title == path.stem and not outline_lines and len(text) < 120:
                title = text

    outline = "\n".join(outline_lines)
    body = "\n".join(body_lines)
    yield _make_doc_item(path, "docx", title, outline, body)


def iter_raw_items(path: Path, source: str | None = None) -> Iterator[RawItem]:
    """
    Yield normalised RawItems from a file.
    Dispatches to the appropriate parser based on file type.
    """
    src = source or _detect_source(path)

    if src == "rss":
        yield from _iter_rss(path)
        return
    if src == "pocket":
        yield from _iter_pocket(path)
        return
    if src == "instapaper":
        yield from _iter_instapaper(path)
        return
    if src == "bookmarks_html":
        yield from _iter_bookmarks_html(path)
        return
    if src == "markdown":
        yield from _iter_markdown(path)
        return
    if src == "text":
        yield from _iter_text(path)
        return
    if src == "docx":
        yield from _iter_docx(path)
        return

    # Default: Twitter/X JSON
    data = json.loads(path.read_text(encoding="utf-8"))
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


def ingest_url(url: str, state_dir: Path, root: Path) -> tuple[bool, str]:
    """
    Ingest a single URL directly (used by `bookbuilder add`).
    Returns (is_new, item_id).
    """
    from .models import Item
    from .store import write_item

    raw = _make_url_item(url)
    if not raw:
        return False, ""

    state_path = state_dir / "items.jsonl"
    states = load_state(state_path)

    if raw.id in states:
        return False, raw.id

    now = datetime.now(timezone.utc).isoformat()
    state = ItemState(
        item_id=raw.id, content_hash=raw.content_hash, source="manual",
        ingested_at=now, fetch_status="pending", analyze_status="pending",
    )
    states[raw.id] = state
    item = Item(
        id=raw.id, source="manual", url=raw.url, text="",
        author_name="", author_handle="", timestamp=now,
        state=state,
    )
    write_item(root, item)
    save_state(states, state_path)
    return True, raw.id


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
        # Tolerate old records missing newer fields
        d.setdefault("read_status", "unread")
        d.setdefault("read_at", "")
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
