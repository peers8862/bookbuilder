"""
Status stage: print a one-screen summary of corpus health.

Shows counts at each pipeline stage, last-run timestamps, cluster stats,
agent output presence, and a quick quality score distribution.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


def run_status(root: Path) -> None:
    try:
        from rich.console import Console
        from rich.table import Table
        from rich import box
        _rich = True
    except ImportError:
        _rich = False

    data = _collect(root)

    if _rich:
        _print_rich(data)
    else:
        _print_plain(data)


def _collect(root: Path) -> dict:
    # ── State ────────────────────────────────────────────────────────────────
    state_path = root / "state" / "items.jsonl"
    states: list[dict] = []
    if state_path.exists():
        for line in state_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    states.append(json.loads(line))
                except Exception:
                    pass

    total = len(states)
    fetch_ok    = sum(1 for s in states if s.get("fetch_status") == "ok")
    fetch_pend  = sum(1 for s in states if s.get("fetch_status") == "pending")
    fetch_err   = sum(1 for s in states if s.get("fetch_status") not in ("ok", "pending"))
    analyze_ok  = sum(1 for s in states if s.get("analyze_status") == "ok")
    analyze_pend= sum(1 for s in states if s.get("analyze_status") == "pending")
    analyze_err = sum(1 for s in states if s.get("analyze_status") not in ("ok", "pending"))

    sources: Counter = Counter(s.get("source", "unknown") for s in states)

    last_ingested = ""
    last_analyzed = ""
    if states:
        ingested_dates = [s.get("ingested_at", "") for s in states if s.get("ingested_at")]
        analyzed_dates = [s.get("analyzed_at", "") for s in states if s.get("analyzed_at")]
        if ingested_dates:
            last_ingested = max(ingested_dates)[:19].replace("T", " ")
        if analyzed_dates:
            last_analyzed = max(analyzed_dates)[:19].replace("T", " ")

    # ── Clusters ─────────────────────────────────────────────────────────────
    clusters_path = root / "system" / "clusters.json"
    cluster_count = 0
    noise_count = 0
    last_clustered = ""
    if clusters_path.exists():
        try:
            clusters = json.loads(clusters_path.read_text(encoding="utf-8"))
            cluster_count = sum(1 for k in clusters if k != "_noise")
            noise_count = clusters.get("_noise", {}).get("item_count", 0)
            dates = [v.get("updated_at", "") for v in clusters.values() if v.get("updated_at")]
            if dates:
                last_clustered = max(dates)[:19].replace("T", " ")
        except Exception:
            pass

    # ── Quality score distribution ────────────────────────────────────────────
    items_dir = root / "knowledge" / "items"
    scores: list[float] = []
    cat_counter: Counter = Counter()
    if items_dir.exists():
        for path in items_dir.glob("*.json"):
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
                a = d.get("analysis")
                if a:
                    scores.append(float(a.get("quality_score", 0)))
                    for cat in (a.get("categories") or []):
                        cat_counter[cat.split("/")[0]] += 1
            except Exception:
                pass

    score_dist = {"0.0–0.4": 0, "0.4–0.6": 0, "0.6–0.8": 0, "0.8–1.0": 0}
    for s in scores:
        if s < 0.4:
            score_dist["0.0–0.4"] += 1
        elif s < 0.6:
            score_dist["0.4–0.6"] += 1
        elif s < 0.8:
            score_dist["0.6–0.8"] += 1
        else:
            score_dist["0.8–1.0"] += 1
    avg_score = sum(scores) / len(scores) if scores else 0.0

    # ── Agent outputs ─────────────────────────────────────────────────────────
    agent_outputs = {
        "cluster analyses": len(list((root / "knowledge" / "clusters").glob("*.md")))
            if (root / "knowledge" / "clusters").exists() else 0,
        "connections.md":   (root / "knowledge" / "connections.md").exists(),
        "gaps.md":          (root / "knowledge" / "gaps.md").exists(),
        "taxonomy proposals": (root / "system" / "notes" / "taxonomy_proposals.md").exists(),
    }

    # ── Build outputs ─────────────────────────────────────────────────────────
    topic_count  = sum(1 for _ in (root / "knowledge" / "topics").rglob("*.md")) \
                   if (root / "knowledge" / "topics").exists() else 0
    author_count = len(list((root / "knowledge" / "authors").glob("*.md"))) \
                   if (root / "knowledge" / "authors").exists() else 0
    tech_count   = len(list((root / "knowledge" / "tech").glob("*.md"))) \
                   if (root / "knowledge" / "tech").exists() else 0

    return {
        "total": total,
        "sources": dict(sources.most_common()),
        "fetch":   {"ok": fetch_ok, "pending": fetch_pend, "error": fetch_err},
        "analyze": {"ok": analyze_ok, "pending": analyze_pend, "error": analyze_err},
        "last_ingested": last_ingested,
        "last_analyzed": last_analyzed,
        "clusters": {"count": cluster_count, "noise": noise_count, "last_run": last_clustered},
        "quality": {"distribution": score_dist, "avg": avg_score},
        "top_categories": dict(cat_counter.most_common(6)),
        "agent_outputs": agent_outputs,
        "build": {"topics": topic_count, "authors": author_count, "tech": tech_count},
    }


def _print_rich(data: dict) -> None:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import box

    console = Console()

    # ── Pipeline ──────────────────────────────────────────────────────────────
    pipeline = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan", padding=(0, 2))
    pipeline.add_column("Stage")
    pipeline.add_column("Done",    style="green",  justify="right")
    pipeline.add_column("Pending", style="yellow", justify="right")
    pipeline.add_column("Error",   style="red",    justify="right")
    pipeline.add_column("Last run", style="dim")

    pipeline.add_row(
        "ingest",
        str(data["total"]), "—", "—",
        data["last_ingested"] or "—",
    )
    pipeline.add_row(
        "fetch",
        str(data["fetch"]["ok"]),
        str(data["fetch"]["pending"]),
        str(data["fetch"]["error"]),
        "—",
    )
    pipeline.add_row(
        "analyze",
        str(data["analyze"]["ok"]),
        str(data["analyze"]["pending"]),
        str(data["analyze"]["error"]),
        data["last_analyzed"] or "—",
    )
    pipeline.add_row(
        "cluster",
        str(data["clusters"]["count"]) + " clusters",
        "—", "—",
        data["clusters"]["last_run"] or "—",
    )

    # ── Quality ───────────────────────────────────────────────────────────────
    dist = data["quality"]["distribution"]
    quality = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan", padding=(0, 2))
    quality.add_column("Score range")
    quality.add_column("Items", justify="right")
    for band, count in dist.items():
        quality.add_row(band, str(count))
    quality.add_row("[bold]avg[/bold]", f"[bold]{data['quality']['avg']:.2f}[/bold]")

    # ── Build outputs ─────────────────────────────────────────────────────────
    build = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    build.add_column("Output")
    build.add_column("Count", justify="right")
    build.add_row("topic digests",  str(data["build"]["topics"]))
    build.add_row("author digests", str(data["build"]["authors"]))
    build.add_row("tech pages",     str(data["build"]["tech"]))
    build.add_row("cluster analyses", str(data["agent_outputs"]["cluster analyses"]))
    build.add_row("connections.md", "✓" if data["agent_outputs"]["connections.md"] else "—")
    build.add_row("gaps.md",        "✓" if data["agent_outputs"]["gaps.md"] else "—")
    build.add_row("taxonomy proposals", "✓" if data["agent_outputs"]["taxonomy proposals"] else "—")

    # ── Sources ───────────────────────────────────────────────────────────────
    sources_str = "  ".join(f"{k}: {v}" for k, v in data["sources"].items())
    cats_str = "  ".join(f"{k}: {v}" for k, v in data["top_categories"].items())

    console.print()
    console.print(f"[bold]bookbuilder status[/bold]  [dim]{data['total']} items total[/dim]")
    console.print(f"[dim]sources:[/dim] {sources_str}")
    console.print(f"[dim]top categories:[/dim] {cats_str}")
    console.print()
    console.print("[bold cyan]Pipeline[/bold cyan]")
    console.print(pipeline)
    console.print("[bold cyan]Quality scores[/bold cyan]")
    console.print(quality)
    console.print("[bold cyan]Outputs[/bold cyan]")
    console.print(build)
    console.print()


def _print_plain(data: dict) -> None:
    print(f"\nbookbuilder status — {data['total']} items total")
    print(f"  sources: {data['sources']}")
    print(f"\nPipeline:")
    print(f"  ingest:  {data['total']} items  (last: {data['last_ingested'] or '—'})")
    print(f"  fetch:   ok={data['fetch']['ok']}  pending={data['fetch']['pending']}  error={data['fetch']['error']}")
    print(f"  analyze: ok={data['analyze']['ok']}  pending={data['analyze']['pending']}  error={data['analyze']['error']}  (last: {data['last_analyzed'] or '—'})")
    print(f"  cluster: {data['clusters']['count']} clusters  noise={data['clusters']['noise']}  (last: {data['clusters']['last_run'] or '—'})")
    print(f"\nQuality scores (avg {data['quality']['avg']:.2f}):")
    for band, count in data["quality"]["distribution"].items():
        print(f"  {band}: {count}")
    print(f"\nOutputs:")
    print(f"  topics={data['build']['topics']}  authors={data['build']['authors']}  tech={data['build']['tech']}")
    print(f"  cluster analyses={data['agent_outputs']['cluster analyses']}")
    print(f"  connections={'✓' if data['agent_outputs']['connections.md'] else '—'}  gaps={'✓' if data['agent_outputs']['gaps.md'] else '—'}")
    print()
