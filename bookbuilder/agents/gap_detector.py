"""
gap_detector agent

Looks at what's thin or missing in the knowledge base:
  - taxonomy categories with few/no items
  - tech referenced by followed authors but rarely saved
  - clusters that are large but have low average quality (lots of noise)

Output: knowledge/gaps.md
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from ..config import load_config, load_taxonomy
from ..store import iter_items
from .base import Agent


class GapDetectorAgent(Agent):
    name = "gap_detector"

    def run(self, force: bool = False) -> dict:
        taxonomy = load_taxonomy(self.root)
        clusters = self._load_clusters()
        template = self.prompt_template()
        out_path = self.root / "knowledge" / "gaps.md"

        # Gather stats
        cat_counts: Counter = Counter()
        tech_counts: Counter = Counter()
        author_tech: dict[str, Counter] = defaultdict(Counter)
        cluster_scores: dict[str, list[float]] = defaultdict(list)
        total = 0

        for item in iter_items(self.root):
            total += 1
            a = item.analysis
            if not a:
                continue
            for cat in (a.categories or ["_uncategorized"]):
                cat_counts[cat.split("/")[0]] += 1
            for ref in a.tech_refs.all_refs():
                tech_counts[ref.lower()] += 1
                if item.author_handle:
                    author_tech[item.author_handle.lower()][ref.lower()] += 1
            if a.cluster_id:
                cluster_scores[a.cluster_id].append(a.quality_score)

        # Thin categories: defined in taxonomy but < 5 items
        tax_cats = taxonomy.get("categories", {})
        thin_cats = [
            f"{info.get('label', key)} ({cat_counts.get(key, 0)} items)"
            for key, info in tax_cats.items()
            if key != "_uncategorized" and cat_counts.get(key, 0) < 5
        ]

        # Low-quality clusters: avg score < 0.5 and size > 10
        noisy_clusters = []
        for cid, scores in cluster_scores.items():
            if cid == "_noise" or len(scores) < 10:
                continue
            avg = sum(scores) / len(scores)
            if avg < 0.5:
                label = clusters.get(cid, {}).get("label", cid)
                noisy_clusters.append(f"{label} (avg score: {avg:.2f}, {len(scores)} items)")

        # Tech mentioned by prolific authors but rarely saved overall
        # Authors with 10+ items whose top tech has < 3 items saved
        prolific = {h: c for h, c in author_tech.items() if sum(c.values()) >= 10}
        underrepresented_tech = []
        for handle, tech_c in prolific.items():
            for tech, count in tech_c.most_common(5):
                if tech_counts.get(tech, 0) < 3:
                    underrepresented_tech.append(
                        f"{tech} (mentioned by @{handle}, only {tech_counts.get(tech,0)} items saved)"
                    )

        fingerprint = f"{total}:{len(thin_cats)}:{len(noisy_clusters)}:{len(underrepresented_tech)}"
        if not force and not self.needs_update("gap_detector:main", fingerprint):
            return {"written": 0, "skipped": 1}

        prompt = (
            template
            .replace("{total_items}", str(total))
            .replace("{thin_categories}", "\n".join(f"- {c}" for c in thin_cats) or "(none)")
            .replace("{noisy_clusters}", "\n".join(f"- {c}" for c in noisy_clusters) or "(none)")
            .replace("{underrepresented_tech}", "\n".join(f"- {t}" for t in underrepresented_tech[:20]) or "(none)")
        )

        try:
            result = self.call(prompt, max_tokens=1500)
            self._write_output(out_path, result, thin_cats, noisy_clusters, underrepresented_tech)
            self.save_manifest()
            return {"written": 1}
        except Exception as e:
            print(f"  [gap_detector] {e}")
            return {"written": 0, "errors": 1}

    def _write_output(
        self,
        path: Path,
        analysis: str,
        thin_cats: list,
        noisy_clusters: list,
        underrepresented_tech: list,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Knowledge Gaps\n",
            analysis.strip(),
            "",
            "---",
            "",
            "## Thin categories\n",
        ]
        for c in thin_cats:
            lines.append(f"- {c}")
        lines += ["", "## Noisy clusters\n", "_Large clusters with low average quality score_\n"]
        for c in noisy_clusters:
            lines.append(f"- {c}")
        lines += ["", "## Underrepresented technologies\n"]
        for t in underrepresented_tech[:20]:
            lines.append(f"- {t}")
        path.write_text("\n".join(lines), encoding="utf-8")

    def _load_clusters(self) -> dict:
        path = self.root / "system" / "clusters.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return {}
