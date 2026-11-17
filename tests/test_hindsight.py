import asyncio
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from hindsight.chaos import ChaosProxy, FaultConfig
from hindsight.demo import build_demo_repo
from hindsight.history import discover_services, file_timeline
from hindsight.report import build_report

JAVA = "orders-service/src/main/java/com/demo/orders/InventoryClient.java"


class RepoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = build_demo_repo(Path(cls.tmp.name) / "demo")
        cls.rep = build_report(cls.repo)
        cls.by_name = {r["name"]: r for r in cls.rep["services"]}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_discovers_services(self):
        self.assertEqual(
            discover_services(self.repo),
            ["notifications-service", "orders-service", "payments-service"],
        )

    def test_ranking(self):
        names = [r["name"] for r in self.rep["services"]]
        self.assertEqual(names[0], "orders-service")
        self.assertEqual(names[-1], "payments-service")

    def test_orders_history(self):
        o = self.by_name["orders-service"]
        self.assertEqual(o["fix_commits"], 4)
        self.assertEqual(o["outbound_calls"], 2)

    def test_findings(self):
        kinds = lambda n: {f["kind"] for f in self.by_name[n]["findings"]}
        self.assertIn("missing-timeout", kinds("orders-service"))
        self.assertIn("missing-timeout", kinds("notifications-service"))
        self.assertEqual(kinds("payments-service"), set())

    def test_payments_protections(self):
        p = self.by_name["payments-service"]["protections"]
        self.assertTrue(all(p.values()))

    def test_timeline(self):
        events = file_timeline(self.repo, JAVA)
        self.assertEqual(len(events), 5)
        self.assertEqual(sum(e["fix"] for e in events), 4)


class ServerTests(unittest.TestCase):
    def test_api(self):
        import json
        from urllib.request import urlopen

        from hindsight.server import make_server

        with tempfile.TemporaryDirectory() as tmp:
            repo = build_demo_repo(Path(tmp) / "demo")
            srv = make_server(repo, port=0)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            try:
                rep = json.load(urlopen(base + "/api/report"))
                self.assertEqual(rep["services"][0]["name"], "orders-service")
                tl = json.load(urlopen(base + "/api/timeline?path=orders-service"))
                self.assertEqual(len(tl["events"]), 5)
                with self.assertRaises(HTTPError) as cm:
                    urlopen(base + "/api/timeline?path=../..")
                self.assertEqual(cm.exception.code, 400)
                self.assertIn(b"Hindsight", urlopen(base + "/").read())
            finally:
                srv.shutdown()
                srv.server_close()


class DrillTests(unittest.TestCase):
    def drill(self, mode):
        from hindsight.drill import ProxyThread, run_drill
        from hindsight.samples import start_dependency, start_service

        dep = start_dependency()
        px = ProxyThread("127.0.0.1", dep.server_address[1]).start()
        svc = start_service(px.port, mode, open_for=0.4)
        try:
            return run_drill(f"http://127.0.0.1:{svc.server_address[1]}/", px,
                             requests=10, concurrency=10, timeout=2.0, settle=0.5)
        finally:
            svc.shutdown(); svc.server_close(); px.stop(); dep.shutdown(); dep.server_close()

    def test_naive_vs_resilient(self):
        naive, good = self.drill("naive"), self.drill("resilient")
        self.assertLess(naive["score"], 40)
        self.assertGreaterEqual(good["score"], 85)
        by = {r["scenario"]: r for r in naive["scenarios"]}
        self.assertEqual(by["slow dependency"]["verdict"], "FAIL")
        self.assertEqual(by["black hole"]["verdict"], "FAIL")

    def test_unrouted_target_is_rejected(self):
        from hindsight.drill import ProxyThread, run_drill
        from hindsight.samples import start_dependency

        dep = start_dependency()  # target talks to dep directly, bypassing the proxy
        px = ProxyThread("127.0.0.1", dep.server_address[1]).start()
        try:
            with self.assertRaises(RuntimeError):
                run_drill(f"http://127.0.0.1:{dep.server_address[1]}/", px, requests=4, timeout=1, settle=0)
        finally:
            px.stop(); dep.shutdown(); dep.server_close()


