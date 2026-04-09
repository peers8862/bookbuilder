"""
connection_finder agent

Looks across clusters to find non-obvious connections:
  - tech that appears in multiple unrelated clusters
  - authors who bridge different topic areas
  - conceptual threads that cut across the taxonomy

Output: knowledge/connections.md
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from ..config import load_config
from ..store import iter_items
from .base import Agent


class ConnectionFinderAgent(Agent):
    name = "connection_finder"

    def run(self, force: bool = False) -> dict:
        clusters = self._load_clusters()
        if not clusters:
            print("  [connection_finder] no clusters found — run `bookbuilder cluster` first")
            return {"written": 0}

        template = self.prompt_template()
        out_path = self.root / "knowledge" / "connections.md"

        # Build cross-cluster stats
        cluster_items: dict[str, list] = defaultdict(list)
        tech_clusters: dict[str, set[str]] = defaultdict(set)
        author_clusters: dict[str, set[str]] = defaultdict(set)

        for item in iter_items(self.root):
            if not item.analysis or not item.analysis.cluster_id:
                continue
            cid = item.analysis.cluster_id
            if cid == "_noise":
                continue
            cluster_items[cid].append(item)
            for ref in item.analysis.tech_refs.all_refs():
                tech_clusters[ref.lower()].add(cid)
            if item.author_handle:
                author_clusters[item.author_handle.lower()].add(cid)

        # Cross-cutting tech: appears in 3+ distinct clusters
        cross_tech = {
            tech: sorted(cids)
            for tech, cids in tech_clusters.items()
            if len(cids) >= 3
        }

        # Bridge authors: appear in 3+ distinct clusters
        bridge_authors = {
            handle: sorted(cids)
            for handle, cids in author_clusters.items()
            if len(cids) >= 3
        }

        fingerprint = f"{len(clusters)}:{len(cross_tech)}:{len(bridge_authors)}"
        if not force and not self.needs_update("connection_finder:main", fingerprint):
            return {"written": 0, "skipped": 1}

        # Build cluster summaries for the prompt
        cluster_summaries = []
        for cid, info in clusters.items():
            if cid == "_noise":
                continue
            items = cluster_items.get(cid, [])
            top_tags: list[str] = []
            for item in items:
                if item.analysis:
                    top_tags.extend(item.analysis.tags)
            top = ", ".join(t for t, _ in Counter(top_tags).most_common(5))
            cluster_summaries.append(f"- **{info.get('label', cid)}** ({info.get('parent','')}): {top}")

        cross_tech_lines = [
            f"- {tech}: {', '.join(self._cluster_label(cid, clusters) for cid in cids)}"
            for tech, cids in sorted(cross_tech.items(), key=lambda x: -len(x[1]))[:20]
        ]
        bridge_lines = [
            f"- @{handle}: {', '.join(self._cluster_label(cid, clusters) for cid in cids)}"
            for handle, cids in sorted(bridge_authors.items(), key=lambda x: -len(x[1]))[:15]
        ]

        prompt = (
            template
            .replace("{cluster_summaries}", "\n".join(cluster_summaries))
            .replace("{cross_cutting_tech}", "\n".join(cross_tech_lines) or "(none)")
            .replace("{bridge_authors}", "\n".join(bridge_lines) or "(none)")
            .replace("{total_items}", str(sum(len(v) for v in cluster_items.values())))
            .replace("{total_clusters}", str(len([c for c in clusters if c != "_noise"])))
        )

        try:
            result = self.call(prompt, max_tokens=2048)
            self._write_output(out_path, result, cross_tech, bridge_authors, clusters)
            self.save_manifest()
            return {"written": 1}
        except Exception as e:
            print(f"  [connection_finder] {e}")
            return {"written": 0, "errors": 1}

    def _cluster_label(self, cid: str, clusters: dict) -> str:
        return clusters.get(cid, {}).get("label", cid)

    def _write_output(
        self,
        path: Path,
        analysis: str,
        cross_tech: dict[str, list],
        bridge_authors: dict[str, list],
        clusters: dict,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Cross-Cluster Connections\n",
            analysis.strip(),
            "",
            "---",
            "",
            "## Cross-cutting technologies\n",
            "_Technologies appearing in 3+ distinct clusters_\n",
        ]
        for tech, cids in sorted(cross_tech.items(), key=lambda x: -len(x[1])):
            labels = ", ".join(self._cluster_label(c, clusters) for c in cids)
            lines.append(f"- **{tech}** → {labels}")

        lines += ["", "## Bridge authors\n", "_Authors whose saves span 3+ distinct clusters_\n"]
        for handle, cids in sorted(bridge_authors.items(), key=lambda x: -len(x[1])):
            labels = ", ".join(self._cluster_label(c, clusters) for c in cids)
            lines.append(f"- **@{handle}** → {labels}")

        path.write_text("\n".join(lines), encoding="utf-8")

    def _load_clusters(self) -> dict:
        path = self.root / "system" / "clusters.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return {}
