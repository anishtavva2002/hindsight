from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import __version__
from .chaos import ChaosProxy, FaultConfig
from .history import GitError, file_timeline, narrate
from .report import build_report, render_markdown, render_text


def _hostport(value: str, default_host: str):
    if ":" in value:
        host, port = value.rsplit(":", 1)
        return host or default_host, int(port)
    return default_host, int(value)


def cmd_drill_all(a) -> int:
    from .drill import render_text as render_drill
    from .runner import ConfigError, drill_service, load_config

    try:
        cfg = load_config(a.repo, a.config)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not cfg:
        print("error: no services configured (create hindsight.json, see `hindsight drill-all --help`)", file=sys.stderr)
        return 2
    rep = build_report(a.repo, since_days=a.since, config_path=a.config)
    order = [r["path"] for r in rep["services"] if r["path"] in cfg]  # riskiest first
    order += [k for k in cfg if k not in order]
    if a.only:
        order = [k for k in order if k in a.only]
    if a.top:
        order = order[: a.top]
    worst, failed = 101, False
    for key in order:
        print(f"\n=== {key} ===", flush=True)
        try:
            res = drill_service(a.repo, key, cfg[key], log=lambda m: print(f"  {m}", file=sys.stderr, flush=True))
        except (RuntimeError, OSError) as e:
            print(f"error: {e}")
            failed = True
            continue
        print(render_drill(res), flush=True)
        worst = min(worst, res["score"])
    if a.min_score and worst < a.min_score:
        failed = True
    return 1 if failed else 0


def cmd_scan(a) -> int:
    rep = build_report(a.repo, since_days=a.since, config_path=getattr(a, "config", None))
    if a.md:
        Path(a.md).write_text(render_markdown(rep, a.top), encoding="utf-8")
        print(f"wrote {a.md}", file=sys.stderr)
    print(json.dumps(rep, indent=2) if a.json else render_text(rep, a.top))
    if a.fail_on_high and any(f["severity"] == "HIGH" for r in rep["services"] for f in r["findings"]):
        return 1
    return 0


def cmd_why(a) -> int:
    try:
        events = file_timeline(Path(a.repo), a.path)
    except GitError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print("\n".join(narrate(a.path, events)))
    return 0


def cmd_demo(a) -> int:
    from .demo import build_demo_repo

    repo = build_demo_repo(a.dir)
    print(f"demo repo ready: {repo}\ntry: python -m hindsight scan {repo}")
    return 0


def cmd_serve(a) -> int:
    from .server import serve

    serve(a.repo, a.host, a.port, a.since, open_browser=not a.no_open, config=a.config)
    return 0


