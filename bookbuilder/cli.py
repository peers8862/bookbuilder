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


def cmd_run(args: argparse.Namespace, root: Path) -> int:
    """Run all enabled stages in order."""
    stages = [cmd_ingest, cmd_fetch, cmd_analyze, cmd_cluster, cmd_build]
    for stage in stages:
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
        "run":     cmd_run,
    }

    rc = dispatch[args.command](args, root)
    sys.exit(rc)


if __name__ == "__main__":
    main()
