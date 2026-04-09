"""
highlight_curator agent

For each topic digest, asks Claude to select and rank the genuinely best items
to feature — not just top-N by quality score. Writes a curated highlights block
to knowledge/highlights/{cat}/{subcat}.md which build.py can include.

This replaces the mechanical score-sorted highlights section with editorial picks.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from ..config import load_config, load_taxonomy
from ..store import iter_items
from .base import Agent


_CURATOR_MARKER = "<!-- highlight_curator -->"


class HighlightCuratorAgent(Agent):
    name = "highlight_curator"

    def run(self, force: bool = False) -> dict:
        cfg = load_config(self.root)
        agent_cfg = cfg.get("agents", {}).get("highlight_curator", {})
        pool_size = int(agent_cfg.get("pool_size", 20))    # items sent to Claude
        pick_count = int(agent_cfg.get("pick_count", 5))   # items Claude selects
        min_items = int(agent_cfg.get("min_items", 6))

        taxonomy = load_taxonomy(self.root)
        tax_cats = taxonomy.get("categories", {})
        template = self.prompt_template()

        topic_items: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        for item in iter_items(self.root):
            cats = item.analysis.categories if item.analysis else ["_uncategorized"]
            for cat_path in (cats or ["_uncategorized"]):
                parts = cat_path.split("/")
                topic_items[parts[0]][parts[1] if len(parts) > 1 else "_all"].append(item)

        out_base = self.root / "knowledge" / "highlights"
        written = skipped = 0

        for cat_key, subcats in topic_items.items():
            cat_info = tax_cats.get(cat_key, {})
            cat_label = cat_info.get("label", cat_key)

            for sub_key, items in subcats.items():
                if len(items) < min_items:
                    skipped += 1
                    continue

                sub_info = cat_info.get("subcategories", {}).get(sub_key, {})
                sub_label = sub_info.get("label", sub_key)

                # Pool: top items by quality score
                pool = sorted(
                    [i for i in items if i.analysis],
                    key=lambda x: x.analysis.quality_score,
                    reverse=True,
                )[:pool_size]

                fingerprint = "|".join(f"{i.id}:{i.analysis.quality_score:.2f}" for i in pool)
                out_path = out_base / cat_key / f"{sub_key}.md"
                if not force and not self.needs_update(f"curator:{cat_key}/{sub_key}", fingerprint):
                    skipped += 1
                    continue

                prompt = self._render(template, cat_label, sub_label, pool, pick_count)
                try:
                    raw = self.call(prompt, max_tokens=1024)
                    picks = self._parse_picks(raw, pool)
                    if picks:
                        self._write_output(out_path, cat_label, sub_label, picks)
                        written += 1
                except Exception as e:
                    print(f"  [highlight_curator] {cat_key}/{sub_key}: {e}")

        self.save_manifest()
        return {"written": written, "skipped": skipped}

    def _render(self, template: str, cat_label: str, sub_label: str,
                pool: list, pick_count: int) -> str:
        items_block = "\n".join(
            f"ID:{item.id} | Score:{item.analysis.quality_score:.2f} | "
            f"@{item.author_handle or 'unknown'} | {item.analysis.summary or item.text[:100]}"
            for item in pool
        )
        return (
            template
            .replace("{category}", cat_label)
            .replace("{subcategory}", sub_label)
            .replace("{pick_count}", str(pick_count))
            .replace("{pool_size}", str(len(pool)))
            .replace("{items}", items_block)
        )

    def _parse_picks(self, response: str, pool: list) -> list:
        """Extract selected item IDs from the response and return those items in order."""
        pool_by_id = {item.id: item for item in pool}
        # Look for ID: patterns in the response
        ids_found = re.findall(r"ID:(\w+)", response)
        picks = []
        seen: set[str] = set()
        for item_id in ids_found:
            if item_id in pool_by_id and item_id not in seen:
                picks.append(pool_by_id[item_id])
                seen.add(item_id)
        return picks

    def _write_output(self, path: Path, cat_label: str, sub_label: str, picks: list) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# {sub_label} — Curated Highlights\n",
            f"_Category: {cat_label} · {len(picks)} picks_\n",
            "",
        ]
        for item in picks:
            a = item.analysis
            handle = item.author_handle or "unknown"
            author_url = f"https://x.com/{handle}"
            lines += [
                f"### [{handle}]({author_url})",
                "",
                f"> {a.summary}" if a and a.summary else f"> {item.text[:200]}",
                "",
                f"[Source]({item.url})",
            ]
            if a and a.tags:
                lines.append("  `" + "`  `".join(a.tags[:5]) + "`")
            lines += ["", "---", ""]
        path.write_text("\n".join(lines), encoding="utf-8")
