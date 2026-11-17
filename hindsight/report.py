"""Combine history risk and resilience gaps into a ranked report + chaos plan."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .history import GitError, ServiceHistory, discover_services, mine
from .runner import load_config_safe, load_drills, summarize
from .scanner import ServiceScan, scan_service

SEV_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def gap_score(scan: ServiceScan) -> int:
    """0-100: how exposed a service's outbound calls are."""
    if not scan.outbound_calls:
        return 0
    n_timeout = sum(1 for f in scan.findings if f.kind == "missing-timeout")
    score = min(40, 10 * n_timeout)
    if not scan.has_retry:
        score += 20
    if not scan.has_breaker:
        score += 25
    if not scan.has_fallback:
        score += 15
    return score


def chaos_plan(scan: ServiceScan) -> list[dict]:
    if not scan.outbound_calls:
        return []
    plan = []
    if not scan.timeout_ok:
        plan.append({"flags": "--latency 8000", "why": "Dependency hangs: does the caller give up, or block forever?"})
    if not scan.has_retry:
        plan.append({"flags": "--error-rate 0.3", "why": "30% transient 503s: does the caller recover or surface every blip?"})
    if not scan.has_breaker:
        plan.append({"flags": "--reset-rate 1.0", "why": "Dependency is down: does the caller fail fast and stop hammering it?"})
    if not scan.has_fallback:
        plan.append({"flags": "--error-rate 1.0", "why": "Hard outage: is there a degraded response, or a full failure?"})
    plan.append({"flags": "--corrupt-rate 0.2", "why": "Garbled response bodies: does the caller parse defensively?"})
    return plan


def build_report(repo, since_days: int = 365, now: float | None = None, config_path=None) -> dict:
    repo = Path(repo).resolve()
    cfg, config_error = load_config_safe(repo, config_path)
    drills = load_drills(repo)
    services = discover_services(repo)
    git_error = None
    try:
        hist = mine(repo, services, since_days, now)
    except GitError as e:
        git_error = str(e)
        hist = {s: ServiceHistory(name=s) for s in services}
    scans = {s: scan_service(repo, s) for s in services}
    raws = {s: hist[s].raw_risk() for s in services}
    top = max(raws.values(), default=0.0) or 1.0

    rows = []
    for s in services:
        h, sc = hist[s], scans[s]
        hr = round(100 * raws[s] / top, 1)
        gs = gap_score(sc)
        drill = summarize(drills.get(s))
        if drill:  # a measured score beats the static estimate
            priority = 0.5 * hr + 0.5 * (100 - drill["score"])
        else:
            priority = 0.5 * hr + 0.5 * gs if sc.outbound_calls else 0.3 * hr
        rows.append({
            "drill": drill,
            "drill_configured": s in cfg,
            "priority_basis": "measured" if drill else "static",
            "name": repo.name if s == "." else s,
            "path": s,
            "priority": round(priority, 1),
            "history_risk": hr,
            "gap_score": gs,
            "commits": h.commits,
            "fix_commits": h.fix_commits,
            "lines_changed": h.lines_changed,
            "authors": len(h.authors),
            "last_fix": h.last_fix,
            "hot_files": h.hot_files.most_common(5),
            "recent_fixes": h.recent_fixes,
            "outbound_calls": sc.outbound_calls,
            "protections": {
                "timeout": sc.timeout_ok if sc.outbound_calls else None,
                "retry": sc.has_retry,
                "circuit_breaker": sc.has_breaker,
                "fallback": sc.has_fallback,
            },
            "findings": [f.to_dict() for f in sorted(sc.findings, key=lambda f: SEV_ORDER[f.severity])],
            "chaos_plan": chaos_plan(sc),
        })
    rows.sort(key=lambda r: -r["priority"])
    return {
        "repo": str(repo),
        "since_days": since_days,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_error": git_error,
        "config_error": config_error,
        "services": rows,
    }