def cmd_chaos(a) -> int:
    lh, lp = _hostport(a.listen, "127.0.0.1")
    uh, up = _hostport(a.upstream, "127.0.0.1")
    faults = FaultConfig(
        latency_ms=a.latency, jitter_ms=a.jitter, error_rate=a.error_rate,
        reset_rate=a.reset_rate, corrupt_rate=a.corrupt_rate,
        blackhole_rate=a.blackhole_rate, seed=a.seed,
    )
    try:
        faults.validate()
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    proxy = ChaosProxy(lh, lp, uh, up, faults, logger=lambda m: print(m, flush=True))

    async def run() -> None:
        await proxy.start()
        print(f"chaos proxy {lh}:{proxy.listen_port} -> {uh}:{up} | faults: {faults.describe()}", flush=True)
        try:
            if a.duration:
                await asyncio.sleep(a.duration)
            else:
                await asyncio.Event().wait()
        finally:
            await proxy.stop()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    print("summary:", dict(proxy.stats))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="hindsight", description="Git-history risk + resilience scan + chaos proxy.")
    p.add_argument("--version", action="version", version=f"hindsight {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="rank services by history risk x resilience gaps")
    s.add_argument("repo")
    s.add_argument("--since", type=int, default=365, help="history window in days (default 365)")
    s.add_argument("--top", type=int, default=10)
    s.add_argument("--json", action="store_true")
    s.add_argument("--md", metavar="FILE", help="also write a markdown report")
    s.add_argument("--fail-on-high", action="store_true", help="exit 1 if any HIGH finding (CI gate)")
    s.add_argument("--config", help="path to hindsight.json (default: <repo>/hindsight.json)")
    s.set_defaults(fn=cmd_scan)

    w = sub.add_parser("why", help="timeline and story of a file")
    w.add_argument("repo")
    w.add_argument("path", help="path relative to the repo root")
    w.set_defaults(fn=cmd_why)

    v = sub.add_parser("serve", help="web UI: ranked services, timelines, chaos plans")
    v.add_argument("repo")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8765)
    v.add_argument("--since", type=int, default=365)
    v.add_argument("--no-open", action="store_true", help="don't open a browser")
    v.add_argument("--config", help="path to hindsight.json (default: <repo>/hindsight.json)")
    v.set_defaults(fn=cmd_serve)

    da = sub.add_parser("drill-all", help="drill every service configured in hindsight.json, riskiest first")
    da.add_argument("repo")
    da.add_argument("--config", help="path to hindsight.json (default: <repo>/hindsight.json)")
    da.add_argument("--since", type=int, default=365)
    da.add_argument("--top", type=int, default=0, help="only the N riskiest configured services")
    da.add_argument("--only", nargs="+", metavar="SERVICE", help="only these services")
    da.add_argument("--min-score", type=int, default=0, help="exit 1 if any score is below this (CI gate)")
    da.set_defaults(fn=cmd_drill_all)

    r = sub.add_parser("drill", help="run a chaos scenario suite against a service and score it")
    r.add_argument("--target", required=True, help="URL of the service to probe")
    r.add_argument("--upstream", required=True, help="host:port of the real dependency")
    r.add_argument("--listen", type=int, default=9001, help="proxy port (point the service's dependency URL here)")
    r.add_argument("--listen-host", default="127.0.0.1")
    r.add_argument("--requests", type=int, default=20, help="probes per scenario")
    r.add_argument("--concurrency", type=int, default=4)
    r.add_argument("--timeout", type=float, default=4.0, help="probe timeout in seconds")
    r.add_argument("--method", default="GET")
    r.add_argument("--data", help="request body")
    r.add_argument("--settle", type=float, default=3.0, help="pause between scenarios (s), lets breakers reset")
    r.add_argument("--seed", type=int, default=7)
    r.add_argument("--json", action="store_true")
    r.add_argument("--min-score", type=int, default=0, help="exit 1 if score is below this (CI gate)")
    r.set_defaults(fn=lambda a: __import__("hindsight.drill", fromlist=["cmd_drill"]).cmd_drill(a))

    r2 = sub.add_parser("drill-demo", help="drill a naive and a resilient sample service, side by side")
    r2.set_defaults(fn=lambda a: __import__("hindsight.drill", fromlist=["cmd_drill_demo"]).cmd_drill_demo(a))

    d = sub.add_parser("demo", help="create a synthetic multi-service repo to try things on")
    d.add_argument("dir")
    d.set_defaults(fn=cmd_demo)

    c = sub.add_parser("chaos", help="run a fault-injecting proxy in front of a dependency")
    c.add_argument("--listen", default="127.0.0.1:9001", help="[host:]port to listen on")
    c.add_argument("--upstream", required=True, help="host:port of the real dependency")
    c.add_argument("--latency", type=int, default=0, help="added latency per connection (ms)")
    c.add_argument("--jitter", type=int, default=0, help="± random jitter on latency (ms)")
    c.add_argument("--error-rate", type=float, default=0.0, help="fraction answered with HTTP 503")
    c.add_argument("--reset-rate", type=float, default=0.0, help="fraction dropped with a TCP reset")
    c.add_argument("--corrupt-rate", type=float, default=0.0, help="fraction with garbled response bodies")
    c.add_argument("--blackhole-rate", type=float, default=0.0, help="fraction accepted but never answered")
    c.add_argument("--duration", type=float, default=0, help="stop after N seconds (default: run until Ctrl-C)")
    c.add_argument("--seed", type=int, default=None)
    c.set_defaults(fn=cmd_chaos)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
