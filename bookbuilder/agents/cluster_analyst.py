"""
cluster_analyst agent

For each named cluster, sends the top N item summaries to Claude and writes:
  - a 2-3 sentence synthesis of what the cluster is really about
  - the "so what" — why this collection matters
  - 3 key tensions or open questions in this space

Output: knowledge/clusters/{cluster_id}.md
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from ..config import load_config
from ..store import iter_items
from .base import Agent


class ClusterAnalystAgent(Agent):
    name = "cluster_analyst"

    def run(self, force: bool = False) -> dict:
        clusters = self._load_clusters()
        if not clusters:
            print("  [cluster_analyst] no clusters found — run `bookbuilder cluster` first")
            return {"written": 0, "skipped": 0}

        cfg = load_config(self.root)
        agent_cfg = cfg.get("agents", {}).get("cluster_analyst", {})
        top_n = int(agent_cfg.get("top_n_items", 15))
        min_items = int(agent_cfg.get("min_items", 3))

        template = self.prompt_template()
        out_dir = self.root / "knowledge" / "clusters"
        out_dir.mkdir(parents=True, exist_ok=True)

        # Group items by cluster
        cluster_items: dict[str, list] = defaultdict(list)
        for item in iter_items(self.root):
            if item.analysis and item.analysis.cluster_id:
                cluster_items[item.analysis.cluster_id].append(item)

        written = skipped = 0

        for cluster_id, info in clusters.items():
            if cluster_id == "_noise":
                continue
            items = cluster_items.get(cluster_id, [])
            if len(items) < min_items:
                skipped += 1
                continue

            # Sort by quality score, take top N
            top_items = sorted(
                items, key=lambda x: x.analysis.quality_score if x.analysis else 0, reverse=True
            )[:top_n]

            fingerprint = f"{cluster_id}:{info.get('updated_at','')}:{len(items)}"
            out_path = out_dir / f"{cluster_id}.md"
            if not force and not self.needs_update(f"cluster_analyst:{cluster_id}", fingerprint):
                skipped += 1
                continue

            prompt = self._render(template, cluster_id, info, top_items)
            try:
                result = self.call(prompt, max_tokens=1024)
                self._write_output(out_path, cluster_id, info, items, result)
                written += 1
            except Exception as e:
                print(f"  [cluster_analyst] {cluster_id}: {e}")

        self.save_manifest()
        return {"written": written, "skipped": skipped}

    def _render(self, template: str, cluster_id: str, info: dict, items: list) -> str:
        summaries = "\n".join(
            f"- {item.analysis.summary}" if item.analysis and item.analysis.summary
            else f"- {item.text[:120]}"
            for item in items
        )
        tags_flat = []
        for item in items:
            if item.analysis:
                tags_flat.extend(item.analysis.tags)
        from collections import Counter
        top_tags = ", ".join(t for t, _ in Counter(tags_flat).most_common(10))

        return (
            template
            .replace("{cluster_label}", info.get("label", cluster_id))
            .replace("{cluster_parent}", info.get("parent", ""))
            .replace("{item_count}", str(len(items)))
            .replace("{top_tags}", top_tags)
            .replace("{summaries}", summaries)
        )

    def _write_output(self, path: Path, cluster_id: str, info: dict, items: list, analysis: str) -> None:
        label = info.get("label", cluster_id)
        parent = info.get("parent", "")
        lines = [
            f"# {label}\n",
            f"_Category: {parent} · {len(items)} items_\n",
            "",
            analysis.strip(),
            "",
            "---",
            "",
            "## Items in this cluster\n",
        ]
        sorted_items = sorted(
            items, key=lambda x: x.analysis.quality_score if x.analysis else 0, reverse=True
        )
        for item in sorted_items:
            a = item.analysis
            summary = (a.summary[:100] + "…") if a and a.summary else item.text[:80].replace("\n", " ")
            handle = item.author_handle or "unknown"
            score = f"{a.quality_score:.2f}" if a else "—"
            lines.append(f"- **@{handle}** — _{summary}_ [{score}]({item.url})")

        path.write_text("\n".join(lines), encoding="utf-8")

    def _load_clusters(self) -> dict:
        path = self.root / "system" / "clusters.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return {}
