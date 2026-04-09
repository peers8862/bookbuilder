"""
Inbox: reading list management.

Commands:
  bookbuilder inbox              — show unread items, highest quality first
  bookbuilder mark <id> <status> — set read_status on one or more items

Statuses: unread | read | want_to_read | archived
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

VALID_STATUSES = ("unread", "read", "want_to_read", "archived")


def run_mark(root: Path, item_ids: list[str], status: str) -> int:
    from .ingest import load_state, save_state

    if status not in VALID_STATUSES:
        print(f"Invalid status '{status}'. Choose from: {', '.join(VALID_STATUSES)}")
        return 1

    state_path = root / "state" / "items.jsonl"
    states = load_state(state_path)
    now = datetime.now(timezone.utc).isoformat()
    changed = 0

    for item_id in item_ids:
        # Support prefix matching — useful for short IDs
        matches = [k for k in states if k == item_id or k.startswith(item_id)]
        if not matches:
            print(f"  not found: {item_id}")
            continue
        for mid in matches:
            states[mid].read_status = status
            states[mid].read_at = now if status == "read" else ""
            print(f"  {mid[:12]}… → {status}")
            changed += 1

    if changed:
        save_state(states, state_path)
    return 0


def run_inbox(root: Path, status: str = "unread", limit: int = 20) -> int:
    from .ingest import load_state
    from .store import read_item

    try:
        from rich.console import Console
        from rich.table import Table
        from rich import box
        _rich = True
    except ImportError:
        _rich = False

    state_path = root / "state" / "items.jsonl"
    states = load_state(state_path)

    matching = [s for s in states.values() if s.read_status == status]

    # Load items to get quality scores for sorting
    scored: list[tuple[float, object]] = []
    for s in matching:
        item = read_item(root, s.item_id)
        if item is None:
            continue
        qs = item.analysis.quality_score if item.analysis else 0.0
        scored.append((qs, item))

    scored.sort(key=lambda x: -x[0])
    scored = scored[:limit]

    if not scored:
        print(f"No items with status '{status}'.")
        return 0

    if _rich:
        console = Console()
        table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan",
                      expand=True, padding=(0, 1))
        table.add_column("ID",      style="dim",       width=14, no_wrap=True)
        table.add_column("Score",   style="green",     width=5,  no_wrap=True)
        table.add_column("Author",  style="cyan",      width=16, no_wrap=True)
        table.add_column("Summary", ratio=3)
        table.add_column("URL",     style="blue dim",  width=24, no_wrap=True)

        for qs, item in scored:
            a = item.analysis
            summary = (a.summary[:100] if a and a.summary else item.text[:80].replace("\n", " "))
            handle  = f"@{item.author_handle}" if item.author_handle else item.author_name[:14]
            url     = item.url[-40:] if len(item.url) > 40 else item.url
            table.add_row(item.id[:12], f"{qs:.2f}", handle, summary, url)

        console.print(f"\n[bold]{len(scored)}[/bold] {status} items\n")
        console.print(table)
        console.print(f"\n[dim]bookbuilder mark <id> read|want_to_read|archived[/dim]\n")
    else:
        print(f"\n{len(scored)} {status} items:\n")
        for qs, item in scored:
            a = item.analysis
            summary = a.summary[:80] if a and a.summary else item.text[:60].replace("\n", " ")
            handle  = item.author_handle or item.author_name
            print(f"  {item.id[:12]}  [{qs:.2f}]  @{handle}")
            print(f"    {summary}")
            print(f"    {item.url}")
            print()

    return 0
