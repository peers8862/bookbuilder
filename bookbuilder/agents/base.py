"""
Base class for all bookbuilder agents.

Each agent:
  - loads its prompt from system/prompts/agents/{name}.md
  - calls Claude with a rendered prompt
  - writes output to knowledge/
  - records a fingerprint in state/agents_manifest.json to skip unchanged work
"""

from __future__ import annotations

import hashlib
import json
import os
from abc import ABC, abstractmethod
from pathlib import Path

import anthropic


class Agent(ABC):
    name: str  # subclasses set this

    def __init__(self, root: Path, cfg: dict) -> None:
        self.root = root
        self.cfg = cfg
        ai_cfg = cfg.get("ai", {})
        self.model = ai_cfg.get("model", "claude-sonnet-4-6")
        self.max_tokens = int(ai_cfg.get("max_tokens", 2048))
        api_key = os.environ.get(
            cfg.get("api_keys", {}).get("anthropic_env", "ANTHROPIC_API_KEY"), ""
        )
        self.client = anthropic.Anthropic(api_key=api_key)
        self._manifest = _AgentManifest(root / "state" / "agents_manifest.json")

    def prompt_template(self) -> str:
        path = self.root / "system" / "prompts" / "agents" / f"{self.name}.md"
        return path.read_text(encoding="utf-8")

    def call(self, prompt: str, max_tokens: int | None = None) -> str:
        msg = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens or self.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text if msg.content else ""

    def needs_update(self, key: str, content: str) -> bool:
        return self._manifest.needs_update(key, content)

    def save_manifest(self) -> None:
        self._manifest.save()

    def checkpoint(self) -> None:
        """Incremental save — call every N items to survive crashes."""
        self._manifest.save()

    @abstractmethod
    def run(self, force: bool = False) -> dict:
        """Run the agent. Returns a summary dict of what was done."""


class _AgentManifest:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, str] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass

    def needs_update(self, key: str, content: str) -> bool:
        fp = hashlib.sha256(content.encode()).hexdigest()[:16]
        if self._data.get(key) == fp:
            return False
        self._data[key] = fp
        return True

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, indent=2), encoding="utf-8"
        )
