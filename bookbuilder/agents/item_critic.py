"""
item_critic agent

Re-analyzes items where quality_score < threshold or categories = [_uncategorized].
Sends the item back to Claude with its cluster context for a tighter second pass.
Overwrites quality_score, categories, summary, and tags in place.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..store import iter_items, write_item
from ..config import load_config, taxonomy_category_paths
from .base import Agent


class ItemCriticAgent(Agent):
    name = "item_critic"

    def run(self, force: bool = False) -> dict:
        cfg = load_config(self.root)
        agent_cfg = cfg.get("agents", {}).get("item_critic", {})
        score_threshold = float(agent_cfg.get("score_threshold", 0.55))
        category_paths = taxonomy_category_paths(self.root)
        clusters = self._load_clusters()
        template = self.prompt_template()

        revised = skipped = errors = 0

        for item in iter_items(self.root):
            if not item.analysis:
                continue

            a = item.analysis
            needs_review = (
                force
                or a.quality_score < score_threshold
                or a.categories == ["_uncategorized"]
                or not a.categories
            )
            if not needs_review:
                skipped += 1
                continue

            cluster_context = ""
            if a.cluster_id and a.cluster_id in clusters:
                cl = clusters[a.cluster_id]
                cluster_context = f"This item belongs to cluster: \"{cl.get('label', '')}\" (parent: {cl.get('parent', '')})"

            fingerprint_input = f"{item.id}:{a.quality_score}:{a.categories}:{cluster_context}"
            if not force and not self.needs_update(f"critic:{item.id}", fingerprint_input):
                skipped += 1
                continue

            prompt = self._render(template, item, cluster_context, category_paths)
            try:
                raw = self.call(prompt, max_tokens=512)
                updates = self._parse(raw)
                if updates:
                    if "quality_score" in updates:
                        a.quality_score = float(updates["quality_score"])
                    if "categories" in updates and updates["categories"]:
                        a.categories = updates["categories"]
                    if "summary" in updates and updates["summary"]:
                        a.summary = updates["summary"]
                    if "tags" in updates and updates["tags"]:
                        a.tags = updates["tags"]
                    a.analyzed_at = datetime.now(timezone.utc).isoformat()
                    write_item(self.root, item)
                    revised += 1
                if (revised + errors) % 50 == 0:
                    self.checkpoint()
            except Exception as e:
                print(f"  [item_critic] {item.id}: {e}")
                errors += 1

        self.save_manifest()
        return {"revised": revised, "skipped": skipped, "errors": errors}

    def _render(self, template: str, item, cluster_context: str, category_paths: list[str]) -> str:
        a = item.analysis
        taxonomy_str = "\n".join(f"  - {p}" for p in category_paths)
        return (
            template
            .replace("{text}", item.text or "(no text)")
            .replace("{card_title}", item.card_title or "(none)")
            .replace("{card_desc}", item.card_desc or "(none)")
            .replace("{current_summary}", a.summary or "(none)")
            .replace("{current_tags}", ", ".join(a.tags) if a.tags else "(none)")
            .replace("{current_categories}", ", ".join(a.categories) if a.categories else "(none)")
            .replace("{current_quality_score}", str(a.quality_score))
            .replace("{cluster_context}", cluster_context or "(not clustered yet)")
            .replace("{taxonomy_categories}", taxonomy_str)
        )

    def _parse(self, text: str) -> dict:
        import json, re
        try:
            return json.loads(text)
        except Exception:
            pass
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                pass
        return {}

    def _load_clusters(self) -> dict:
        path = self.root / "system" / "clusters.json"
        if path.exists():
            import json
            return json.loads(path.read_text(encoding="utf-8"))
        return {}
