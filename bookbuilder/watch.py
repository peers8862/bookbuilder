"""
Watch stage: poll input/ for new or modified files and trigger a pipeline run.

Uses mtime-based polling (no fsevents dependency) so it works everywhere.
Interval is configurable; default 60 seconds.

Also generates a launchd plist for macOS background scheduling.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


_INPUT_PATTERNS = ["*.json", "*.xml", "*.csv", "*.html", "*.htm",
                   "*.md", "*.txt", "*.docx"]


def _input_snapshot(input_dir: Path) -> dict[str, float]:
    """Return {filename: mtime} for all supported files in input/."""
    snapshot: dict[str, float] = {}
    for pat in _INPUT_PATTERNS:
        for p in input_dir.glob(pat):
            snapshot[p.name] = p.stat().st_mtime
    return snapshot


def run_watch(root: Path, interval: int = 60, run_once: bool = False) -> None:
    """
    Poll input/ every `interval` seconds. When files are added or modified,
    run `bookbuilder run` as a subprocess.

    run_once=True exits after the first triggered run (useful for launchd/cron).
    """
    input_dir = root / "input"
    input_dir.mkdir(exist_ok=True)

    print(f"bookbuilder watch — polling {input_dir} every {interval}s")
    print("Press Ctrl-C to stop.\n")

    last_snapshot = _input_snapshot(input_dir)

    while True:
        time.sleep(interval)
        current = _input_snapshot(input_dir)

        new_files     = [f for f in current if f not in last_snapshot]
        changed_files = [f for f in current if f in last_snapshot
                         and current[f] != last_snapshot[f]]

        if new_files or changed_files:
            if new_files:
                print(f"  new: {', '.join(new_files)}")
            if changed_files:
                print(f"  changed: {', '.join(changed_files)}")
            print("  triggering bookbuilder run...")
            result = subprocess.run(
                [sys.executable, "-m", "bookbuilder.cli", "run"],
                cwd=str(root),
            )
            if result.returncode != 0:
                print(f"  [watch] run exited with code {result.returncode}")
            last_snapshot = _input_snapshot(input_dir)
            if run_once:
                return
        else:
            last_snapshot = current


# ── launchd plist generation ──────────────────────────────────────────────────

_PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.bookbuilder.watch</string>

  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>-m</string>
    <string>bookbuilder.cli</string>
    <string>watch</string>
    <string>--run-once</string>
  </array>

  <key>WorkingDirectory</key>
  <string>{root}</string>

  <!-- Run every {interval_minutes} minutes -->
  <key>StartInterval</key>
  <integer>{interval_seconds}</integer>

  <key>StandardOutPath</key>
  <string>{root}/state/watch.log</string>

  <key>StandardErrorPath</key>
  <string>{root}/state/watch.log</string>

  <!-- Start immediately on load -->
  <key>RunAtLoad</key>
  <false/>

  <key>KeepAlive</key>
  <false/>
</dict>
</plist>
"""


def generate_plist(root: Path, interval_minutes: int = 15) -> Path:
    """
    Write a launchd plist to ~/Library/LaunchAgents/com.bookbuilder.watch.plist.
    Load it with: launchctl load ~/Library/LaunchAgents/com.bookbuilder.watch.plist
    """
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / "com.bookbuilder.watch.plist"

    content = _PLIST_TEMPLATE.format(
        python=sys.executable,
        root=str(root.resolve()),
        interval_minutes=interval_minutes,
        interval_seconds=interval_minutes * 60,
    )
    plist_path.write_text(content, encoding="utf-8")
    return plist_path
