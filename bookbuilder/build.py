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

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config, load_taxonomy
from .store import iter_items


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
        })
    return records


# ── Static site ───────────────────────────────────────────────────────────────

_SITE_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: #0f0f0f; color: #e0e0e0; padding: 2rem; }
h1 { font-size: 1.6rem; margin-bottom: 1rem; color: #fff; }
#search { width: 100%; padding: .75rem 1rem; font-size: 1rem;
          background: #1a1a1a; border: 1px solid #333; color: #e0e0e0;
          border-radius: 6px; margin-bottom: 1.5rem; outline: none; }
#search:focus { border-color: #555; }
#stats { font-size: .8rem; color: #666; margin-bottom: 1rem; }
#results { display: grid; gap: .75rem; }
.card { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 8px;
        padding: 1rem; }
.card-header { display: flex; justify-content: space-between;
               align-items: baseline; gap: 1rem; margin-bottom: .4rem; }
.handle { font-weight: 600; color: #7eb8f7; font-size: .9rem; }
.date   { color: #555; font-size: .8rem; flex-shrink: 0; }
.summary { font-size: .9rem; line-height: 1.5; color: #ccc; margin-bottom: .5rem; }
.tags { display: flex; flex-wrap: wrap; gap: .3rem; margin-bottom: .4rem; }
.tag  { background: #222; color: #aaa; padding: .15rem .5rem;
        border-radius: 999px; font-size: .75rem; }
.tech-tag { background: #1a2a1a; color: #7ecf7e; }
.meta { display: flex; gap: 1rem; font-size: .78rem; color: #555; }
.meta a { color: #7eb8f7; text-decoration: none; }
.meta a:hover { text-decoration: underline; }
.score { margin-left: auto; }
"""

_SITE_JS = """
let allItems = [];
async function init() {
  const resp = await fetch('../knowledge/index.json');
  allItems = await resp.json();
  document.getElementById('stats').textContent =
    allItems.length.toLocaleString() + ' items loaded';
  render(allItems.slice(0, 50));

  // Fuse.js search
  const fuse = new Fuse(allItems, {
    keys: ['summary','text','handle','tags','tech','cats'],
    threshold: 0.35, includeScore: true,
  });
  const input = document.getElementById('search');
  input.addEventListener('input', () => {
    const q = input.value.trim();
    if (!q) { render(allItems.slice(0, 50)); return; }
    const results = fuse.search(q, { limit: 100 }).map(r => r.item);
    render(results);
  });
}

function render(items) {
  const el = document.getElementById('results');
  el.innerHTML = items.map(item => {
    const tags = (item.tags||[]).map(t => `<span class="tag">${t}</span>`).join('');
    const tech = (item.tech||[]).map(t => `<span class="tag tech-tag">${t}</span>`).join('');
    const cats = (item.cats||[]).join(', ');
    return `<div class="card">
      <div class="card-header">
        <span class="handle">@${item.handle||'?'}</span>
        <span class="date">${item.ts}</span>
      </div>
      <div class="summary">${item.summary || item.text}</div>
      <div class="tags">${tags}${tech}</div>
      <div class="meta">
        <span>${cats}</span>
        <a href="${item.url}" target="_blank">tweet →</a>
        <span class="score">${(item.score||0).toFixed(2)}</span>
      </div>
    </div>`;
  }).join('');
}

init();
"""

def _write_site(site_dir: Path, item_count: int) -> None:
    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "style.css").write_text(_SITE_CSS, encoding="utf-8")
    (site_dir / "search.js").write_text(_SITE_JS, encoding="utf-8")
    (site_dir / "index.html").write_text(f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Bookbuilder</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <h1>Bookbuilder</h1>
  <input id="search" type="search" placeholder="Search {item_count:,} items…" autofocus>
  <div id="stats"></div>
  <div id="results"></div>
  <script src="https://cdn.jsdelivr.net/npm/fuse.js/dist/fuse.min.js"></script>
  <script src="search.js"></script>
</body>
</html>
""", encoding="utf-8")


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
                _write_topic_digest(out_path, cat_label, sub_label, items, highlights_count, highlights_min)
        print(f"  wrote topic digests → knowledge/topics/")

    # ── Author digests ───────────────────────────────────────────────────────
    if build_outputs.get("author_digests", True):
        written = 0
        for handle, items in author_items.items():
            if len(items) < author_min_items:
                continue
            _write_author_digest(knowledge_dir / "authors" / f"{handle}.md", handle, items)
            written += 1
        print(f"  wrote {written} author digests → knowledge/authors/")

    # ── Tech registry ────────────────────────────────────────────────────────
    if build_outputs.get("tech_registry", True):
        for tech_name, subcat_items in tech_items.items():
            slug = _slugify(tech_name)
            _write_tech_page(knowledge_dir / "tech" / f"{slug}.md", tech_name, subcat_items)
        print(f"  wrote {len(tech_items)} tech pages → knowledge/tech/")

    # ── Search index ─────────────────────────────────────────────────────────
    if build_outputs.get("search_index", True):
        index = _build_search_index(all_items)
        idx_path = knowledge_dir / "index.json"
        idx_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        print(f"  wrote search index ({len(index)} records) → knowledge/index.json")

    # ── Static site ──────────────────────────────────────────────────────────
    if build_outputs.get("site", True):
        _write_site(site_dir, len(all_items))
        print(f"  wrote site → site/index.html")

    print("  build complete.")