def render_text(rep: dict, top: int = 10) -> str:
    out = [f"Hindsight: {rep['repo']}  (history window: {rep['since_days']}d)"]
    if rep["git_error"]:
        out.append(f"! history unavailable: {rep['git_error']}")
    if rep.get("config_error"):
        out.append(f"! config problem: {rep['config_error']}")
    out += ["", f"{'#':>2}  {'SERVICE':<24} {'PRIORITY':>8} {'HISTORY':>8} {'GAPS':>5} {'CALLS':>5} {'FIXES':>5} {'DRILL':>7}"]
    rows = rep["services"][:top]
    for i, r in enumerate(rows, 1):
        d = r["drill"]
        drill = f"{d['score']} {d['grade']}" if d else "-"
        out.append(
            f"{i:>2}  {r['name']:<24.24} {r['priority']:>8.1f} {r['history_risk']:>8.1f} "
            f"{r['gap_score']:>5} {r['outbound_calls']:>5} {r['fix_commits']:>5} {drill:>7}"
        )
    findings = sorted((f for r in rows for f in r["findings"]), key=lambda f: SEV_ORDER[f["severity"]])
    if findings:
        out += ["", "Findings:"]
        for f in findings[:30]:
            out.append(f"  [{f['severity']:<6}] {f['file']}:{f['line']}  {f['message']}")
        if len(findings) > 30:
            out.append(f"  ... {len(findings) - 30} more (use --json or --md)")
    planned = [r for r in rows if r["chaos_plan"]][:3]
    if planned:
        out += ["", "Chaos plan (run: hindsight chaos --listen 9001 --upstream HOST:PORT <flags>):"]
        for r in planned:
            out.append(f"  {r['name']}: " + " | ".join(p["flags"] for p in r["chaos_plan"][:4]))
    return "\n".join(out)


def render_markdown(rep: dict, top: int = 10) -> str:
    out = [f"# Hindsight report: {Path(rep['repo']).name}", "",
           f"Generated {rep['generated']} · history window {rep['since_days']} days", ""]
    if rep["git_error"]:
        out += [f"> History unavailable: {rep['git_error']}", ""]
    out += ["| # | Service | Priority | History | Gaps | Calls | Fixes | Drill |", "|---|---|---|---|---|---|---|---|"]
    rows = rep["services"][:top]
    for i, r in enumerate(rows, 1):
        d = r["drill"]
        drill = "{} ({})".format(d["score"], d["grade"]) if d else "-"
        out.append(f"| {i} | {r['name']} | {r['priority']} | {r['history_risk']} | {r['gap_score']} | "
                   f"{r['outbound_calls']} | {r['fix_commits']} | {drill} |")
    for i, r in enumerate(rows, 1):
        p = r["protections"]
        def mark(v):
            return "n/a" if v is None else ("yes" if v else "NO")
        out += ["", f"## {i}. {r['name']}  (priority {r['priority']})", "",
                f"- History: {r['commits']} commits, {r['fix_commits']} look like fixes, "
                f"{r['lines_changed']} lines changed, {r['authors']} author(s). Last fix: {r['last_fix'] or 'none'}.",
                f"- Protections: timeout {mark(p['timeout'])}, retry {mark(p['retry'])}, "
                f"circuit breaker {mark(p['circuit_breaker'])}, fallback {mark(p['fallback'])}."]
        if r["hot_files"]:
            out += ["", "**Hot files (most-touched):**"]
            out += [f"- `{path}` ({n} commits)" for path, n in r["hot_files"]]
        if r["recent_fixes"]:
            out += ["", "**Recent fixes:**"]
            out += [f"- {d}: {s}" for d, s in r["recent_fixes"]]
        if r["findings"]:
            out += ["", "**Gaps:**"]
            out += [f"- **{f['severity']}** `{f['file']}:{f['line']}` {f['message']}" for f in r["findings"]]
        if r["chaos_plan"]:
            out += ["", "**Chaos plan** (point this service's dependency URL at the proxy):"]
            out += [f"- `hindsight chaos --listen 9001 --upstream HOST:PORT {c['flags']}` - {c['why']}"
                    for c in r["chaos_plan"]]
    return "\n".join(out) + "\n"
