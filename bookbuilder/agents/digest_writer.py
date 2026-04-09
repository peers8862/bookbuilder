"""
digest_writer agent

Writes an AI-generated intro paragraph for each topic digest in knowledge/topics/.
The intro is prepended to the existing digest file produced by build.py.
Tracks which digests have changed so re-runs are cheap.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from ..config import load_config, load_taxonomy
from ..store import iter_items
from .base import Agent


_AGENT_MARKER = "<!-- digest_writer -->"


class DigestWriterAgent(Agent):
    name = "digest_writer"

    def run(self, force: bool = False) -> dict:
        cfg = load_config(self.root)
        agent_cfg = cfg.get("agents", {}).get("digest_writer", {})
        top_n = int(agent_cfg.get("top_n_items", 12))
        min_items = int(agent_cfg.get("min_items", 4))

        taxonomy = load_taxonomy(self.root)
        tax_cats = taxonomy.get("categories", {})
        template = self.prompt_template()

        # Group items by category/subcategory
        from collections import defaultdict
        topic_items: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        for item in iter_items(self.root):
            cats = item.analysis.categories if item.analysis else ["_uncategorized"]
            for cat_path in (cats or ["_uncategorized"]):
                parts = cat_path.split("/")
                topic_items[parts[0]][parts[1] if len(parts) > 1 else "_all"].append(item)

        written = skipped = 0

        for cat_key, subcats in topic_items.items():
            cat_info = tax_cats.get(cat_key, {})
            cat_label = cat_info.get("label", cat_key)

            for sub_key, items in subcats.items():
                if len(items) < min_items:
                    skipped += 1
                    continue

                digest_path = self.root / "knowledge" / "topics" / cat_key / f"{sub_key}.md"
                if not digest_path.exists():
                    skipped += 1
                    continue

                sub_info = cat_info.get("subcategories", {}).get(sub_key, {})
                sub_label = sub_info.get("label", sub_key)

                top_items = sorted(
                    items,
                    key=lambda x: x.analysis.quality_score if x.analysis else 0,
                    reverse=True,
                )[:top_n]

                fingerprint = "|".join(
                    f"{i.id}:{i.analysis.quality_score if i.analysis else 0}"
                    for i in top_items
                )
                if not force and not self.needs_update(f"digest:{cat_key}/{sub_key}", fingerprint):
                    skipped += 1
                    continue

                prompt = self._render(template, cat_label, sub_label, top_items)
                try:
                    intro = self.call(prompt, max_tokens=512)
                    self._prepend_intro(digest_path, intro.strip())
                    written += 1
                except Exception as e:
                    print(f"  [digest_writer] {cat_key}/{sub_key}: {e}")

        self.save_manifest()
        return {"written": written, "skipped": skipped}

    def _render(self, template: str, cat_label: str, sub_label: str, items: list) -> str:
        summaries = "\n".join(
            f"- {item.analysis.summary}" if item.analysis and item.analysis.summary
            else f"- {item.text[:100]}"
            for item in items
        )
        tags: list[str] = []
        for item in items:
            if item.analysis:
                tags.extend(item.analysis.tags)
        top_tags = ", ".join(t for t, _ in Counter(tags).most_common(8))

        return (
            template
            .replace("{category}", cat_label)
            .replace("{subcategory}", sub_label)
            .replace("{item_count}", str(len(items)))
            .replace("{top_tags}", top_tags)
            .replace("{summaries}", summaries)
        )

    def _prepend_intro(self, digest_path: Path, intro: str) -> None:
        existing = digest_path.read_text(encoding="utf-8")
        # Remove any previously written intro block
        existing = re.sub(
            rf"{re.escape(_AGENT_MARKER)}.*?{re.escape(_AGENT_MARKER)}\n",
            "",
            existing,
            flags=re.DOTALL,
        )
        # Find the first ## heading and insert before it
        insert_block = f"{_AGENT_MARKER}\n{intro}\n{_AGENT_MARKER}\n\n"
        match = re.search(r"^## ", existing, re.MULTILINE)
        if match:
            pos = match.start()
            new_content = existing[:pos] + insert_block + existing[pos:]
        else:
            new_content = existing + "\n" + insert_block

        digest_path.write_text(new_content, encoding="utf-8")
