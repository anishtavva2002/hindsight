"""Sample services for `hindsight drill-demo` and tests: one naive, one resilient."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass  # clients that time out mid-request are expected during drills


def _serve(handler, port=0) -> ThreadingHTTPServer:
    srv = _QuietServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _reply(h: BaseHTTPRequestHandler, code: int, obj: dict) -> None:
    body = json.dumps(obj).encode()
    h.send_response(code)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Connection", "close")
    h.end_headers()
    h.wfile.write(body)


def start_dependency() -> ThreadingHTTPServer:
    class Dep(BaseHTTPRequestHandler):
        def do_GET(self):
            _reply(self, 200, {"stock": 42})

        def log_message(self, *a):
            pass

    return _serve(Dep)


def start_service(dep_port: int, mode: str, open_for: float = 2.0) -> ThreadingHTTPServer:
    """mode='naive': no timeout/retry/breaker/fallback.
    mode='resilient': 0.5s timeout, 3 tries with backoff, circuit breaker, fallback."""
    url = f"http://127.0.0.1:{dep_port}/stock"
    state = {"fails": 0, "open_until": 0.0}
    lock = threading.Lock()

    def naive():
        return json.loads(urlopen(url).read())

    def resilient():
        with lock:
            if time.monotonic() < state["open_until"]:
                raise RuntimeError("circuit open")
        last = None
        for attempt in range(3):
            try:
                data = json.loads(urlopen(url, timeout=0.5).read())
                with lock:
                    state["fails"] = 0
                return data
            except Exception as e:
                last = e
                time.sleep(0.05 * 2 ** attempt)
        with lock:
            state["fails"] += 1
            if state["fails"] >= 3:
                state["open_until"] = time.monotonic() + open_for
                state["fails"] = 0
        raise last

    call = naive if mode == "naive" else resilient

    class Svc(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                _reply(self, 200, {"ok": True, "dep": call()})
            except Exception:
                if mode == "resilient":
                    _reply(self, 200, {"ok": True, "degraded": True})
                else:
                    _reply(self, 500, {"ok": False})

        def log_message(self, *a):
            pass

    return _serve(Svc)
