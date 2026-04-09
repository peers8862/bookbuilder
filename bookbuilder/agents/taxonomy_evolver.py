"""
taxonomy_evolver agent

Looks at _uncategorized items and the _noise cluster, then proposes new
subcategories to add to taxonomy.yaml. Writes proposals to
system/notes/taxonomy_proposals.md — never auto-edits taxonomy.yaml.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from ..config import load_config, load_taxonomy
from ..store import iter_items
from .base import Agent


class TaxonomyEvolverAgent(Agent):
    name = "taxonomy_evolver"

    def run(self, force: bool = False) -> dict:
        taxonomy = load_taxonomy(self.root)
        existing_cats = set(taxonomy.get("categories", {}).keys())
        template = self.prompt_template()
        out_path = self.root / "system" / "notes" / "taxonomy_proposals.md"

        # Collect uncategorized and noise items
        uncategorized: list = []
        noise: list = []
        tag_counter: Counter = Counter()

        for item in iter_items(self.root):
            if not item.analysis:
                continue
            cats = item.analysis.categories or []
            is_uncat = not cats or cats == ["_uncategorized"]
            is_noise = item.analysis.cluster_id == "_noise"

            if is_uncat or is_noise:
                target = uncategorized if is_uncat else noise
                target.append(item)
                tag_counter.update(item.analysis.tags)

        total_flagged = len(uncategorized) + len(noise)
        if total_flagged < 5:
            print(f"  [taxonomy_evolver] only {total_flagged} flagged items, skipping")
            return {"proposals": 0, "skipped": 1}

        fingerprint = f"{total_flagged}:{','.join(t for t,_ in tag_counter.most_common(20))}"
        if not force and not self.needs_update("taxonomy_evolver:main", fingerprint):
            return {"proposals": 0, "skipped": 1}

        # Build item summaries for the prompt
        def _summarise(items: list, n: int = 30) -> str:
            top = sorted(items, key=lambda x: x.analysis.quality_score if x.analysis else 0, reverse=True)[:n]
            return "\n".join(
                f"- {item.analysis.summary or item.text[:100]} [tags: {', '.join(item.analysis.tags[:4])}]"
                for item in top
                if item.analysis
            )

        top_tags = ", ".join(f"{t} ({c})" for t, c in tag_counter.most_common(20))
        existing_list = "\n".join(f"  - {k}" for k in sorted(existing_cats))

        prompt = (
            template
            .replace("{uncategorized_count}", str(len(uncategorized)))
            .replace("{noise_count}", str(len(noise)))
            .replace("{top_tags}", top_tags)
            .replace("{uncategorized_samples}", _summarise(uncategorized))
            .replace("{noise_samples}", _summarise(noise))
            .replace("{existing_categories}", existing_list)
        )

        try:
            result = self.call(prompt, max_tokens=1500)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                f"# Taxonomy Proposals\n\n"
                f"_{len(uncategorized)} uncategorized + {len(noise)} noise items analysed_\n\n"
                f"{result.strip()}\n",
                encoding="utf-8",
            )
            self.save_manifest()
            return {"proposals": 1, "flagged_items": total_flagged}
        except Exception as e:
            print(f"  [taxonomy_evolver] {e}")
            return {"proposals": 0, "errors": 1}
