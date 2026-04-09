"""
Build stage: generate knowledge/ markdown files and site/ static HTML.

Outputs:
  knowledge/items/{id}.json          — already written by prior stages
  knowledge/topics/{cat}/{subcat}.md — topic digest (highlights + full archive)
  knowledge/authors/{handle}.md      — per-author digest
  knowledge/tech/{slug}.md           — one file per discovered tech tool/package/repo
  knowledge/index.json               — Fuse.js search index
  site/index.html                    — search interface
  site/topic/*.html                  — topic pages
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config, load_taxonomy
from .store import iter_items


# ── Build manifest ────────────────────────────────────────────────────────────

def _fingerprint(items: list) -> str:
    """Stable 16-char hash of a set of items based on id + last-modified timestamp."""
    parts = sorted(
        f"{item.id}:{item.analysis.analyzed_at if item.analysis else (item.state.ingested_at if item.state else '')}"
        for item in items
    )
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


class _Manifest:
    """
    Tracks per-output-file fingerprints so unchanged files can be skipped.
    Persisted to state/build_manifest.json.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, str] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        self.written = 0
        self.skipped = 0

    def needs_update(self, out_path: Path, items: list) -> bool:
        """Return True (and record new fingerprint) if items have changed since last build."""
        key = str(out_path)
        fp = _fingerprint(items)
        if self._data.get(key) == fp:
            self.skipped += 1
            return False
        self._data[key] = fp
        self.written += 1
        return True

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8"
        )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text


