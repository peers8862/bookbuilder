"""
Export stage: generate a single long-form digest for reading or sharing.

Outputs:
  Markdown  — clean .md file, readable in any editor or Obsidian
  HTML      — self-contained print-ready HTML with inline CSS (no external deps)

Filters: --category, --min-score, --limit, --author, --cluster
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config, load_taxonomy
from .store import iter_items


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%b %-d, %Y")
    except Exception:
        return ts[:10] if ts else ""


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── Markdown export ───────────────────────────────────────────────────────────

def _export_markdown(items: list, title: str, meta: str) -> str:
    lines = [f"# {title}", "", f"_{meta}_", "", "---", ""]

    # Group by top-level category
    by_cat: dict[str, list] = defaultdict(list)
    for item in items:
        cats = item.analysis.categories if item.analysis else ["_uncategorized"]
        top = (cats[0] if cats else "_uncategorized").split("/")[0]
        by_cat[top].append(item)

    for cat_key, cat_items in sorted(by_cat.items(), key=lambda x: -len(x[1])):
        lines.append(f"## {cat_key.replace('_', ' ').title()} ({len(cat_items)})")
        lines.append("")
        for item in cat_items:
            a = item.analysis
            handle = item.author_handle or item.author_name or "unknown"
            summary = (a.summary if a and a.summary else item.text[:200]).strip()
            tags = "  ".join(f"`{t}`" for t in (a.tags[:4] if a else []))
            ts = _ts(item.timestamp)
            source_label = "source" if item.source in ("markdown", "text", "docx") else "tweet"
            lines += [
                f"### {handle} · {ts}",
                "",
                f"> {summary}",
                "",
                f"[{source_label}]({item.url})" + (f"  {tags}" if tags else ""),
                "",
                "---",
                "",
            ]

    lines += [
        "",
        f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_",
    ]
    return "\n".join(lines)


# ── HTML export ───────────────────────────────────────────────────────────────

_PRINT_CSS = """
  *, *::before, *::after { box-sizing: border-box; }
  body {
    font-family: Georgia, "Times New Roman", serif;
    font-size: 15px; line-height: 1.7;
    color: #1a1a1a; background: #fff;
    max-width: 780px; margin: 0 auto; padding: 3rem 2rem;
  }
  h1 { font-size: 2rem; font-weight: 700; margin-bottom: .3rem; }
  h2 { font-size: 1.25rem; font-weight: 600; margin: 2.5rem 0 1rem;
       padding-bottom: .4rem; border-bottom: 2px solid #e0e0e0; color: #333; }
  h3 { font-size: 1rem; font-weight: 600; color: #444; margin: 1.5rem 0 .3rem; }
  .meta { color: #888; font-size: .85rem; margin-bottom: 2rem; }
  blockquote {
    margin: .5rem 0 .5rem 0; padding: .6rem 1rem;
    border-left: 3px solid #ccc; color: #333;
    font-style: italic; background: #fafafa;
  }
  .tags { margin: .4rem 0; }
  .tag { display: inline-block; background: #f0f0f0; color: #555;
         padding: .1rem .5rem; border-radius: 3px; font-size: .78rem;
         font-family: monospace; margin-right: .3rem; }
  .item-link { font-size: .85rem; color: #0066cc; }
  .item-link a { color: #0066cc; }
  hr { border: none; border-top: 1px solid #e8e8e8; margin: 1.2rem 0; }
  .footer { margin-top: 3rem; color: #aaa; font-size: .8rem; text-align: center; }
  @media print {
    body { padding: 1rem; font-size: 11pt; }
    h2 { page-break-before: auto; }
    .item { page-break-inside: avoid; }
    a { color: inherit; text-decoration: none; }
  }
"""


def _export_html(items: list, title: str, meta: str) -> str:
    by_cat: dict[str, list] = defaultdict(list)
    for item in items:
        cats = item.analysis.categories if item.analysis else ["_uncategorized"]
        top = (cats[0] if cats else "_uncategorized").split("/")[0]
        by_cat[top].append(item)

    sections = []
    for cat_key, cat_items in sorted(by_cat.items(), key=lambda x: -len(x[1])):
        cat_label = cat_key.replace("_", " ").title()
        item_html = []
        for item in cat_items:
            a = item.analysis
            handle = _esc(item.author_handle or item.author_name or "unknown")
            summary = _esc((a.summary if a and a.summary else item.text[:300]).strip())
            tags_html = "".join(
                f'<span class="tag">{_esc(t)}</span>'
                for t in (a.tags[:5] if a else [])
            )
            ts = _ts(item.timestamp)
            source_label = "source" if item.source in ("markdown", "text", "docx") else "tweet"
            item_html.append(f"""<div class="item">
  <h3>{handle} <span style="font-weight:400;color:#888">&middot; {ts}</span></h3>
  <blockquote>{summary}</blockquote>
  <div class="tags">{tags_html}</div>
  <p class="item-link"><a href="{item.url}" target="_blank">{source_label} &rarr;</a></p>
  <hr>
</div>""")
        sections.append(
            f'<h2>{_esc(cat_label)} <span style="font-weight:400;color:#aaa">({len(cat_items)})</span></h2>\n'
            + "\n".join(item_html)
        )

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = "\n".join(sections)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{_esc(title)}</title>
  <style>{_PRINT_CSS}</style>
</head>
<body>
  <h1>{_esc(title)}</h1>
  <p class="meta">{_esc(meta)}</p>
  {body}
  <p class="footer">Generated {generated}</p>
</body>
</html>"""


# ── Main export runner ────────────────────────────────────────────────────────

def run_export(
    root: Path,
    output: Path | None = None,
    fmt: str = "md",
    category: str | None = None,
    author: str | None = None,
    cluster_id: str | None = None,
    min_score: float = 0.6,
    limit: int = 200,
) -> Path:
    """
    Generate a long-form export digest.

    Returns the path of the written file.
    """
    taxonomy = load_taxonomy(root)
    cfg = load_config(root)

    # Collect and filter items
    items: list = []
    for item in iter_items(root):
        if not item.analysis:
            continue
        a = item.analysis
        if a.quality_score < min_score:
            continue
        if category:
            cat_lower = category.lower()
            if not any(c.lower().startswith(cat_lower) for c in a.categories):
                continue
        if author:
            if item.author_handle.lower() != author.lower().lstrip("@"):
                continue
        if cluster_id:
            if a.cluster_id != cluster_id:
                continue
        items.append(item)

    # Sort by quality score descending, then cap
    items.sort(key=lambda x: x.analysis.quality_score, reverse=True)
    items = items[:limit]

    if not items:
        print("  No items matched the filters.")
        return Path()

    # Build title and meta line
    parts = []
    if category:
        tax_cats = taxonomy.get("categories", {})
        top = category.split("/")[0]
        cat_info = tax_cats.get(top, {})
        parts.append(cat_info.get("label", category))
    if author:
        parts.append(f"@{author.lstrip('@')}")
    if cluster_id:
        clusters_path = root / "system" / "clusters.json"
        if clusters_path.exists():
            clusters = json.loads(clusters_path.read_text(encoding="utf-8"))
            parts.append(clusters.get(cluster_id, {}).get("label", cluster_id))
    title = " · ".join(parts) if parts else "Knowledge Digest"
    meta = f"{len(items)} items · min score {min_score:.1f}"
    if category:
        meta += f" · category: {category}"

    # Render
    if fmt == "html":
        content = _export_html(items, title, meta)
        ext = ".html"
    else:
        content = _export_markdown(items, title, meta)
        ext = ".md"

    # Determine output path
    if output is None:
        slug = title.lower().replace(" ", "-").replace("·", "").replace("/", "-")
        slug = "".join(c for c in slug if c.isalnum() or c == "-").strip("-")
        output = root / f"export_{slug}{ext}"

    output.write_text(content, encoding="utf-8")
    print(f"  Exported {len(items)} items → {output}")
    return output
