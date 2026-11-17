"""Tiny local web UI: JSON API + one static page. Binds to localhost by default."""
from __future__ import annotations

import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .history import GitError, file_timeline
from .report import build_report
from .runner import drill_service, load_config_safe

STATIC = Path(__file__).parent / "static" / "index.html"
CACHE_TTL = 30.0


def make_server(repo, host="127.0.0.1", port=8765, since=365, config=None):
    repo = Path(repo).resolve()
    cache: dict = {"at": 0.0, "data": None}
    lock = threading.Lock()
    jobs: dict = {}
    job_lock = threading.Lock()

    def report(refresh=False):
        with lock:
            if refresh or cache["data"] is None or time.time() - cache["at"] > CACHE_TTL:
                cache["data"] = build_report(repo, since_days=since, config_path=config)
                cache["at"] = time.time()
            return cache["data"]

    def invalidate():
        with lock:
            cache["data"] = None

    def start_drill(key: str):
        cfg, err = load_config_safe(repo, config)
        if err:
            return 400, {"error": err}
        if key not in cfg:
            return 400, {"error": f"service '{key}' has no drill config"}
        with job_lock:
            if any(j["state"] == "running" for j in jobs.values()):
                return 409, {"error": "another drill is already running"}
            jobs[key] = {"state": "running", "started": time.time()}

        def work():
            try:
                drill_service(repo, key, cfg[key])
                jobs[key] = {"state": "done"}
            except Exception as e:  # report to UI, keep server alive
                jobs[key] = {"state": "error", "msg": f"{type(e).__name__}: {e}"}
            finally:
                invalidate()

        threading.Thread(target=work, daemon=True).start()
        return 202, {"started": True}

    class Handler(BaseHTTPRequestHandler):
        server_version = "Hindsight"

        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_POST(self):
            u = urlparse(self.path)
            # A custom header forces a CORS preflight, which we never answer, so a
            # random web page can't trigger drills against your localhost services.
            if self.headers.get("X-Hindsight") != "1":
                return self._json({"error": "missing X-Hindsight header"}, 403)
            if u.path == "/api/drill":
                key = (parse_qs(u.query).get("service") or [""])[0]
                code, body = start_drill(key)
                return self._json(body, code)
            self._json({"error": "not found"}, 404)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                if u.path == "/":
                    self._send(200, STATIC.read_bytes(), "text/html; charset=utf-8")
                elif u.path == "/api/report":
                    self._json(report(refresh="refresh" in q))
                elif u.path == "/api/drill/status":
                    with job_lock:
                        self._json({k: dict(v) for k, v in jobs.items()})
                elif u.path == "/api/timeline":
                    path = (q.get("path") or ["."])[0]
                    target = (repo / path).resolve()
                    if target != repo and repo not in target.parents:
                        return self._json({"error": "path outside repo"}, 400)
                    rel = "." if target == repo else target.relative_to(repo).as_posix()
                    self._json({"path": rel, "events": file_timeline(repo, rel)})
                else:
                    self._json({"error": "not found"}, 404)
            except GitError as e:
                self._json({"error": str(e)}, 500)
            except Exception as e:  # keep the server alive
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    return ThreadingHTTPServer((host, port), Handler)


def serve(repo, host="127.0.0.1", port=8765, since=365, open_browser=True, config=None):
    srv = make_server(repo, host, port, since, config)
    url = f"http://{host}:{srv.server_address[1]}/"
    print(f"Hindsight UI: {url}  (Ctrl-C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
