"""
Analyze stage: run AI analysis on each item using the Claude API.

Batches items, renders the prompt template from system/prompts/analyze.md,
calls Claude, parses the JSON response, and writes results back to
knowledge/items/{id}.json and updates state/items.jsonl.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from .config import load_config, taxonomy_category_paths
from .ingest import load_state, save_state
from .models import Analysis, Entities, TechRefs
from .store import iter_items, read_item, write_item


# ── Prompt rendering ─────────────────────────────────────────────────────────

def _load_prompt_template(root: Path) -> str:
    return (root / "system" / "prompts" / "analyze.md").read_text(encoding="utf-8")


def _item_text_excerpt(item, max_chars: int = 800) -> str:
    """Best available text from a fetched page for the item."""
    for page in item.fetched_pages:
        if page.status == "ok" and page.text_path:
            try:
                text = Path(page.text_path).read_text(encoding="utf-8")
                return text[:max_chars]
            except Exception:
                pass
    return ""


def _render_prompt(template: str, item, category_paths: list[str]) -> str:
    fetched_title = next(
        (p.title for p in item.fetched_pages if p.status == "ok" and p.title), ""
    )
    fetched_text = _item_text_excerpt(item)
    taxonomy_str = "\n".join(f"  - {p}" for p in category_paths)
    # For document sources, card_title holds the document title
    doc_sources = {"markdown", "text", "docx"}
    title = item.card_title if item.source in doc_sources else (fetched_title or "(none)")
    source_hint = f"local {item.source} file" if item.source in doc_sources else "tweet"

    return (
        template
        .replace("{text}", item.text or "(no text)")
        .replace("{fetched_title}", title or "(none)")
        .replace("{fetched_text}", fetched_text or "(not fetched)")
        .replace("{card_title}", item.card_title or "(none)")
        .replace("{card_desc}", item.card_desc or "(none)")
        .replace("{source_hint}", source_hint)
        .replace("{taxonomy_categories}", taxonomy_str)
    )


# ── JSON response parsing ────────────────────────────────────────────────────

def _extract_json(text: str) -> dict:
    """Extract the first JSON object from a response that may have surrounding text."""
    # Try direct parse first
    try:
        return json.loads(text)
    except Exception:
        pass
    # Extract from markdown code block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # Find outermost braces
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    return {}


def _parse_analysis(raw: dict, model: str, now: str) -> Analysis:
    ent = raw.get("entities") or {}
    tech = raw.get("tech_refs") or {}
    categories = raw.get("categories") or []
    if isinstance(categories, str):
        categories = [categories]

    return Analysis(
        summary=str(raw.get("summary", "")).strip(),
        tags=[str(t).strip().lower() for t in (raw.get("tags") or [])],
        entities=Entities(
            people=list(ent.get("people") or []),
            orgs=list(ent.get("orgs") or []),
            concepts=list(ent.get("concepts") or []),
            places=list(ent.get("places") or []),
        ),
        tech_refs=TechRefs(
            languages=list(tech.get("languages") or []),
            frameworks=list(tech.get("frameworks") or []),
            tools=list(tech.get("tools") or []),
            packages=list(tech.get("packages") or []),
            repos=list(tech.get("repos") or []),
            hardware=list(tech.get("hardware") or []),
            platforms=list(tech.get("platforms") or []),
        ),
        categories=categories,
        quality_score=float(raw.get("quality_score") or 0.0),
        analyzed_at=now,
        model=model,
    )


# ── Single item analysis (one API call) ─────────────────────────────────────

def _analyze_one(
    client: anthropic.Anthropic,
    item,
    template: str,
    category_paths: list[str],
    model: str,
    max_tokens: int,
) -> Analysis | None:
    prompt = _render_prompt(template, item, category_paths)
    now = datetime.now(timezone.utc).isoformat()
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = msg.content[0].text if msg.content else ""
        raw = _extract_json(raw_text)
        if not raw:
            print(f"  [analyze] {item.id}: empty JSON response")
            return None
        return _parse_analysis(raw, model, now)
    except Exception as e:
        print(f"  [analyze] {item.id}: API error — {e}")
        return None


# ── Main analyze runner ──────────────────────────────────────────────────────

def run_analyze(root: Path, force: bool = False, batch_size: int | None = None,
                since: str | None = None, cluster_id: str | None = None) -> tuple[int, int]:
    """
    Analyze all items with analyze_status != "ok" (or all if force=True).
    --since DATE  : only items ingested on or after DATE (ISO format, e.g. 2024-01-01)
    --cluster ID  : only items belonging to this cluster_id
    Returns (analyzed_count, error_count).
    """
    import os
    cfg = load_config(root)
    ai_cfg = cfg.get("ai", {})
    model      = ai_cfg.get("model", "claude-sonnet-4-6")
    max_tokens = int(ai_cfg.get("max_tokens", 1024))
    batch      = batch_size or int(ai_cfg.get("analyze_batch_size", 20))

    api_key = os.environ.get(cfg.get("api_keys", {}).get("anthropic_env", "ANTHROPIC_API_KEY"), "")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        return 0, 0

    client = anthropic.Anthropic(api_key=api_key)
    template = _load_prompt_template(root)
    category_paths = taxonomy_category_paths(root)

    state_path = root / "state" / "items.jsonl"
    states = load_state(state_path)

    pending = [
        iid for iid, s in states.items()
        if force or s.analyze_status != "ok"
    ]

    # --since filter
    if since:
        pending = [iid for iid in pending if states[iid].ingested_at[:10] >= since[:10]]

    # --cluster filter: load items to check cluster_id
    if cluster_id:
        from .store import read_item
        pending = [
            iid for iid in pending
            if (item := read_item(root, iid)) and item.analysis
            and item.analysis.cluster_id == cluster_id
        ]
    print(f"  {len(pending)} items to analyze (model={model}, batch={batch})")

    analyzed = error = 0
    for i, item_id in enumerate(pending):
        item = read_item(root, item_id)
        if item is None:
            continue

        analysis = _analyze_one(client, item, template, category_paths, model, max_tokens)

        if analysis:
            item.analysis = analysis
            if item.state:
                item.state.analyze_status = "ok"
                item.state.analyzed_at = analysis.analyzed_at
            states[item_id].analyze_status = "ok"
            states[item_id].analyzed_at = analysis.analyzed_at
            write_item(root, item)
            analyzed += 1
        else:
            states[item_id].analyze_status = "error"
            error += 1

        # Progress + rate-limit courtesy pause every batch
        if (i + 1) % batch == 0:
            print(f"  progress: {i + 1}/{len(pending)}  ok={analyzed} err={error}")
            time.sleep(0.5)

    save_state(states, state_path)
    print(f"  done: analyzed={analyzed} errors={error}")
    return analyzed, error
