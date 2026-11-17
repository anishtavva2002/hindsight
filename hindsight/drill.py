"""Automated chaos drill: run a scenario suite through the chaos proxy while probing
a target service, then score how it behaved.

The target must reach its dependency *through* the proxy (point its dependency URL
at the proxy's listen address).
"""
from __future__ import annotations

import asyncio
import http.client
import json
import random
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .chaos import ChaosProxy, FaultConfig


class ProxyThread:
    """Runs a ChaosProxy on a background event loop so blocking probes can run beside it."""

    def __init__(self, upstream_host, upstream_port, listen_host="127.0.0.1", listen_port=0, seed=7):
        self.seed = seed
        self.proxy = ChaosProxy(listen_host, listen_port, upstream_host, upstream_port, FaultConfig(seed=seed))
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)

    @property
    def port(self) -> int:
        return self.proxy.listen_port

    def start(self) -> "ProxyThread":
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self.proxy.start(), self._loop).result(10)
        return self

    def set_faults(self, faults: FaultConfig) -> None:
        faults.seed = self.seed
        faults.validate()
        self.proxy.faults = faults
        self.proxy._rng = random.Random(self.seed)

    def connections(self) -> int:
        return self.proxy.stats["connections"]

    def stop(self) -> None:
        try:
            asyncio.run_coroutine_threadsafe(self.proxy.stop(), self._loop).result(10)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(5)
            if not self._thread.is_alive():
                self._loop.close()


def probe(url: str, method: str, data: bytes | None, timeout: float) -> dict:
    t = time.monotonic()
    try:
        with urlopen(Request(url, data=data, method=method), timeout=timeout) as r:
            r.read()
            return {"status": r.status, "secs": time.monotonic() - t, "err": None}
    except HTTPError as e:
        try:
            e.read()
        except Exception:
            pass
        return {"status": e.code, "secs": time.monotonic() - t, "err": None}
    except (socket.timeout, TimeoutError):
        return {"status": None, "secs": time.monotonic() - t, "err": "timeout"}
    except URLError as e:
        kind = "timeout" if isinstance(e.reason, (socket.timeout, TimeoutError)) else "conn"
        return {"status": None, "secs": time.monotonic() - t, "err": kind}
    except (OSError, http.client.HTTPException):
        return {"status": None, "secs": time.monotonic() - t, "err": "conn"}


def run_probes(url, n, concurrency, timeout, method="GET", data=None, ramp=0) -> list[dict]:
    """`ramp` probes run one at a time first, so breakers can trip before the burst."""
    ramp = min(ramp, n)
    results = [probe(url, method, data, timeout) for _ in range(ramp)]
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        results += list(ex.map(lambda _: probe(url, method, data, timeout), range(n - ramp)))
    return results


def metrics(results: list[dict], hits: int) -> dict:
    n = len(results)
    ok = sum(1 for r in results if r["status"] and 200 <= r["status"] < 300)
    s5 = sum(1 for r in results if r["status"] and r["status"] >= 500)
    to = sum(1 for r in results if r["err"] == "timeout")
    secs = sorted(r["secs"] for r in results)
    p95 = secs[min(n - 1, int(0.95 * n))] if n else 0.0
    return {
        "requests": n, "ok_rate": ok / n if n else 0.0, "ok": ok, "errors_5xx": s5,
        "timeouts": to, "conn_errors": sum(1 for r in results if r["err"] == "conn"),
        "p95_secs": round(p95, 2), "dep_hits": hits, "hits_per_req": round(hits / n, 2) if n else 0.0,
    }


def scenarios(timeout: float) -> list[tuple[str, str, FaultConfig]]:
    slow_ms = int(timeout * 1000 * 1.5)
    return [
        ("baseline", "no faults", FaultConfig()),
        ("slow dependency", f"+{slow_ms}ms on every call", FaultConfig(latency_ms=slow_ms)),
        ("flaky 30%", "30% of calls get HTTP 503", FaultConfig(error_rate=0.3)),
        ("outage (reset)", "every connection dropped", FaultConfig(reset_rate=1.0)),
        ("outage (503)", "every call answered 503", FaultConfig(error_rate=1.0)),
        ("corrupt bodies", "every response body garbled", FaultConfig(corrupt_rate=1.0)),
        ("black hole", "accepted, never answered", FaultConfig(blackhole_rate=1.0)),
    ]


