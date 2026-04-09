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


def cmd_status(args: argparse.Namespace, root: Path) -> int:
    from .status import run_status
    run_status(root)
    return 0


def cmd_taxonomy(args: argparse.Namespace, root: Path) -> int:
    from .taxonomy import run_taxonomy_apply
    if getattr(args, "taxonomy_command", None) == "apply":
        dry_run = getattr(args, "dry_run", False)
        return run_taxonomy_apply(root, dry_run=dry_run)
    print("Usage: bookbuilder taxonomy apply [--dry-run]")
    return 1


def cmd_add(args: argparse.Namespace, root: Path) -> int:
    from .ingest import ingest_url
    state_dir = root / "state"
    for url in args.url:
        is_new, item_id = ingest_url(url, state_dir, root)
        status = "added" if is_new else "already exists"
        print(f"  {status}: {item_id}  {url}")
    return 0


def cmd_ingest(args: argparse.Namespace, root: Path) -> int:
    from .ingest import run_ingest

    input_dir = root / "input"
    state_dir  = root / "state"

    if args.input:
        paths = [Path(p) for p in args.input]
    else:
        patterns = ["*.json", "*.xml", "*.csv", "*.html", "*.htm", "*.md", "*.txt", "*.docx"]
        paths = sorted(p for pat in patterns for p in input_dir.glob(pat))
        if not paths:
            print(f"No input files found in {input_dir}")
            print("Drop your export files there and re-run, or use `bookbuilder add <url>`.")
            return 1

    print(f"Ingesting {len(paths)} file(s)...")
    new, updated, skipped = run_ingest(paths, state_dir, root)
    print(f"  new={new}  updated={updated}  skipped={skipped}")
    return 0


def cmd_fetch(args: argparse.Namespace, root: Path) -> int:
    from .fetch import run_fetch
    workers = getattr(args, "workers", None)
    force   = getattr(args, "force", False)
    stale   = getattr(args, "stale_after", None)
    print("Fetching linked pages and images...")
    ok, err, skip = run_fetch(root, force=force, workers=workers, stale_after_days=stale)
    print(f"  ok={ok}  errors={err}  skipped={skip}")
    return 0


def cmd_analyze(args: argparse.Namespace, root: Path) -> int:
    from .analyze import run_analyze
    batch = getattr(args, "batch", None)
    force = getattr(args, "force", False)
    since = getattr(args, "since", None)
    cluster_id = getattr(args, "cluster", None)
    print("Running AI analysis...")
    run_analyze(root, force=force, batch_size=batch, since=since, cluster_id=cluster_id)
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


def cmd_agents(args: argparse.Namespace, root: Path) -> int:
    from .agents import run_agents
    only = getattr(args, "only", None) or None
    force = getattr(args, "force", False)
    print("Running agents...")
    results = run_agents(root, only=only, force=force)
    return 0


def cmd_obsidian(args: argparse.Namespace, root: Path) -> int:
    from .obsidian import run_obsidian_export
    vault = Path(args.vault) if getattr(args, "vault", None) else None
    run_obsidian_export(
        root,
        vault_path=vault,
        write_items=not getattr(args, "no_items", False),
        min_score=getattr(args, "min_score", 0.0),
    )
    return 0


def cmd_inbox(args: argparse.Namespace, root: Path) -> int:
    from .inbox import run_inbox
    status = getattr(args, "status", "unread")
    limit  = getattr(args, "limit", 20)
    return run_inbox(root, status=status, limit=limit)


def cmd_mark(args: argparse.Namespace, root: Path) -> int:
    from .inbox import run_mark
    return run_mark(root, item_ids=args.id, status=args.status)


def cmd_watch(args: argparse.Namespace, root: Path) -> int:
    from .watch import run_watch, generate_plist
    subcmd = getattr(args, "watch_command", None)
    if subcmd == "install":
        interval = getattr(args, "interval", 15)
        path = generate_plist(root, interval_minutes=interval)
        print(f"  wrote {path}")
        print(f"  to enable: launchctl load {path}")
        print(f"  to disable: launchctl unload {path}")
        return 0
    interval = getattr(args, "interval", 60)
    run_once = getattr(args, "run_once", False)
    run_watch(root, interval=interval, run_once=run_once)
    return 0


