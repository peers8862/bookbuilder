"""
Load and cache project configuration from system/.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@lru_cache(maxsize=1)
def load_config(root: Path) -> dict:
    return _load_yaml(root / "system" / "config.yaml")


@lru_cache(maxsize=1)
def load_taxonomy(root: Path) -> dict:
    return _load_yaml(root / "system" / "taxonomy.yaml")


@lru_cache(maxsize=1)
def load_skip_domains(root: Path) -> frozenset[str]:
    path = root / "system" / "skip_domains.txt"
    domains: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            domains.add(line.lower())
    return frozenset(domains)


def taxonomy_category_paths(root: Path) -> list[str]:
    """
    Return flat list of all valid category paths for use in prompts.
    e.g. ["tech_stack/languages", "tech_stack/frameworks", "ai_ml", "ai_ml/ai_tools", ...]
    """
    tax = load_taxonomy(root)
    paths: list[str] = []
    for cat_key, cat in tax.get("categories", {}).items():
        paths.append(cat_key)
        for sub_key in cat.get("subcategories", {}).keys():
            paths.append(f"{cat_key}/{sub_key}")
    return paths


def is_skip_domain(url: str, skip_domains: frozenset[str]) -> bool:
    """Return True if the URL's domain (or any parent domain) is in the skip list."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        host = host.lower().lstrip("www.")
        parts = host.split(".")
        for i in range(len(parts) - 1):
            candidate = ".".join(parts[i:])
            if candidate in skip_domains:
                return True
    except Exception:
        pass
    return False