def evaluate(name: str, m: dict) -> tuple[float, str, str]:
    """-> (points 0..1, verdict, note)"""
    hung = m["timeouts"] > 0
    graceful = m["ok_rate"] >= 0.5
    note_hits = f"{m['hits_per_req']} dep calls/req"

    if name in ("slow dependency", "black hole"):
        if hung:
            return 0.0, "FAIL", f"{m['timeouts']}/{m['requests']} requests hung (no timeout on dependency call)"
        if graceful:
            return 1.0, "PASS", f"bounded, degraded OK (p95 {m['p95_secs']}s)"
        return 0.6, "PARTIAL", f"fails fast but no fallback (p95 {m['p95_secs']}s)"

    if name == "flaky 30%":
        if m["ok_rate"] >= 0.8:
            return 1.0, "PASS", f"{m['ok_rate']:.0%} success, failures masked ({note_hits})"
        return round(m["ok_rate"] * 0.5, 2), "FAIL", f"only {m['ok_rate']:.0%} success, blips reach callers (no retry)"

    if name in ("outage (reset)", "outage (503)"):
        if hung:
            return 0.0, "FAIL", f"{m['timeouts']}/{m['requests']} requests hung"
        pts, verdict, note = (1.0, "PASS", f"degraded OK ({note_hits})") if graceful else (
            0.5, "PARTIAL", f"fails fast, no fallback ({note_hits})")
        if m["hits_per_req"] > 2.5:
            return max(0.0, pts - 0.25), "PARTIAL", f"retry storm, no circuit breaker ({note_hits})"
        return pts, verdict, note

    if name == "corrupt bodies":
        if hung:
            return 0.0, "FAIL", "hung on garbled response"
        if graceful:
            return 1.0, "PASS", "garbled data handled"
        if m["errors_5xx"]:
            return 0.3, "FAIL", f"{m['errors_5xx']}/{m['requests']} unhandled errors (5xx) on bad payloads"
        return 0.5, "PARTIAL", "rejected cleanly, no fallback"
    return 0.0, "FAIL", "unknown scenario"


def grade(score: float) -> str:
    return "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D" if score >= 40 else "F"


def run_drill(target, proxy: ProxyThread, requests=20, concurrency=4, timeout=4.0,
              method="GET", data=None, settle=3.0, log=None) -> dict:
    log = log or (lambda m: None)
    rows = []
    for name, desc, faults in scenarios(timeout):
        proxy.set_faults(faults)
        before = proxy.connections()
        ramp = 4 if name.startswith("outage") and requests >= 8 else 0
        res = run_probes(target, requests, concurrency, timeout, method, data, ramp)
        m = metrics(res, proxy.connections() - before)
        if name == "baseline":
            if m["ok_rate"] < 0.9:
                raise RuntimeError(f"baseline failed ({m['ok_rate']:.0%} success): target unhealthy or unreachable")
            if m["dep_hits"] == 0:
                raise RuntimeError("target never called the dependency through the proxy; "
                                   f"point its dependency URL at 127.0.0.1:{proxy.port}")
            rows.append({"scenario": name, "desc": desc, "points": None, "verdict": "OK", "note": "healthy", "metrics": m})
        else:
            pts, verdict, note = evaluate(name, m)
            rows.append({"scenario": name, "desc": desc, "points": pts, "verdict": verdict, "note": note, "metrics": m})
        log(f"{name}: {rows[-1]['verdict']}")
        proxy.set_faults(FaultConfig())
        time.sleep(settle)  # let breakers/backoffs reset between scenarios
    scored = [r["points"] for r in rows if r["points"] is not None]
    score = round(100 * sum(scored) / len(scored)) if scored else 0
    return {"target": target, "score": score, "grade": grade(score), "scenarios": rows}


def render_text(rep: dict) -> str:
    out = [f"Drill: {rep['target']}", "", f"{'SCENARIO':<17} {'VERDICT':<8} {'PTS':>4}  NOTES"]
    for r in rep["scenarios"]:
        pts = "-" if r["points"] is None else f"{r['points']:.2f}"
        out.append(f"{r['scenario']:<17} {r['verdict']:<8} {pts:>4}  {r['note']}")
    out += ["", f"Resilience score: {rep['score']}/100  (grade {rep['grade']})"]
    return "\n".join(out)


def _print_err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def cmd_drill(a) -> int:
    uh, up = _hostport(a.upstream)
    px = ProxyThread(uh, up, a.listen_host, a.listen, seed=a.seed).start()
    print(f"proxy listening on {a.listen_host}:{px.port} -> {uh}:{up}", file=sys.stderr)
    try:
        rep = run_drill(a.target, px, a.requests, a.concurrency, a.timeout, a.method,
                        a.data.encode() if a.data else None, a.settle, log=_print_err)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        px.stop()
    print(json.dumps(rep, indent=2) if a.json else render_text(rep))
    return 1 if a.min_score and rep["score"] < a.min_score else 0


def cmd_drill_demo(a) -> int:
    from .samples import start_dependency, start_service

    dep = start_dependency()
    px = ProxyThread("127.0.0.1", dep.server_address[1]).start()
    try:
        for mode in ("naive", "resilient"):
            svc = start_service(px.port, mode, open_for=2.0)
            url = f"http://127.0.0.1:{svc.server_address[1]}/"
            print(f"\n=== sample service: {mode} ===", flush=True)
            rep = run_drill(url, px, requests=10, concurrency=10, timeout=3.0, settle=2.2, log=_print_err)
            print(render_text(rep), flush=True)
            svc.shutdown()
            svc.server_close()
    finally:
        px.stop()
        dep.shutdown()
        dep.server_close()
    return 0


def _hostport(value: str):
    host, _, port = value.rpartition(":")
    return host or "127.0.0.1", int(port)
