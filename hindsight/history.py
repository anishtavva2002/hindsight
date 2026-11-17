"""Mine git history for per-service risk signals and per-file timelines."""
from __future__ import annotations

import math
import os
import re
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

FIX_RE = re.compile(
    r"\b(fix(es|ed)?|bug|hotfix|revert(ed)?|incident|outage|timeout|"
    r"crash(es|ed)?|regression|rollback|patch)\b",
    re.I,
)
SERVICE_MARKERS = (
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "pyproject.toml",
    "setup.py",
    "package.json",
    "go.mod",
    "Dockerfile",
)
SKIP_DIRS = {".git", "node_modules", "target", "build", "dist", "__pycache__", ".venv", "venv"}
HALF_LIFE_DAYS = 90.0


class GitError(RuntimeError):
    pass


def git(repo: Path | str, *args: str) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as e:
        raise GitError("git is not installed") from e
    if r.returncode != 0:
        raise GitError(r.stderr.strip() or "git command failed")
    return r.stdout


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass
class ServiceHistory:
    name: str
    commits: int = 0
    fix_commits: int = 0
    lines_changed: int = 0
    authors: set = field(default_factory=set)
    recent_score: float = 0.0
    last_fix: str | None = None
    hot_files: Counter = field(default_factory=Counter)
    recent_fixes: list = field(default_factory=list)  # [(date, subject)], newest first

    def raw_risk(self) -> float:
        return self.recent_score + 0.3 * math.log1p(self.lines_changed)


def discover_services(repo: Path, max_depth: int = 3) -> list[str]:
    """A service is a directory holding a build/packaging marker. Aggregator
    directories (a parent pom next to child poms, or the repo root) are dropped
    in favour of their children."""
    repo = Path(repo).resolve()
    found: list[str] = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        rel = Path(root).relative_to(repo)
        if len(rel.parts) > max_depth:
            dirs[:] = []
            continue
        if any(m in files for m in SERVICE_MARKERS):
            found.append(rel.as_posix() if rel.parts else ".")

    def is_ancestor(a: str, b: str) -> bool:
        if a == ".":
            return b != "."
        return b.startswith(a + "/")

    services = [s for s in found if not any(is_ancestor(s, o) for o in found if o != s)]
    return sorted(services) or ["."]


def service_for(path: str, services: list[str]) -> str | None:
    best: str | None = None
    for s in services:
        if s == ".":
            if best is None:
                best = "."
        elif path == s or path.startswith(s + "/"):
            if best is None or best == "." or len(s) > len(best):
                best = s
    return best


def parse_log(raw: str) -> list[dict]:
    commits: list[dict] = []
    cur: dict | None = None
    for line in raw.splitlines():
        if line.startswith("@@"):
            parts = line[2:].split("\x1f", 3)
            if len(parts) < 4:
                cur = None
                continue
            cur = {
                "sha": parts[0],
                "at": int(parts[1]),
                "author": parts[2],
                "subject": parts[3],
                "files": [],
            }
            commits.append(cur)
        elif cur is not None and line.strip():
            bits = line.split("\t", 2)
            if len(bits) == 3:
                a, d, p = bits
                n = (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
                cur["files"].append((p, n))
    return commits


def mine(repo: Path, services: list[str], since_days: int = 365, now: float | None = None):
    now = time.time() if now is None else now
    hist = {s: ServiceHistory(name=s) for s in services}
    raw = git(
        repo,
        "log",
        f"--since={int(since_days)}.days.ago",
        "--no-renames",
        "--numstat",
        "--format=@@%H%x1f%at%x1f%an%x1f%s",
    )
    for c in parse_log(raw):  # newest first
        age = max(0.0, (now - c["at"]) / 86400.0)
        weight = 0.5 ** (age / HALF_LIFE_DAYS)
        is_fix = bool(FIX_RE.search(c["subject"]))
        touched: dict[str, int] = {}
        for path, n in c["files"]:
            s = service_for(path, services)
            if s is None:
                continue
            touched[s] = touched.get(s, 0) + n
            hist[s].hot_files[path] += 1
        for s, n in touched.items():
            h = hist[s]
            h.commits += 1
            h.lines_changed += n
            h.authors.add(c["author"])
            h.recent_score += weight * (3 if is_fix else 1)
            if is_fix:
                h.fix_commits += 1
                if h.last_fix is None:
                    h.last_fix = _day(c["at"])
                if len(h.recent_fixes) < 5:
                    h.recent_fixes.append((_day(c["at"]), c["subject"]))
    return hist


def file_timeline(repo: Path, path: str) -> list[dict]:
    """Commits touching `path` (following renames), newest first."""
    is_file = (Path(repo) / path).is_file()
    spec = [] if path in ("", ".") else ["--", path]
    raw = git(
        repo,
        "log",
        *(["--follow"] if is_file else []),
        "--numstat",
        "--format=@@%h%x1f%at%x1f%an%x1f%s",
        *spec,
    )
    events = []
    for c in parse_log(raw):
        events.append(
            {
                "sha": c["sha"],
                "date": _day(c["at"]),
                "author": c["author"],
                "subject": c["subject"],
                "fix": bool(FIX_RE.search(c["subject"])),
                "lines": sum(n for _, n in c["files"]),
            }
        )
    return events


def narrate(path: str, events: list[dict]) -> list[str]:
    if not events:
        return [f"No history found for {path}."]
    first, last = events[-1], events[0]
    fixes = [e for e in events if e["fix"]]
    authors = Counter(e["author"] for e in events)
    biggest = max(events, key=lambda e: e["lines"])
    out = [
        f"{path}: {len(events)} commits by {len(authors)} author(s), "
        f"{first['date']} -> {last['date']}.",
        f"Origin: {first['sha']} on {first['date']} by {first['author']}: \"{first['subject']}\".",
        f"{len(fixes)} of {len(events)} commits look like fixes "
        f"({round(100 * len(fixes) / len(events))}%).",
    ]
    if fixes:
        f = fixes[0]
        out.append(f"Most recent fix: {f['sha']} on {f['date']}: \"{f['subject']}\".")
    out.append(
        f"Biggest change: {biggest['sha']} on {biggest['date']} "
        f"({biggest['lines']} lines): \"{biggest['subject']}\"."
    )
    out.append("")
    out.append("Timeline (oldest first):")
    for e in reversed(events):
        mark = "FIX" if e["fix"] else "   "
        out.append(f"  {e['date']}  {e['sha']}  {mark}  {e['subject']}  ({e['author']})")
    return out
