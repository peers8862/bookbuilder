"""
Fetch stage: download linked article text and tweet images.

For each pending item:
  - Resolves all URLs (links[] + cardUrl)
  - Skips domains in skip_domains.txt
  - Extracts readable article text via readability-lxml
  - Saves text to cache/pages/{url_hash}.txt
  - Downloads images to cache/images/{item_id}_{n}.ext
  - Updates knowledge/items/{id}.json with fetched_pages + images
  - Updates state/items.jsonl fetch_status
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import is_skip_domain, load_config, load_skip_domains
from .ingest import load_state, save_state
from .models import FetchedImage, FetchedPage, ItemState
from .store import iter_items, read_item, write_item

# Image URL patterns found in tweet text / card URLs
_IMAGE_EXTENSIONS = frozenset([".jpg", ".jpeg", ".png", ".gif", ".webp"])
_TWEET_IMAGE_PATTERN = re.compile(r"https://pbs\.twimg\.com/[^\s\"'>]+")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


# ── Text extraction ──────────────────────────────────────────────────────────

def _extract_text(html: str, url: str) -> tuple[str, str]:
    """
    Return (title, plain_text) from HTML using readability-lxml.
    Falls back to basic tag stripping on failure.
    """
    try:
        from readability import Document
        doc = Document(html)
        title = doc.title() or ""
        summary_html = doc.summary()
        # Strip tags from readability output
        text = re.sub(r"<[^>]+>", " ", summary_html)
        text = re.sub(r"\s{2,}", " ", text).strip()
        return title, text
    except Exception:
        # Fallback: strip all tags
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s{2,}", " ", text).strip()
        return "", text[:5000]


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:20]


def _is_image_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in _IMAGE_EXTENSIONS)


# ── Single URL fetch ─────────────────────────────────────────────────────────

async def _fetch_page(
    client: httpx.AsyncClient,
    url: str,
    cache_dir: Path,
    max_chars: int,
    timeout: float,
) -> FetchedPage:
    url_hash = _url_hash(url)
    cache_path = cache_dir / "pages" / f"{url_hash}.txt"
    meta_path  = cache_dir / "pages" / f"{url_hash}.meta.json"
    now = datetime.now(timezone.utc).isoformat()

    # Cache hit
    if cache_path.exists() and meta_path.exists():
        import json
        meta = json.loads(meta_path.read_text())
        return FetchedPage(
            url=url,
            title=meta.get("title", ""),
            text_path=str(cache_path),
            word_count=meta.get("word_count", 0),
            fetched_at=meta.get("fetched_at", ""),
            status="ok",
        )

    try:
        resp = await client.get(url, timeout=timeout, follow_redirects=True)
        if resp.status_code == 200:
            content_type = resp.headers.get("content-type", "")
            if "text/html" not in content_type and "text/plain" not in content_type:
                return FetchedPage(url=url, status="skipped", fetched_at=now)

            html = resp.text
            title, text = _extract_text(html, url)
            text = text[:max_chars]
            word_count = len(text.split())

            cache_dir.joinpath("pages").mkdir(parents=True, exist_ok=True)
            cache_path.write_text(text, encoding="utf-8")

            import json
            meta = {"title": title, "word_count": word_count, "fetched_at": now, "url": url}
            meta_path.write_text(json.dumps(meta), encoding="utf-8")

            return FetchedPage(
                url=url,
                title=title,
                text_path=str(cache_path),
                word_count=word_count,
                fetched_at=now,
                status="ok",
            )
        elif resp.status_code in (401, 403, 402):
            return FetchedPage(url=url, status="paywall", fetched_at=now)
        else:
            return FetchedPage(url=url, status=f"http_{resp.status_code}", fetched_at=now)

    except httpx.TimeoutException:
        return FetchedPage(url=url, status="timeout", fetched_at=now)
    except Exception as e:
        return FetchedPage(url=url, status=f"error:{type(e).__name__}", fetched_at=now)


async def _fetch_image(
    client: httpx.AsyncClient,
    url: str,
    save_path: Path,
    max_kb: int,
    timeout: float,
) -> FetchedImage:
    now = datetime.now(timezone.utc).isoformat()
    if save_path.exists():
        return FetchedImage(url=url, path=str(save_path), fetched_at=now, status="ok")
    try:
        resp = await client.get(url, timeout=timeout, follow_redirects=True)
        if resp.status_code == 200:
            if len(resp.content) > max_kb * 1024:
                return FetchedImage(url=url, path="", fetched_at=now, status="too_large")
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_bytes(resp.content)
            return FetchedImage(url=url, path=str(save_path), fetched_at=now, status="ok")
        return FetchedImage(url=url, path="", fetched_at=now, status=f"http_{resp.status_code}")
    except Exception as e:
        return FetchedImage(url=url, path="", fetched_at=now, status=f"error:{type(e).__name__}")


# ── Image URL discovery ──────────────────────────────────────────────────────

def _find_image_urls(item) -> list[str]:
    """Extract tweet image URLs from pbs.twimg.com patterns in text or links."""
    found: list[str] = []
    for text in [item.text, item.card_url] + item.links:
        if not text:
            continue
        found += _TWEET_IMAGE_PATTERN.findall(text)
    # Also check direct image links
    for url in item.links:
        if _is_image_url(url):
            found.append(url)
    return list(dict.fromkeys(found))  # deduplicate preserving order


# ── Main fetch runner ────────────────────────────────────────────────────────

async def _run_fetch_async(root: Path, force: bool, workers: int,
                          stale_after_days: int | None = None) -> tuple[int, int, int]:
    cfg          = load_config(root)
    skip_domains = load_skip_domains(root)
    fetch_cfg    = cfg.get("fetch", {})
    timeout      = float(fetch_cfg.get("timeout_s", 15))
    max_chars    = int(fetch_cfg.get("max_content_chars", 20000))
    max_img_kb   = int(fetch_cfg.get("image_max_kb", 500))
    fetch_images = bool(fetch_cfg.get("fetch_images", True))

    cache_dir  = root / "cache"
    state_path = root / "state" / "items.jsonl"
    states     = load_state(state_path)

    # Determine stale cutoff
    stale_cutoff: str | None = None
    if stale_after_days is not None:
        from datetime import timedelta
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=stale_after_days)
        stale_cutoff = cutoff_dt.isoformat()

    sem = asyncio.Semaphore(workers)
    ok_count = skip_count = err_count = 0

    async with httpx.AsyncClient(headers=HEADERS, http2=True) as client:

        async def process_item(item_id: str, state: ItemState):
            nonlocal ok_count, skip_count, err_count

            already_fetched = state.fetch_status == "ok"
            is_stale = (
                stale_cutoff is not None
                and already_fetched
                and state.fetched_at
                and state.fetched_at < stale_cutoff
            )
            if not force and already_fetched and not is_stale:
                skip_count += 1
                return
            if is_stale:
                state.fetch_status = "pending"  # reset so it re-fetches

            item = read_item(root, item_id)
            if item is None:
                return

            now = datetime.now(timezone.utc).isoformat()
            fetched_pages: list[FetchedPage] = []
            fetched_images: list[FetchedImage] = []

            # Collect URLs to fetch
            page_urls = [u for u in item.all_urls if not is_skip_domain(u, skip_domains) and not _is_image_url(u)]

            async with sem:
                # Pages
                for url in page_urls:
                    page = await _fetch_page(client, url, cache_dir, max_chars, timeout)
                    fetched_pages.append(page)

                # Images
                if fetch_images:
                    img_urls = _find_image_urls(item)
                    for n, img_url in enumerate(img_urls[:5]):  # cap at 5 images per tweet
                        ext = Path(urlparse(img_url).path).suffix or ".jpg"
                        save_path = cache_dir / "images" / f"{item_id}_{n}{ext}"
                        img = await _fetch_image(client, img_url, save_path, max_img_kb, timeout)
                        fetched_images.append(img)

            item.fetched_pages = fetched_pages
            item.images = fetched_images
            state.fetch_status = "ok"
            state.fetched_at = now
            if item.state:
                item.state.fetch_status = "ok"
                item.state.fetched_at = now
            write_item(root, item)

            any_error = any(p.status not in ("ok", "skipped", "paywall") for p in fetched_pages)
            if any_error:
                err_count += 1
            else:
                ok_count += 1

        tasks = [process_item(iid, s) for iid, s in states.items()]
        # Process in chunks to show progress
        chunk = 50
        for i in range(0, len(tasks), chunk):
            await asyncio.gather(*tasks[i:i + chunk])
            done = min(i + chunk, len(tasks))
            print(f"  fetched {done}/{len(tasks)} items  ok={ok_count} err={err_count} skip={skip_count}")

    save_state(states, state_path)
    return ok_count, err_count, skip_count


def run_fetch(root: Path, force: bool = False, workers: int | None = None,
              stale_after_days: int | None = None) -> tuple[int, int, int]:
    cfg = load_config(root)
    w = workers or cfg.get("fetch", {}).get("workers", 8)
    return asyncio.run(_run_fetch_async(root, force, w, stale_after_days=stale_after_days))
