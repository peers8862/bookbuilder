"""
Obsidian vault export.

Writes a folder structure openable directly as an Obsidian vault:

  {vault}/
    _index.md              — vault home with stats and navigation
    topics/
      {cat}/{subcat}.md    — topic digest with frontmatter + wikilinks
    authors/
      {handle}.md          — author digest with frontmatter
    items/
      {id}.md              — one note per item (optional, --no-items to skip)
    clusters/
      {id}.md              — cluster synthesis if available

All notes have YAML frontmatter. Cross-references use [[wikilinks]].
Tags use Obsidian's #tag syntax.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config, load_taxonomy
from .store import iter_items


# ── Helpers ───────────────────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower().strip())
    return re.sub(r"[\s_]+", "-", text)


def _ts(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ts[:10] if ts else ""


def _frontmatter(fields: dict) -> str:
    lines = ["---"]
    for k, v in fields.items():
        if isinstance(v, list):
            if not v:
                lines.append(f"{k}: []")
            else:
                lines.append(f"{k}:")
                for item in v:
                    lines.append(f"  - {item}")
        elif isinstance(v, str) and ("\n" in v or ":" in v or v.startswith('"')):
            escaped = v.replace('"', '\\"')
            lines.append(f'{k}: "{escaped}"')
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


def _wikilink(text: str, alias: str | None = None) -> str:
    if alias:
        return f"[[{text}|{alias}]]"
    return f"[[{text}]]"


def _obs_tags(tags: list[str]) -> str:
    return "  ".join(f"#{t.replace('-', '_')}" for t in tags)


# ── Item notes ────────────────────────────────────────────────────────────────

def _write_item_note(vault: Path, item) -> Path:
    a = item.analysis
    handle = item.author_handle or item.author_name or "unknown"
    title = (a.summary[:60] if a and a.summary else item.text[:60]).strip()
    title = re.sub(r'[\\/*?"<>|:]', "", title)  # strip chars invalid in filenames

    fm = _frontmatter({
        "id":          item.id,
        "source":      item.source,
        "url":         item.url,
        "author":      handle,
        "date":        _ts(item.timestamp),
        "tags":        [t.replace("-", "_") for t in (a.tags if a else [])],
        "categories":  a.categories if a else [],
        "quality":     round(a.quality_score, 2) if a else 0.0,
        "cluster":     a.cluster_id or "",
        "read_status": item.state.read_status if item.state else "unread",
    })

    lines = [fm, "", f"# {title}", ""]
    if a and a.summary:
        lines += [f"> {a.summary}", ""]

    lines += [f"**Source:** [{item.url}]({item.url})", ""]

    if a and a.tags:
        lines += [_obs_tags(a.tags), ""]

    if a and not a.tech_refs.is_empty():
        tech = ", ".join(a.tech_refs.all_refs()[:8])
        lines += [f"**Tech:** {tech}", ""]

    if a and a.entities.people:
        lines += [f"**People:** {', '.join(a.entities.people)}", ""]

    if a and a.categories:
        cat_links = "  ".join(_wikilink(f"topics/{c.replace('/', '/')}") for c in a.categories)
        lines += [f"**Topics:** {cat_links}", ""]

    if a and a.cluster_id:
        lines += [f"**Cluster:** {_wikilink(f'clusters/{a.cluster_id}')}", ""]

    if item.text and item.text != (a.summary if a else ""):
        lines += ["", "## Original text", "", item.text[:1000]]

    out = vault / "items" / f"{item.id}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


# ── Topic digests ─────────────────────────────────────────────────────────────

def _write_topic_note(vault: Path, cat_key: str, sub_key: str,
                      cat_label: str, sub_label: str, items: list,
                      write_items: bool) -> None:
    sorted_items = sorted(
        items, key=lambda x: x.analysis.quality_score if x.analysis else 0, reverse=True
    )
    top = sorted_items[:20]

    fm = _frontmatter({
        "category":    cat_key,
        "subcategory": sub_key,
        "label":       sub_label,
        "item_count":  len(items),
        "tags":        [cat_key, sub_key],
    })

    lines = [fm, "", f"# {sub_label}", "", f"*{cat_label} · {len(items)} items*", ""]

    for item in top:
        a = item.analysis
        handle = item.author_handle or "unknown"
        summary = (a.summary[:120] if a and a.summary else item.text[:80].replace("\n", " "))
        score = f"{a.quality_score:.2f}" if a else "—"
        date = _ts(item.timestamp)
        if write_items:
            item_link = _wikilink(f"items/{item.id}", f"@{handle} · {date}")
        else:
            item_link = f"[@{handle}]({item.url}) · {date}"
        lines += [f"- {item_link} — {summary} `{score}`"]

    out = vault / "topics" / cat_key / f"{sub_key}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")


# ── Author digests ────────────────────────────────────────────────────────────

def _write_author_note(vault: Path, handle: str, items: list,
                       write_items: bool) -> None:
    sorted_items = sorted(
        items, key=lambda x: x.analysis.quality_score if x.analysis else 0, reverse=True
    )
    name = sorted_items[0].author_name if sorted_items else handle
    scores = [i.analysis.quality_score for i in items if i.analysis]
    avg_q = sum(scores) / len(scores) if scores else 0.0

    # Collect all tags this author uses
    all_tags: list[str] = []
    for item in items:
        if item.analysis:
            all_tags.extend(item.analysis.tags)
    from collections import Counter
    top_tags = [t for t, _ in Counter(all_tags).most_common(8)]

    fm = _frontmatter({
        "handle":     handle,
        "name":       name,
        "item_count": len(items),
        "avg_quality": round(avg_q, 2),
        "tags":       top_tags,
        "profile":    f"https://x.com/{handle}",
    })

    lines = [fm, "", f"# {name}", "",
             f"[@{handle}](https://x.com/{handle}) · {len(items)} saved · avg quality {avg_q:.2f}",
             ""]

    for item in sorted_items[:30]:
        a = item.analysis
        summary = (a.summary[:100] if a and a.summary else item.text[:80].replace("\n", " "))
        date = _ts(item.timestamp)
        score = f"{a.quality_score:.2f}" if a else "—"
        if write_items:
            ref = _wikilink(f"items/{item.id}", f"{date}")
        else:
            ref = f"[{date}]({item.url})"
        lines.append(f"- {ref} — {summary} `{score}`")

    out = vault / "authors" / f"{handle}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")


# ── Cluster notes ─────────────────────────────────────────────────────────────

def _write_cluster_note(vault: Path, cluster_id: str, info: dict,
                        items: list, knowledge_dir: Path) -> None:
    label = info.get("label", cluster_id)
    parent = info.get("parent", "")

    fm = _frontmatter({
        "cluster_id":  cluster_id,
        "label":       label,
        "parent":      parent,
        "item_count":  len(items),
        "tags":        [parent, "cluster"],
    })

    lines = [fm, "", f"# {label}", "", f"*{parent} · {len(items)} items*", ""]

    # Include agent synthesis if available
    synthesis_path = knowledge_dir / "clusters" / f"{cluster_id}.md"
    if synthesis_path.exists():
        raw = synthesis_path.read_text(encoding="utf-8")
        # Strip the items list section — just keep the synthesis prose
        synthesis = raw.split("## Items")[0].strip()
        # Remove the H1 title (already have it above)
        synthesis = re.sub(r"^#[^#].*\n", "", synthesis, count=1).strip()
        if synthesis:
            lines += [synthesis, ""]

    lines.append("## Items")
    sorted_items = sorted(
        items, key=lambda x: x.analysis.quality_score if x.analysis else 0, reverse=True
    )
    for item in sorted_items[:20]:
        a = item.analysis
        handle = item.author_handle or "unknown"
        summary = (a.summary[:100] if a and a.summary else item.text[:80].replace("\n", " "))
        score = f"{a.quality_score:.2f}" if a else "—"
        lines.append(f"- {_wikilink(f'items/{item.id}', f'@{handle}')} — {summary} `{score}`")

    out = vault / "clusters" / f"{cluster_id}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")


# ── Vault index ───────────────────────────────────────────────────────────────

def _write_index(vault: Path, stats: dict) -> None:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "---",
        "tags: [bookbuilder, index]",
        f"generated: {generated}",
        "---",
        "",
        "# bookbuilder vault",
        "",
        f"*{stats['items']} items · {stats['topics']} topics · "
        f"{stats['authors']} authors · {stats['clusters']} clusters*",
        "",
        "## Navigate",
        "",
        "- **Topics** — [[topics/index|Browse by topic]]",
        "- **Authors** — [[authors/index|Browse by author]]",
        "- **Clusters** — [[clusters/index|Browse by cluster]]",
        "",
        "## Quick stats",
        "",
        f"- Total items: {stats['items']}",
        f"- Analyzed: {stats['analyzed']}",
        f"- Avg quality score: {stats['avg_quality']:.2f}",
        f"- Unread: {stats['unread']}",
        f"- Want to read: {stats['want_to_read']}",
        "",
        f"*Generated {generated}*",
    ]
    (vault / "_index.md").write_text("\n".join(lines), encoding="utf-8")


def _write_section_index(vault: Path, section: str, entries: list[tuple[str, str, int]]) -> None:
    """Write topics/index.md, authors/index.md, clusters/index.md."""
    lines = [f"# {section.title()}", ""]
    for name, path, count in sorted(entries, key=lambda x: -x[2]):
        lines.append(f"- {_wikilink(path, name)} ({count})")
    out = vault / section / "index.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")


# ── Main export runner ────────────────────────────────────────────────────────

def run_obsidian_export(
    root: Path,
    vault_path: Path | None = None,
    write_items: bool = True,
    min_score: float = 0.0,
) -> Path:
    taxonomy = load_taxonomy(root)
    tax_cats = taxonomy.get("categories", {})
    knowledge_dir = root / "knowledge"

    vault = vault_path or (root / "vault")
    vault.mkdir(parents=True, exist_ok=True)
    for d in ("topics", "authors", "clusters", "items"):
        (vault / d).mkdir(exist_ok=True)

    # Load clusters
    clusters: dict = {}
    clusters_path = root / "system" / "clusters.json"
    if clusters_path.exists():
        clusters = json.loads(clusters_path.read_text(encoding="utf-8"))

    # Group items
    topic_items: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    author_items: dict[str, list] = defaultdict(list)
    cluster_items: dict[str, list] = defaultdict(list)
    all_items = []
    analyzed = unread = want_to_read = 0
    scores: list[float] = []

    for item in iter_items(root):
        if item.analysis and item.analysis.quality_score < min_score:
            continue
        all_items.append(item)
        if item.analysis:
            analyzed += 1
            scores.append(item.analysis.quality_score)
        if item.state:
            if item.state.read_status == "unread":
                unread += 1
            elif item.state.read_status == "want_to_read":
                want_to_read += 1

        if item.author_handle:
            author_items[item.author_handle].append(item)

        cats = item.analysis.categories if item.analysis else ["_uncategorized"]
        for cat_path in (cats or ["_uncategorized"]):
            parts = cat_path.split("/")
            topic_items[parts[0]][parts[1] if len(parts) > 1 else "_all"].append(item)

        if item.analysis and item.analysis.cluster_id:
            cluster_items[item.analysis.cluster_id].append(item)

    # Write item notes
    if write_items:
        for item in all_items:
            _write_item_note(vault, item)

    # Write topic notes
    topic_entries: list[tuple[str, str, int]] = []
    for cat_key, subcats in topic_items.items():
        cat_info = tax_cats.get(cat_key, {})
        cat_label = cat_info.get("label", cat_key)
        for sub_key, items in subcats.items():
            sub_info = cat_info.get("subcategories", {}).get(sub_key, {})
            sub_label = sub_info.get("label", sub_key)
            _write_topic_note(vault, cat_key, sub_key, cat_label, sub_label,
                              items, write_items)
            topic_entries.append((f"{cat_label} / {sub_label}",
                                  f"topics/{cat_key}/{sub_key}", len(items)))
    _write_section_index(vault, "topics", topic_entries)

    # Write author notes
    author_entries: list[tuple[str, str, int]] = []
    for handle, items in author_items.items():
        _write_author_note(vault, handle, items, write_items)
        author_entries.append((f"@{handle}", f"authors/{handle}", len(items)))
    _write_section_index(vault, "authors", author_entries)

    # Write cluster notes
    cluster_entries: list[tuple[str, str, int]] = []
    for cid, info in clusters.items():
        if cid == "_noise":
            continue
        items = cluster_items.get(cid, [])
        _write_cluster_note(vault, cid, info, items, knowledge_dir)
        cluster_entries.append((info.get("label", cid), f"clusters/{cid}", len(items)))
    _write_section_index(vault, "clusters", cluster_entries)

    # Write vault index
    _write_index(vault, {
        "items":       len(all_items),
        "analyzed":    analyzed,
        "topics":      len(topic_entries),
        "authors":     len(author_entries),
        "clusters":    len(cluster_entries),
        "avg_quality": sum(scores) / len(scores) if scores else 0.0,
        "unread":      unread,
        "want_to_read": want_to_read,
    })

    print(f"  vault written to {vault}")
    print(f"  {len(all_items)} items · {len(topic_entries)} topics · "
          f"{len(author_entries)} authors · {len(cluster_entries)} clusters")
    return vault