def _ts_display(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%b %-d, %Y")
    except Exception:
        return ts[:10] if ts else ""


def _tweet_url(item) -> str:
    return item.url


def _author_link(handle: str) -> str:
    return f"https://x.com/{handle}"


# ── Topic digests ─────────────────────────────────────────────────────────────

def _write_topic_digest(
    path: Path,
    category_label: str,
    subcategory_label: str,
    items: list,
    highlights_count: int,
    highlights_min_score: float,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    lines.append(f"# {subcategory_label}\n")
    lines.append(f"_Category: {category_label}_\n")
    lines.append(f"_{len(items)} items total_\n")
    lines.append("")

    # Sort by quality score descending
    sorted_items = sorted(items, key=lambda x: (x.analysis.quality_score if x.analysis else 0), reverse=True)
    highlights = [i for i in sorted_items if i.analysis and i.analysis.quality_score >= highlights_min_score][:highlights_count]

    if highlights:
        lines.append(f"## Highlights ({len(highlights)})\n")
        for item in highlights:
            a = item.analysis
            handle = item.author_handle or "unknown"
            lines.append(f"### [{handle}]({_author_link(handle)}) · {_ts_display(item.timestamp)}")
            lines.append("")
            if a and a.summary:
                lines.append(f"> {a.summary}")
                lines.append("")
            lines.append(f"[Tweet]({_tweet_url(item)})")
            if item.fetched_pages:
                for page in item.fetched_pages:
                    if page.status == "ok" and page.title:
                        lines.append(f" · [{page.title}]({page.url})")
            if a and a.tags:
                lines.append("")
                lines.append("`" + "`  `".join(a.tags) + "`")
            if a and not a.tech_refs.is_empty():
                tech_all = a.tech_refs.all_refs()
                lines.append(f"  **Tech:** {', '.join(tech_all)}")
            lines.append("")
            lines.append("---")
            lines.append("")

    # All items (compact)
    lines.append(f"## All Items ({len(sorted_items)})\n")
    for item in sorted_items:
        handle = item.author_handle or "unknown"
        a = item.analysis
        summary = (a.summary[:100] + "…") if a and a.summary else item.text[:80].replace("\n", " ")
        score = f"{a.quality_score:.2f}" if a else "—"
        lines.append(f"- **[@{handle}]({_author_link(handle)})** {_ts_display(item.timestamp)} · _{summary}_ · [→]({_tweet_url(item)}) `{score}`")

    path.write_text("\n".join(lines), encoding="utf-8")


# ── Author digests ────────────────────────────────────────────────────────────

def _write_author_digest(path: Path, handle: str, items: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    name = items[0].author_name if items else handle
    lines.append(f"# {name} (@{handle})\n")
    lines.append(f"[Profile]({_author_link(handle)}) · {len(items)} saved items\n")
    lines.append("")

    sorted_items = sorted(items, key=lambda x: x.timestamp or "", reverse=True)
    for item in sorted_items:
        a = item.analysis
        summary = (a.summary[:120] + "…") if a and a.summary else item.text[:100].replace("\n", " ")
        tags = ("`" + "`  `".join(a.tags) + "`") if a and a.tags else ""
        lines.append(f"- {_ts_display(item.timestamp)} — _{summary}_ {tags} [→]({_tweet_url(item)})")

    path.write_text("\n".join(lines), encoding="utf-8")


# ── Tech registry ─────────────────────────────────────────────────────────────

def _write_tech_page(path: Path, tech_name: str, items_by_subcat: dict[str, list]) -> None:
    """One markdown page per tool/language/repo, listing all items that reference it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    total = sum(len(v) for v in items_by_subcat.values())
    lines: list[str] = [f"# {tech_name}\n", f"_{total} items reference this_\n", ""]

    for subcat, items in sorted(items_by_subcat.items()):
        if not items:
            continue
        lines.append(f"## {subcat.title()} ({len(items)})\n")
        for item in sorted(items, key=lambda x: x.timestamp or "", reverse=True):
            a = item.analysis
            summary = (a.summary[:100] + "…") if a and a.summary else item.text[:80].replace("\n", " ")
            handle = item.author_handle or "unknown"
            lines.append(f"- **[@{handle}]({_author_link(handle)})** {_ts_display(item.timestamp)} — _{summary}_ [→]({_tweet_url(item)})")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


# ── Search index ──────────────────────────────────────────────────────────────

def _build_search_index(items: list) -> list[dict]:
    records = []
    for item in items:
        a = item.analysis
        records.append({
            "id":       item.id,
            "url":      item.url,
            "handle":   item.author_handle,
            "ts":       item.timestamp[:10] if item.timestamp else "",
            "text":     item.text[:200],
            "summary":  a.summary if a else "",
            "tags":     a.tags if a else [],
            "cats":     a.categories if a else [],
            "tech":     a.tech_refs.all_refs() if a else [],
            "score":    a.quality_score if a else 0.0,
            "source":   item.source,
            "read":     item.state.read_status if item.state else "unread",
        })
    return records


# ── Static site ───────────────────────────────────────────────────────────────

_SHARED_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: #0f0f0f; color: #e0e0e0; padding: 0; }
a { color: #7eb8f7; text-decoration: none; }
a:hover { text-decoration: underline; }

/* Nav */
nav { background: #111; border-bottom: 1px solid #222; padding: .6rem 2rem;
      display: flex; gap: 1.5rem; align-items: center; }
nav .brand { font-weight: 700; color: #fff; font-size: 1rem; }
nav a { color: #aaa; font-size: .85rem; }
nav a:hover { color: #fff; }

.page { padding: 2rem; max-width: 960px; margin: 0 auto; }
h1 { font-size: 1.5rem; color: #fff; margin-bottom: .4rem; }
h2 { font-size: 1.1rem; color: #ccc; margin: 1.5rem 0 .6rem; border-bottom: 1px solid #222; padding-bottom: .3rem; }
h3 { font-size: .95rem; color: #bbb; margin: 1rem 0 .3rem; }
.meta-line { font-size: .8rem; color: #555; margin-bottom: 1.2rem; }

/* Search */
#search { width: 100%; padding: .75rem 1rem; font-size: 1rem;
          background: #1a1a1a; border: 1px solid #333; color: #e0e0e0;
          border-radius: 6px; margin-bottom: 1rem; outline: none; }
#search:focus { border-color: #555; }
#stats { font-size: .8rem; color: #666; margin-bottom: 1rem; }

/* Cards */
#results, .card-grid { display: grid; gap: .75rem; }
.card { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 8px; padding: 1rem; }
.card-header { display: flex; justify-content: space-between; align-items: baseline;
               gap: 1rem; margin-bottom: .4rem; }
.handle { font-weight: 600; color: #7eb8f7; font-size: .9rem; }
.date   { color: #555; font-size: .8rem; flex-shrink: 0; }
.summary { font-size: .9rem; line-height: 1.5; color: #ccc; margin-bottom: .5rem; }
.tags { display: flex; flex-wrap: wrap; gap: .3rem; margin-bottom: .4rem; }
.tag  { background: #222; color: #aaa; padding: .15rem .5rem;
        border-radius: 999px; font-size: .75rem; }
.tech-tag { background: #1a2a1a; color: #7ecf7e; }
.card-meta { display: flex; gap: 1rem; font-size: .78rem; color: #555; }
.card-meta a { color: #7eb8f7; }
.score { margin-left: auto; }

/* Index grids */
.topic-grid, .author-grid, .cluster-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: .6rem;
  margin-top: .8rem;
}
.tile { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 6px;
        padding: .75rem 1rem; }
.tile h3 { font-size: .9rem; color: #e0e0e0; margin: 0 0 .2rem; }
.tile .count { font-size: .75rem; color: #555; }
.tile a { display: block; }
"""

_NAV = """<nav>
  <span class="brand">bookbuilder</span>
  <a href="/index.html">Search</a>
  <a href="/topics.html">Topics</a>
  <a href="/authors.html">Authors</a>
  <a href="/clusters.html">Clusters</a>
</nav>"""


def _html_page(title: str, body: str, extra_head: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title} — bookbuilder</title>
  <link rel="stylesheet" href="/style.css">
  {extra_head}
</head>
<body>
{_NAV}
<div class="page">
{body}
</div>
</body>
</html>"""


def _card_html(item, show_source_link: bool = True) -> str:
    a = item.analysis
    handle = item.author_handle or "unknown"
    author_url = f"https://x.com/{handle}" if item.source not in ("markdown", "text", "docx") else item.url
    summary = (a.summary if a and a.summary else item.text[:200]).replace("<", "&lt;").replace(">", "&gt;")
    tags_html = "".join(f'<span class="tag">{t}</span>' for t in (a.tags[:5] if a else []))
    tech_html = "".join(f'<span class="tag tech-tag">{t}</span>' for t in (a.tech_refs.all_refs()[:4] if a else []))
    score = f"{a.quality_score:.2f}" if a else "—"
    ts = _ts_display(item.timestamp)
    source_link = f'<a href="{item.url}" target="_blank">source →</a>' if show_source_link else ""
    cats = ", ".join(a.categories[:2]) if a and a.categories else ""
    return f"""<div class="card">
  <div class="card-header">
    <span class="handle"><a href="/authors/{handle}.html">@{handle}</a></span>
    <span class="date">{ts}</span>
  </div>
  <div class="summary">{summary}</div>
  <div class="tags">{tags_html}{tech_html}</div>
  <div class="card-meta">
    <span>{cats}</span>
    {source_link}
    <span class="score">{score}</span>
  </div>
</div>"""


def _write_site(
    site_dir: Path,
    all_items: list,
    topic_items: dict,
    author_items: dict,
    clusters: dict,
    cluster_items: dict,
    taxonomy: dict,
    highlights_count: int,
    highlights_min: float,
) -> None:
    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "topics").mkdir(exist_ok=True)
    (site_dir / "authors").mkdir(exist_ok=True)
    (site_dir / "clusters").mkdir(exist_ok=True)

    (site_dir / "style.css").write_text(_SHARED_CSS, encoding="utf-8")

    tax_cats = taxonomy.get("categories", {})

    # ── Search index page ────────────────────────────────────────────────────
    search_js = """
let allItems = [];
async function init() {
  const resp = await fetch('/knowledge/index.json');
  allItems = await resp.json();
  document.getElementById('stats').textContent = allItems.length.toLocaleString() + ' items';
  render(allItems.slice(0, 50));
  const fuse = new Fuse(allItems, {
    keys: ['summary','text','handle','tags','tech','cats'], threshold: 0.35, includeScore: true,
  });
  document.getElementById('search').addEventListener('input', e => {
    const q = e.target.value.trim();
    render(q ? fuse.search(q, {limit:100}).map(r=>r.item) : allItems.slice(0,50));
  });
}
function render(items) {
  document.getElementById('results').innerHTML = items.map(item => {
    const tags = (item.tags||[]).map(t=>`<span class="tag">${t}</span>`).join('');
    const tech = (item.tech||[]).map(t=>`<span class="tag tech-tag">${t}</span>`).join('');
    return `<div class="card">
      <div class="card-header">
        <span class="handle"><a href="/authors/${item.handle}.html">@${item.handle||'?'}</a></span>
        <span class="date">${item.ts}</span>
      </div>
      <div class="summary">${item.summary||item.text}</div>
      <div class="tags">${tags}${tech}</div>
      <div class="card-meta">
        <span>${(item.cats||[]).join(', ')}</span>
        <a href="${item.url}" target="_blank">source →</a>
        <span class="score">${(item.score||0).toFixed(2)}</span>
      </div></div>`;
  }).join('');
}
init();"""
    (site_dir / "search.js").write_text(search_js, encoding="utf-8")
    body = f"""<h1>bookbuilder</h1>
<p class="meta-line">{len(all_items):,} items</p>
<input id="search" type="search" placeholder="Search {len(all_items):,} items…" autofocus>
<div id="stats"></div>
<div id="results"></div>
<script src="https://cdn.jsdelivr.net/npm/fuse.js/dist/fuse.min.js"></script>
<script src="/search.js"></script>"""
    (site_dir / "index.html").write_text(
        _html_page("Search", body), encoding="utf-8"
    )

    # ── Topics index ─────────────────────────────────────────────────────────
    tiles = []
    for cat_key, subcats in sorted(topic_items.items()):
        cat_info = tax_cats.get(cat_key, {})
        cat_label = cat_info.get("label", cat_key)
        for sub_key, items in sorted(subcats.items(), key=lambda x: -len(x[1])):
            sub_info = cat_info.get("subcategories", {}).get(sub_key, {})
            sub_label = sub_info.get("label", sub_key)
            slug = f"{cat_key}__{sub_key}"
            tiles.append(
                f'<div class="tile"><a href="/topics/{slug}.html">'
                f'<h3>{sub_label}</h3>'
                f'<span class="count">{cat_label} · {len(items)} items</span>'
                f'</a></div>'
            )
    topics_body = f'<h1>Topics</h1><p class="meta-line">{len(tiles)} topic digests</p>'
    topics_body += '<div class="topic-grid">' + "".join(tiles) + "</div>"
    (site_dir / "topics.html").write_text(_html_page("Topics", topics_body), encoding="utf-8")

    # ── Topic detail pages ────────────────────────────────────────────────────
    for cat_key, subcats in topic_items.items():
        cat_info = tax_cats.get(cat_key, {})
        cat_label = cat_info.get("label", cat_key)
        for sub_key, items in subcats.items():
            sub_info = cat_info.get("subcategories", {}).get(sub_key, {})
            sub_label = sub_info.get("label", sub_key)
            slug = f"{cat_key}__{sub_key}"
            sorted_items = sorted(
                items, key=lambda x: x.analysis.quality_score if x.analysis else 0, reverse=True
            )
            highlights = [
                i for i in sorted_items
                if i.analysis and i.analysis.quality_score >= highlights_min
            ][:highlights_count]
            cards = "".join(_card_html(i) for i in highlights)
            rest_items = sorted_items[len(highlights):]
            rest_rows = "".join(
                f'<li><a href="{i.url}" target="_blank">{(i.analysis.summary[:90] if i.analysis and i.analysis.summary else i.text[:80]).replace("<","&lt;")}…</a> '
                f'<span style="color:#555">@{i.author_handle or "?"} · {_ts_display(i.timestamp)}</span></li>'
                for i in rest_items
            )
            body = (
                f'<h1>{sub_label}</h1>'
                f'<p class="meta-line"><a href="/topics.html">← Topics</a> · {cat_label} · {len(items)} items</p>'
                f'<h2>Highlights</h2><div class="card-grid">{cards}</div>'
                + (f'<h2>All items ({len(sorted_items)})</h2><ul style="color:#aaa;line-height:1.8">{rest_rows}</ul>' if rest_rows else "")
            )
            (site_dir / "topics" / f"{slug}.html").write_text(
                _html_page(sub_label, body), encoding="utf-8"
            )

    # ── Authors index ─────────────────────────────────────────────────────────
    import json as _json
    author_data = []
    for handle, items in author_items.items():
        scores = [i.analysis.quality_score for i in items if i.analysis]
        avg_q = sum(scores) / len(scores) if scores else 0.0
        name = next((i.author_name for i in items if i.author_name), handle)
        author_data.append({"handle": handle, "name": name,
                            "count": len(items), "avg_q": avg_q})

    _btn = ("padding:.35rem .8rem;background:#222;border:1px solid #333;"
            "color:#aaa;border-radius:4px;font-size:.8rem;cursor:pointer")
    sort_controls = (
        '<div style="display:flex;gap:.5rem;align-items:center;margin-bottom:1rem;flex-wrap:wrap">'
        '<input id="author-search" type="search" placeholder="Filter authors…" '
        'style="padding:.4rem .8rem;background:#1a1a1a;border:1px solid #333;color:#e0e0e0;'
        'border-radius:4px;font-size:.85rem;outline:none;flex:1;min-width:160px">'
        + f'<button class="sort-btn active" data-sort="count" style="{_btn}">Most saved</button>'
        + f'<button class="sort-btn" data-sort="quality" style="{_btn}">Highest quality</button>'
        + f'<button class="sort-btn" data-sort="alpha" style="{_btn}">A – Z</button>'
        + '</div>'
    )
    _js_template = 'const authors = AUTHOR_JSON;\nfunction render() {\n  const q = document.getElementById(\'author-search\').value.trim().toLowerCase();\n  let filtered = authors.filter(a =>\n    !q || a.handle.toLowerCase().includes(q) || a.name.toLowerCase().includes(q)\n  );\n  const sort = document.querySelector(\'.sort-btn.active\')?.dataset.sort || \'count\';\n  filtered.sort((a, b) =>\n    sort === \'alpha\'   ? a.handle.localeCompare(b.handle) :\n    sort === \'quality\' ? b.avg_q - a.avg_q :\n    b.count - a.count\n  );\n  document.getElementById(\'author-count\').textContent = filtered.length + \' authors\';\n  document.getElementById(\'author-grid\').innerHTML = filtered.map(a =>\n    \'<div class="tile"><a href="/authors/\' + a.handle + \'.html">\' +\n    \'<h3>@\' + a.handle + \'</h3>\' +\n    \'<span class="count">\' + a.count + \' items &middot; avg \' + a.avg_q.toFixed(2) + \'</span>\' +\n    \'</a></div>\'\n  ).join(\'\');\n}\ndocument.getElementById(\'author-search\').addEventListener(\'input\', render);\ndocument.querySelectorAll(\'.sort-btn\').forEach(btn => {\n  btn.addEventListener(\'click\', () => {\n    document.querySelectorAll(\'.sort-btn\').forEach(b => b.classList.remove(\'active\'));\n    btn.classList.add(\'active\'); render();\n  });\n});\nrender();\n'
    authors_js = _js_template.replace('AUTHOR_JSON', _json.dumps(author_data))
    authors_body = (
        f'<h1>Authors</h1>'
        f'<p class="meta-line" id="author-count">{len(author_data)} authors</p>'
        + sort_controls
        + '<div class="author-grid" id="author-grid"></div>'
        + f'<script>{authors_js}</script>'
    )
    (site_dir / "authors.html").write_text(_html_page("Authors", authors_body), encoding="utf-8")

    for d in author_data:
        handle = d["handle"]
        items = author_items[handle]
        sorted_items = sorted(
            items, key=lambda x: x.analysis.quality_score if x.analysis else 0, reverse=True
        )
        cards = "".join(_card_html(i) for i in sorted_items[:30])
        avg_q = d["avg_q"]
        body = (
            f'<h1>{d["name"]}</h1>'
            f'<p class="meta-line"><a href="/authors.html">← Authors</a> · '
            f'<a href="https://x.com/{handle}" target="_blank">@{handle}</a> · '
            f'{len(items)} saved items · avg quality {avg_q:.2f}</p>'
            f'<div class="card-grid">{cards}</div>'
        )
        (site_dir / "authors" / f"{handle}.html").write_text(
            _html_page(f"@{handle}", body), encoding="utf-8"
        )


    # ── Clusters index ────────────────────────────────────────────────────────
    cluster_tiles = []
    for cid, info in sorted(clusters.items(), key=lambda x: -x[1].get("item_count", 0)):
        if cid == "_noise":
            continue
        label = info.get("label", cid)
        count = info.get("item_count", len(cluster_items.get(cid, [])))
        parent = info.get("parent", "")
        cluster_tiles.append(
            f'<div class="tile"><a href="/clusters/{cid}.html">'
            f'<h3>{label}</h3>'
            f'<span class="count">{parent} · {count} items</span>'
            f'</a></div>'
        )
    clusters_body = f'<h1>Clusters</h1><p class="meta-line">{len(cluster_tiles)} topic clusters</p>'
    clusters_body += '<div class="cluster-grid">' + "".join(cluster_tiles) + "</div>"
    (site_dir / "clusters.html").write_text(_html_page("Clusters", clusters_body), encoding="utf-8")

    # ── Cluster detail pages ──────────────────────────────────────────────────
    for cid, info in clusters.items():
        if cid == "_noise":
            continue
        label = info.get("label", cid)
        parent = info.get("parent", "")
        items = cluster_items.get(cid, [])
        sorted_items = sorted(
            items, key=lambda x: x.analysis.quality_score if x.analysis else 0, reverse=True
        )
        # Agent synthesis if available
        synthesis_path = site_dir.parent / "knowledge" / "clusters" / f"{cid}.md"
        synthesis_html = ""
        if synthesis_path.exists():
            raw_md = synthesis_path.read_text(encoding="utf-8")
            # Render the synthesis section (everything before the items list)
            synth_text = raw_md.split("## Items")[0].strip()
            # Simple markdown → html for the synthesis block
            synth_text = re.sub(r'^#{1,3}\s+(.+)$', r'<h3>\1</h3>', synth_text, flags=re.MULTILINE)
            synth_text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', synth_text)
            synth_text = synth_text.replace("\n\n", "</p><p>").replace("\n", " ")
            synthesis_html = f'<div style="background:#161616;border:1px solid #2a2a2a;border-radius:6px;padding:1rem;margin-bottom:1.5rem"><p>{synth_text}</p></div>'
        cards = "".join(_card_html(i) for i in sorted_items[:20])
        body = (
            f'<h1>{label}</h1>'
            f'<p class="meta-line"><a href="/clusters.html">← Clusters</a> · {parent} · {len(items)} items</p>'
            + synthesis_html
            + f'<h2>Top items</h2><div class="card-grid">{cards}</div>'
        )
        (site_dir / "clusters" / f"{cid}.html").write_text(
            _html_page(label, body), encoding="utf-8"
        )


# ── Main build runner ─────────────────────────────────────────────────────────

def run_build(root: Path, force: bool = False) -> None:
    cfg = load_config(root)
    taxonomy = load_taxonomy(root)
    out_cfg = cfg.get("output", {})
    highlights_count = int(out_cfg.get("topic_highlights_count", 20))
    highlights_min   = float(out_cfg.get("topic_highlights_min_score", 0.6))
    author_min_items = int(out_cfg.get("author_min_items", 3))
    pipeline_cfg = {}
    try:
        import yaml
        pipeline_cfg = yaml.safe_load((root / "system" / "pipeline.yaml").read_text()) or {}
    except Exception:
        pass
    build_outputs = pipeline_cfg.get("stages", {}).get("build", {}).get("outputs", {})

    knowledge_dir = root / "knowledge"
    site_dir = root / out_cfg.get("site_dir", "site")

    manifest = _Manifest(root / "state" / "build_manifest.json")

    all_items = list(iter_items(root))
    print(f"  building from {len(all_items)} items")

    # ── Group by category ────────────────────────────────────────────────────
    # topic_items[cat_key][subcat_key] = [items]
    topic_items: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    author_items: dict[str, list] = defaultdict(list)
    # tech_items[tech_name][subcat_type] = [items]
    tech_items: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))

    for item in all_items:
        # Author
        if item.author_handle:
            author_items[item.author_handle].append(item)

        # Topics
        cats = item.analysis.categories if item.analysis else ["_uncategorized"]
        if not cats:
            cats = ["_uncategorized"]
        for cat_path in cats:
            parts = cat_path.split("/")
            cat_key = parts[0]
            sub_key = parts[1] if len(parts) > 1 else "_all"
            topic_items[cat_key][sub_key].append(item)

        # Tech refs
        if item.analysis and not item.analysis.tech_refs.is_empty():
            tr = item.analysis.tech_refs
            for subcat, names in [
                ("languages",  tr.languages),
                ("frameworks", tr.frameworks),
                ("tools",      tr.tools),
                ("packages",   tr.packages),
                ("repos",      tr.repos),
                ("hardware",   tr.hardware),
                ("platforms",  tr.platforms),
            ]:
                for name in names:
                    tech_items[name][subcat].append(item)

    tax_cats = taxonomy.get("categories", {})

    # ── Topic digests ────────────────────────────────────────────────────────
    if build_outputs.get("topic_digests", True):
        for cat_key, subcats in topic_items.items():
            cat_info = tax_cats.get(cat_key, {})
            cat_label = cat_info.get("label", cat_key)
            for sub_key, items in subcats.items():
                sub_info = cat_info.get("subcategories", {}).get(sub_key, {})
                sub_label = sub_info.get("label", sub_key)
                out_path = knowledge_dir / "topics" / cat_key / f"{sub_key}.md"
                if force or manifest.needs_update(out_path, items):
                    _write_topic_digest(out_path, cat_label, sub_label, items, highlights_count, highlights_min)
        print(f"  wrote topic digests → knowledge/topics/")

    # ── Author digests ───────────────────────────────────────────────────────
    if build_outputs.get("author_digests", True):
        written = 0
        for handle, items in author_items.items():
            if len(items) < author_min_items:
                continue
            out_path = knowledge_dir / "authors" / f"{handle}.md"
            if force or manifest.needs_update(out_path, items):
                _write_author_digest(out_path, handle, items)
                written += 1
        print(f"  wrote {written} author digests → knowledge/authors/")

    # ── Tech registry ────────────────────────────────────────────────────────
    if build_outputs.get("tech_registry", True):
        written_tech = 0
        for tech_name, subcat_items in tech_items.items():
            slug = _slugify(tech_name)
            out_path = knowledge_dir / "tech" / f"{slug}.md"
            all_tech_items = [i for lst in subcat_items.values() for i in lst]
            if force or manifest.needs_update(out_path, all_tech_items):
                _write_tech_page(out_path, tech_name, subcat_items)
                written_tech += 1
        print(f"  wrote {written_tech} tech pages → knowledge/tech/")

    # ── Search index ─────────────────────────────────────────────────────────
    if build_outputs.get("search_index", True):
        idx_path = knowledge_dir / "index.json"
        if force or manifest.needs_update(idx_path, all_items):
            index = _build_search_index(all_items)
            idx_path.parent.mkdir(parents=True, exist_ok=True)
            idx_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
            print(f"  wrote search index ({len(index)} records) → knowledge/index.json")
        else:
            print(f"  search index unchanged, skipped")

    # ── Static site ──────────────────────────────────────────────────────────
    if build_outputs.get("site", True):
        site_sentinel = site_dir / "index.html"
        if force or manifest.needs_update(site_sentinel, all_items):
            # Build cluster_items map for site
            from collections import defaultdict as _dd
            cluster_items: dict[str, list] = _dd(list)
            for item in all_items:
                if item.analysis and item.analysis.cluster_id:
                    cluster_items[item.analysis.cluster_id].append(item)
            clusters: dict = {}
            clusters_path = root / "system" / "clusters.json"
            if clusters_path.exists():
                import json as _json
                clusters = _json.loads(clusters_path.read_text(encoding="utf-8"))
            _write_site(
                site_dir, all_items, topic_items, author_items,
                clusters, dict(cluster_items), taxonomy,
                highlights_count, highlights_min,
            )
            print(f"  wrote site → site/")
        else:
            print(f"  site unchanged, skipped")

    manifest.save()
    print(f"  build complete. wrote={manifest.written} skipped={manifest.skipped}")
