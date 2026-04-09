"""
Agent runner — orchestrates all agents in dependency order.

Dependency order:
  item_critic       (needs: analyzed items)
  cluster_analyst   (needs: clusters + item_critic)
  digest_writer     (needs: built topic digests + cluster_analyst)
  highlight_curator (needs: analyzed items)
  connection_finder (needs: clusters + cluster_analyst)
  gap_detector      (needs: everything above)
  taxonomy_evolver  (needs: analyzed items, runs independently)
"""

from __future__ import annotations

from pathlib import Path

from ..config import load_config
from .item_critic import ItemCriticAgent
from .cluster_analyst import ClusterAnalystAgent
from .digest_writer import DigestWriterAgent
from .connection_finder import ConnectionFinderAgent
from .gap_detector import GapDetectorAgent
from .taxonomy_evolver import TaxonomyEvolverAgent
from .highlight_curator import HighlightCuratorAgent


_AGENTS = [
    ("item_critic",        ItemCriticAgent),
    ("cluster_analyst",    ClusterAnalystAgent),
    ("digest_writer",      DigestWriterAgent),
    ("highlight_curator",  HighlightCuratorAgent),
    ("connection_finder",  ConnectionFinderAgent),
    ("gap_detector",       GapDetectorAgent),
    ("taxonomy_evolver",   TaxonomyEvolverAgent),
]


# Agents that require clusters to exist first
_NEEDS_CLUSTERS = {"cluster_analyst", "connection_finder", "gap_detector"}


def run_agents(
    root: Path,
    only: list[str] | None = None,
    force: bool = False,
) -> dict[str, dict]:
    cfg = load_config(root)
    agents_cfg = cfg.get("agents", {})
    clusters_exist = (root / "system" / "clusters.json").exists()
    results: dict[str, dict] = {}

    for name, AgentClass in _AGENTS:
        if only and name not in only:
            continue
        if not agents_cfg.get(name, {}).get("enabled", True):
            print(f"  [{name}] disabled in config, skipping")
            continue
        if name in _NEEDS_CLUSTERS and not clusters_exist:
            print(f"  [{name}] skipped — no clusters.json found, run `bookbuilder cluster` first")
            continue

        print(f"  running {name}...")
        agent = AgentClass(root, cfg)
        result = agent.run(force=force)
        results[name] = result
        print(f"  [{name}] {result}")

    return results
