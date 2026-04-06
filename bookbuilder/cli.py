"""
bookbuilder CLI entry point.

Usage:
    bookbuilder ingest  [--input FILE ...] [--source bookmark|like]
    bookbuilder fetch   [--workers N] [--force]
    bookbuilder analyze [--batch N] [--force]
    bookbuilder cluster [--force]
    bookbuilder build   [--force]
    bookbuilder run     # runs all enabled stages in sequence

Options passed to all stages:
    --dry-run    print what would happen, don't write anything
    --verbose    extra logging
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _project_root() -> Path:
    """Walk up from cwd to find the project root (contains system/config.yaml)."""
    p = Path.cwd()
    for candidate in [p, *p.parents]:
        if (candidate / "system" / "config.yaml").exists():
            return candidate
    return p  # fallback to cwd


def cmd_ingest(args: argparse.Namespace, root: Path) -> int:
    from .ingest import run_ingest

    input_dir = root / "input"
    state_dir  = root / "state"

    if args.input:
        paths = [Path(p) for p in args.input]
    else:
        paths = sorted(input_dir.glob("*.json"))
        if not paths:
            print(f"No JSON files found in {input_dir}")
            print("Drop your bookmarks/likes JSON files there and re-run.")
            return 1

    print(f"Ingesting {len(paths)} file(s)...")
    new, updated, skipped = run_ingest(paths, state_dir, root)
    print(f"  new={new}  updated={updated}  skipped={skipped}")
    return 0


def cmd_fetch(args: argparse.Namespace, root: Path) -> int:
    from .fetch import run_fetch
    workers = getattr(args, "workers", None)
    force   = getattr(args, "force", False)
    print("Fetching linked pages and images...")
    ok, err, skip = run_fetch(root, force=force, workers=workers)
    print(f"  ok={ok}  errors={err}  skipped={skip}")
    return 0


def cmd_analyze(args: argparse.Namespace, root: Path) -> int:
    from .analyze import run_analyze
    batch = getattr(args, "batch", None)
    force = getattr(args, "force", False)
    print("Running AI analysis...")
    run_analyze(root, force=force, batch_size=batch)
    return 0


def cmd_cluster(args: argparse.Namespace, root: Path) -> int:
    from .cluster import run_cluster
    force = getattr(args, "force", False)
    print("Clustering items...")
    n = run_cluster(root, force=force)
    print(f"  {n} clusters written to system/clusters.json")
    return 0


def cmd_build(args: argparse.Namespace, root: Path) -> int:
    from .build import run_build
    force = getattr(args, "force", False)
    print("Building knowledge base and site...")
    run_build(root, force=force)
    return 0


def cmd_mcp(args: argparse.Namespace, root: Path) -> int:
    from .mcp_server import run_server
    run_server(root)
    return 0


def cmd_search(args: argparse.Namespace, root: Path) -> int:
    from .search import search_items

    try:
        from rich.console import Console
        from rich.table import Table
        from rich import box
        _rich = True
    except ImportError:
        _rich = False

    query    = " ".join(args.query) if args.query else ""
    limit    = args.limit
    min_qs   = args.min_score
    category = getattr(args, "category", None)
    author   = getattr(args, "author", None)
    tech     = getattr(args, "tech", None)

    results = search_items(
        root, query,
        limit=limit,
        min_quality=min_qs,
        category=category,
        author=author,
        tech=tech,
    )

    if not results:
        print("No results found.")
        return 0

    if _rich:
        console = Console()
        table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan",
                      expand=True, padding=(0, 1))
        table.add_column("#",        style="dim",          width=3,  no_wrap=True)
        table.add_column("Score",    style="yellow",       width=5,  no_wrap=True)
        table.add_column("Quality",  style="green",        width=5,  no_wrap=True)
        table.add_column("Author",   style="cyan",         width=16, no_wrap=True)
        table.add_column("Summary",  ratio=3)
        table.add_column("Tags",     ratio=1, style="dim")
        table.add_column("URL",      style="blue dim",     width=20, no_wrap=True)

        for i, (score, item) in enumerate(results, 1):
            a = item.analysis
            summary = (a.summary[:120] if a and a.summary else item.text[:80].replace("\n", " "))
            tags    = "  ".join(a.tags[:4]) if a and a.tags else ""
            qs      = f"{a.quality_score:.2f}" if a else "—"
            handle  = f"@{item.author_handle}" if item.author_handle else item.author_name[:14]
            url     = item.url[-40:] if len(item.url) > 40 else item.url
            table.add_row(str(i), f"{score:.1f}", qs, handle, summary, tags, url)

        console.print(f"\n[bold]{len(results)} result(s)[/bold] for [italic]{query or '(all)'}[/italic]\n")
        console.print(table)
    else:
        # Plain fallback
        print(f"\n{len(results)} result(s) for '{query or '(all)'}':\n")
        for i, (score, item) in enumerate(results, 1):
            a = item.analysis
            summary = a.summary[:100] if a and a.summary else item.text[:80].replace("\n", " ")
            handle  = item.author_handle or item.author_name
            qs      = f"{a.quality_score:.2f}" if a else "—"
            print(f"  {i:2}. [{score:.1f}|{qs}] @{handle}: {summary}")
            print(f"      {item.url}")
            print()

    return 0


def cmd_run(args: argparse.Namespace, root: Path) -> int:
    """Run all enabled stages in order, skipping downstream stages if nothing changed."""
    from .ingest import run_ingest

    input_dir = root / "input"
    state_dir  = root / "state"
    paths = sorted(input_dir.glob("*.json")) if not getattr(args, "input", None) else [Path(p) for p in args.input]
    if not paths:
        print(f"No JSON files found in {input_dir}")
        return 1

    print(f"Ingesting {len(paths)} file(s)...")
    new, updated, skipped = run_ingest(paths, state_dir, root)
    print(f"  new={new}  updated={updated}  skipped={skipped}")

    if new == 0 and updated == 0 and not getattr(args, "force", False):
        print("Nothing changed — skipping fetch, analyze, cluster, build.")
        return 0

    for stage in [cmd_fetch, cmd_analyze, cmd_cluster, cmd_build]:
        rc = stage(args, root)
        if rc != 0:
            return rc
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="bookbuilder",
        description="Build a knowledge base from Twitter bookmarks and likes.",
    )
    parser.add_argument("--dry-run",  action="store_true")
    parser.add_argument("--verbose",  action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    # ingest
    p_ingest = sub.add_parser("ingest", help="Normalise and register input files")
    p_ingest.add_argument("--input",  nargs="+", metavar="FILE",
                          help="Explicit input JSON files (default: input/*.json)")
    p_ingest.add_argument("--source", choices=["bookmark", "like"],
                          help="Override source tag (default: inferred from filename)")

    # fetch
    p_fetch = sub.add_parser("fetch", help="Fetch linked pages and images")
    p_fetch.add_argument("--workers", type=int, default=None)
    p_fetch.add_argument("--force",   action="store_true",
                         help="Re-fetch even if already fetched")

    # analyze
    p_analyze = sub.add_parser("analyze", help="AI analysis pass")
    p_analyze.add_argument("--batch", type=int, default=None,
                           help="Override batch size from config")
    p_analyze.add_argument("--force", action="store_true")

    # cluster
    p_cluster = sub.add_parser("cluster", help="Embedding-based clustering")
    p_cluster.add_argument("--force", action="store_true")

    # build
    p_build = sub.add_parser("build", help="Generate knowledge/ and site/")
    p_build.add_argument("--force", action="store_true")

    # search
    p_search = sub.add_parser("search", help="Search the knowledge base from the terminal")
    p_search.add_argument("query",      nargs="*",       metavar="TERM",
                          help="Search terms (omit to list all items by quality)")
    p_search.add_argument("--limit",    type=int, default=20,
                          help="Maximum results (default 20)")
    p_search.add_argument("--min-score", dest="min_score", type=float, default=0.0,
                          help="Minimum quality score filter (0.0–1.0)")
    p_search.add_argument("--category", metavar="CAT",
                          help="Filter by taxonomy category prefix (e.g. ai_ml)")
    p_search.add_argument("--author",   metavar="HANDLE",
                          help="Filter by Twitter/X handle (without @)")
    p_search.add_argument("--tech",     metavar="TECH",
                          help="Filter by technology reference (e.g. pytorch)")

    # mcp
    sub.add_parser("mcp", help="Start the MCP server (stdio) for Claude integration")

    # run (all)
    sub.add_parser("run", help="Run all enabled stages in sequence")

    args = parser.parse_args()
    root = _project_root()

    dispatch = {
        "ingest":  cmd_ingest,
        "fetch":   cmd_fetch,
        "analyze": cmd_analyze,
        "cluster": cmd_cluster,
        "build":   cmd_build,
        "search":  cmd_search,
        "mcp":     cmd_mcp,
        "run":     cmd_run,
    }

    rc = dispatch[args.command](args, root)
    sys.exit(rc)


if __name__ == "__main__":
    main()