def cmd_export(args: argparse.Namespace, root: Path) -> int:
    from .export import run_export
    output = Path(args.output) if getattr(args, "output", None) else None
    run_export(
        root,
        output=output,
        fmt=args.format,
        category=getattr(args, "category", None),
        author=getattr(args, "author", None),
        cluster_id=getattr(args, "cluster", None),
        min_score=args.min_score,
        limit=args.limit,
    )
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
    use_semantic = getattr(args, "semantic", False)

    if use_semantic:
        from .search import semantic_search
        results = semantic_search(
            root, query, limit=limit, min_quality=min_qs,
            category=category, author=author, tech=tech,
        )
    else:
        # Use in-memory index if built, fall back to file scan
        idx_path = root / "knowledge" / "index.json"
        if idx_path.exists() and not (category or author or tech):
            import json as _json
            from .mcp_server import _search_index, _index, _reload_index
            if _index is None:
                _reload_index.__func__ if hasattr(_reload_index, '__func__') else None
                import bookbuilder.mcp_server as _mcp
                _mcp._root = root
                _mcp._reload_index()
            raw = _search_index(query, limit=limit, min_quality=min_qs,
                                category=category, author=author, tech=tech)
            # raw records from index — convert to (score, item) shape for display
            # by loading full items only for the matched IDs
            from .store import read_item as _read_item
            results = []
            for rec in raw:
                item = _read_item(root, rec["id"])
                if item:
                    results.append((float(rec.get("score", 0)), item))
        else:
            results = search_items(
                root, query, limit=limit, min_quality=min_qs,
                category=category, author=author, tech=tech,
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
    """Run all enabled stages in order with per-stage dirty detection."""
    from .ingest import run_ingest
    from .fetch import run_fetch
    from .analyze import run_analyze
    from .cluster import run_cluster
    from .build import run_build
    from .agents import run_agents

    input_dir = root / "input"
    state_dir  = root / "state"
    force = getattr(args, "force", False)

    patterns = ["*.json", "*.xml", "*.csv", "*.html", "*.htm", "*.md", "*.txt", "*.docx"]
    paths = sorted(p for pat in patterns for p in input_dir.glob(pat)) \
            if not getattr(args, "input", None) else [Path(p) for p in args.input]
    if not paths:
        print(f"No input files found in {input_dir}")
        return 1

    # ── Ingest ────────────────────────────────────────────────────────────────
    print(f"Ingesting {len(paths)} file(s)...")
    new, updated, skipped = run_ingest(paths, state_dir, root)
    print(f"  new={new}  updated={updated}  skipped={skipped}")
    ingest_changed = new > 0 or updated > 0

    # ── Fetch ─────────────────────────────────────────────────────────────────
    # Run if new items exist OR if there are pending fetches from prior runs
    from .ingest import load_state as _load_state
    states = _load_state(state_dir / "items.jsonl")
    fetch_pending = any(s.fetch_status == "pending" for s in states.values())
    if force or ingest_changed or fetch_pending:
        print("Fetching linked pages and images...")
        stale = getattr(args, "stale_after", None)
        ok, err, skip = run_fetch(root, force=force, stale_after_days=stale)
        print(f"  ok={ok}  errors={err}  skipped={skip}")
        fetch_changed = ok > 0
    else:
        print("Fetch: nothing pending, skipped")
        fetch_changed = False

    # ── Analyze ───────────────────────────────────────────────────────────────
    states = _load_state(state_dir / "items.jsonl")
    analyze_pending = any(s.analyze_status == "pending" for s in states.values())
    if force or ingest_changed or fetch_changed or analyze_pending:
        print("Running AI analysis...")
        analyzed, errors = run_analyze(root, force=force)
        analyze_changed = analyzed > 0
    else:
        print("Analyze: nothing pending, skipped")
        analyze_changed = False

    # ── Cluster ───────────────────────────────────────────────────────────────
    # Run if new items were analyzed, or clusters.json doesn't exist yet
    clusters_exist = (root / "system" / "clusters.json").exists()
    if force or analyze_changed or not clusters_exist:
        print("Clustering items...")
        n = run_cluster(root, force=force)
        print(f"  {n} clusters written to system/clusters.json")
        cluster_changed = n > 0
    else:
        print("Cluster: no new analyzed items, skipped")
        cluster_changed = False

    # ── Build ─────────────────────────────────────────────────────────────────
    if force or ingest_changed or fetch_changed or analyze_changed or cluster_changed:
        print("Building knowledge base and site...")
        run_build(root, force=force)
        build_changed = True
    else:
        print("Build: nothing changed, skipped")
        build_changed = False

    # ── Agents ────────────────────────────────────────────────────────────────
    if force or analyze_changed or cluster_changed or build_changed:
        print("Running agents...")
        run_agents(root, force=force)
    else:
        print("Agents: nothing changed, skipped")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="bookbuilder",
        description="Build a knowledge base from Twitter bookmarks and likes.",
    )
    parser.add_argument("--dry-run",  action="store_true")
    parser.add_argument("--verbose",  action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    # status
    sub.add_parser("status", help="Show corpus health and pipeline stage counts")

    # taxonomy
    p_tax = sub.add_parser("taxonomy", help="Manage taxonomy")
    tax_sub = p_tax.add_subparsers(dest="taxonomy_command", required=True)
    p_tax_apply = tax_sub.add_parser("apply", help="Interactively apply proposals from taxonomy_proposals.md")
    p_tax_apply.add_argument("--dry-run", action="store_true", help="Show what would be applied without writing")

    # add
    p_add = sub.add_parser("add", help="Ingest one or more URLs directly")
    p_add.add_argument("url", nargs="+", metavar="URL")

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
    p_fetch.add_argument("--stale-after", dest="stale_after", type=int, default=None,
                         metavar="DAYS",
                         help="Re-fetch pages last fetched more than N days ago")

    # analyze
    p_analyze = sub.add_parser("analyze", help="AI analysis pass")
    p_analyze.add_argument("--batch", type=int, default=None,
                           help="Override batch size from config")
    p_analyze.add_argument("--force", action="store_true")
    p_analyze.add_argument("--since", metavar="DATE",
                           help="Only analyze items ingested on or after DATE (YYYY-MM-DD)")
    p_analyze.add_argument("--cluster", metavar="CLUSTER_ID",
                           help="Only analyze items in this cluster")

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
    p_search.add_argument("--semantic", action="store_true",
                          help="Use embedding similarity search instead of keyword scoring")

    # agents
    p_agents = sub.add_parser("agents", help="Run multi-agent analysis pass")
    p_agents.add_argument("--only", nargs="+", metavar="AGENT",
                          help="Run only these agents (item_critic, cluster_analyst, digest_writer, highlight_curator, connection_finder, gap_detector, taxonomy_evolver)")
    p_agents.add_argument("--force", action="store_true")

    # obsidian
    p_obs = sub.add_parser("obsidian", help="Export knowledge base as an Obsidian vault")
    p_obs.add_argument("--vault", metavar="PATH",
                       help="Vault output path (default: vault/ in project root)")
    p_obs.add_argument("--no-items", dest="no_items", action="store_true",
                       help="Skip individual item notes (faster, smaller vault)")
    p_obs.add_argument("--min-score", dest="min_score", type=float, default=0.0,
                       help="Only export items above this quality score")

    # inbox
    p_inbox = sub.add_parser("inbox", help="Show reading list by status")
    p_inbox.add_argument("--status", default="unread",
                         choices=["unread", "read", "want_to_read", "archived"],
                         help="Filter by read status (default: unread)")
    p_inbox.add_argument("--limit", type=int, default=20)

    # mark
    p_mark = sub.add_parser("mark", help="Set read status on items")
    p_mark.add_argument("id", nargs="+", metavar="ID",
                        help="Item ID(s) or prefixes")
    p_mark.add_argument("status", choices=["unread", "read", "want_to_read", "archived"])

    # watch
    p_watch = sub.add_parser("watch", help="Poll input/ and run pipeline when files change")
    p_watch.add_argument("--interval", type=int, default=60, metavar="SECONDS",
                         help="Poll interval in seconds (default 60)")
    p_watch.add_argument("--run-once", dest="run_once", action="store_true",
                         help="Trigger once if changes found, then exit (for cron/launchd)")
    watch_sub = p_watch.add_subparsers(dest="watch_command")
    p_watch_install = watch_sub.add_parser("install",
                         help="Install a launchd plist for background scheduling (macOS)")
    p_watch_install.add_argument("--interval", type=int, default=15, metavar="MINUTES",
                                 help="Run interval in minutes (default 15)")

    # export
    p_export = sub.add_parser("export", help="Generate a long-form markdown or HTML digest")
    p_export.add_argument("--format", choices=["md", "html"], default="md",
                          help="Output format (default: md)")
    p_export.add_argument("--output", metavar="FILE",
                          help="Output file path (default: auto-named in project root)")
    p_export.add_argument("--category", metavar="CAT",
                          help="Filter to a taxonomy category prefix")
    p_export.add_argument("--author",   metavar="HANDLE",
                          help="Filter to a specific author handle")
    p_export.add_argument("--cluster",  metavar="CLUSTER_ID",
                          help="Filter to a specific cluster")
    p_export.add_argument("--min-score", dest="min_score", type=float, default=0.6,
                          help="Minimum quality score (default 0.6)")
    p_export.add_argument("--limit", type=int, default=200,
                          help="Maximum items to include (default 200)")

    # mcp
    sub.add_parser("mcp", help="Start the MCP server (stdio) for Claude integration")

    # run (all)
    p_run = sub.add_parser("run", help="Run all enabled stages in sequence")
    p_run.add_argument("--stale-after", dest="stale_after", type=int, default=None,
                       metavar="DAYS", help="Re-fetch pages older than N days")
    p_run.add_argument("--force", action="store_true")

    args = parser.parse_args()
    root = _project_root()

    dispatch = {
        "status":   cmd_status,
        "taxonomy": cmd_taxonomy,
        "add":      cmd_add,
        "ingest":   cmd_ingest,
        "fetch":    cmd_fetch,
        "analyze":  cmd_analyze,
        "cluster":  cmd_cluster,
        "build":    cmd_build,
        "agents":   cmd_agents,
        "obsidian":  cmd_obsidian,
        "inbox":    cmd_inbox,
        "mark":     cmd_mark,
        "watch":    cmd_watch,
        "export":   cmd_export,
        "search":   cmd_search,
        "mcp":      cmd_mcp,
        "run":      cmd_run,
    }

    rc = dispatch[args.command](args, root)
    sys.exit(rc)


if __name__ == "__main__":
    main()