def free_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LinkTests(unittest.TestCase):
    def setUp(self):
        import json

        from hindsight.samples import start_dependency, start_service

        self.tmp = tempfile.TemporaryDirectory()
        self.repo = build_demo_repo(Path(self.tmp.name) / "demo")
        self.dep = start_dependency()
        self.port = free_port()
        self.svc = start_service(self.port, "resilient", open_for=0.4)
        cfg = {"services": {"orders-service": {
            "target": f"http://127.0.0.1:{self.svc.server_address[1]}/",
            "upstream": f"127.0.0.1:{self.dep.server_address[1]}",
            "listen": self.port, "requests": 8, "concurrency": 8, "timeout": 2, "settle": 0.4}}}
        (self.repo / "hindsight.json").write_text(json.dumps(cfg))

    def tearDown(self):
        for s in (self.svc, self.dep):
            s.shutdown(); s.server_close()
        self.tmp.cleanup()

    def test_config_validation(self):
        from hindsight.runner import ConfigError, load_config

        bad = self.repo / "bad.json"
        bad.write_text('{"services": {"x": {"target": "ftp://h"}}}')
        with self.assertRaises(ConfigError):
            load_config(self.repo, bad)
        bad.write_text("not json")
        with self.assertRaises(ConfigError):
            load_config(self.repo, bad)
        self.assertEqual(load_config(self.repo / "nope"), {})

    def test_drill_feeds_report(self):
        from hindsight.runner import drill_service, load_config

        before = build_report(self.repo)
        self.assertEqual(before["services"][0]["priority_basis"], "static")
        res = drill_service(self.repo, "orders-service", load_config(self.repo)["orders-service"])
        self.assertGreaterEqual(res["score"], 85)
        after = build_report(self.repo)
        o = next(r for r in after["services"] if r["name"] == "orders-service")
        self.assertEqual(o["priority_basis"], "measured")
        self.assertGreaterEqual(o["drill"]["score"], 85)
        self.assertTrue(o["drill_configured"])
        p = next(r for r in after["services"] if r["name"] == "payments-service")
        self.assertIsNone(p["drill"])
        self.assertFalse(p["drill_configured"])

    def test_server_drill_job(self):
        import json
        from urllib.request import Request, urlopen

        from hindsight.server import make_server

        srv = make_server(self.repo, port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            def post(path, headers):
                return urlopen(Request(base + path, method="POST", headers=headers))

            with self.assertRaises(HTTPError) as cm:
                post("/api/drill?service=orders-service", {})
            self.assertEqual(cm.exception.code, 403)
            with self.assertRaises(HTTPError) as cm:
                post("/api/drill?service=payments-service", {"X-Hindsight": "1"})
            self.assertEqual(cm.exception.code, 400)
            self.assertEqual(post("/api/drill?service=orders-service", {"X-Hindsight": "1"}).status, 202)
            for _ in range(60):
                st = json.load(urlopen(base + "/api/drill/status"))
                if st["orders-service"]["state"] != "running":
                    break
                time.sleep(0.5)
            self.assertEqual(st["orders-service"]["state"], "done")
            rep = json.load(urlopen(base + "/api/report"))
            o = next(r for r in rep["services"] if r["name"] == "orders-service")
            self.assertGreaterEqual(o["drill"]["score"], 85)
        finally:
            srv.shutdown(); srv.server_close()


class ChaosTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.up_port = self.srv.server_address[1]

    async def asyncTearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    async def proxy(self, **kw):
        p = ChaosProxy("127.0.0.1", 0, "127.0.0.1", self.up_port, FaultConfig(**kw))
        await p.start()
        self.addAsyncCleanup(p.stop)
        return p

    @staticmethod
    def get(port, timeout=3):
        with urlopen(f"http://127.0.0.1:{port}/", timeout=timeout) as r:
            return r.status, r.read()

    async def test_passthrough(self):
        p = await self.proxy()
        status, body = await asyncio.to_thread(self.get, p.listen_port)
        self.assertEqual((status, body), (200, b'{"ok": true}'))
        self.assertEqual(p.stats["pass"], 1)

    async def test_error(self):
        p = await self.proxy(error_rate=1.0)
        with self.assertRaises(HTTPError) as cm:
            await asyncio.to_thread(self.get, p.listen_port)
        self.assertEqual(cm.exception.code, 503)

    async def test_latency(self):
        p = await self.proxy(latency_ms=300)
        t = time.monotonic()
        status, _ = await asyncio.to_thread(self.get, p.listen_port)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(time.monotonic() - t, 0.28)

    async def test_reset(self):
        p = await self.proxy(reset_rate=1.0)
        with self.assertRaises(Exception):
            await asyncio.to_thread(self.get, p.listen_port)
        self.assertEqual(p.stats["reset"], 1)

    async def test_corrupt(self):
        p = await self.proxy(corrupt_rate=1.0)
        status, body = await asyncio.to_thread(self.get, p.listen_port)
        self.assertEqual(status, 200)
        self.assertEqual(len(body), len(b'{"ok": true}'))
        self.assertNotEqual(body, b'{"ok": true}')

    async def test_blackhole(self):
        p = await self.proxy(blackhole_rate=1.0)
        with self.assertRaises(Exception):
            await asyncio.to_thread(self.get, p.listen_port, 0.5)

    async def test_upstream_down(self):
        p = ChaosProxy("127.0.0.1", 0, "127.0.0.1", 1, FaultConfig())
        await p.start()
        self.addAsyncCleanup(p.stop)
        with self.assertRaises(HTTPError) as cm:
            await asyncio.to_thread(self.get, p.listen_port)
        self.assertEqual(cm.exception.code, 502)

    def test_validate(self):
        with self.assertRaises(ValueError):
            FaultConfig(error_rate=0.7, reset_rate=0.6).validate()


if __name__ == "__main__":
    unittest.main()
