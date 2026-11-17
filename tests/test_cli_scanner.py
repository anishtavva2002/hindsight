import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

import hindsight
from hindsight.cli import main
from hindsight.demo import build_demo_repo
from hindsight.scanner import scan_service

ROOT = Path(__file__).resolve().parent.parent


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(list(argv))
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = build_demo_repo(Path(cls.tmp.name) / "demo")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_version_matches_pyproject(self):
        m = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M)
        self.assertEqual(m.group(1), hindsight.__version__)

    def test_scan_json_and_gate(self):
        code, out, _ = run("scan", str(self.repo), "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["services"][0]["name"], "orders-service")
        code, _, _ = run("scan", str(self.repo), "--fail-on-high")
        self.assertEqual(code, 1)

    def test_scan_markdown(self):
        md = Path(self.tmp.name) / "r.md"
        code, _, _ = run("scan", str(self.repo), "--md", str(md))
        self.assertEqual(code, 0)
        self.assertIn("| Drill |", md.read_text())

    def test_why(self):
        code, out, _ = run("why", str(self.repo), "orders-service")
        self.assertEqual(code, 0)
        self.assertIn("fix: inventory call hangs checkout", out)

    def test_why_outside_git(self):
        with tempfile.TemporaryDirectory() as d:
            code, _, err = run("why", d, "x")
            self.assertEqual(code, 2)
            self.assertIn("error", err)

    def test_chaos_rejects_bad_rates(self):
        code, _, err = run("chaos", "--upstream", "127.0.0.1:1", "--error-rate", "0.8", "--reset-rate", "0.8")
        self.assertEqual(code, 2)
        self.assertIn("sum", err)

    def test_drill_all_without_config(self):
        code, _, err = run("drill-all", str(self.repo))
        self.assertEqual(code, 2)
        self.assertIn("no services configured", err)


class ScannerTests(unittest.TestCase):
    def scan(self, files):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for rel, text in files.items():
                p = root / "svc" / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text)
            return scan_service(root, "svc")

    def kinds(self, scan):
        return {f.kind for f in scan.findings}

    def test_java_timeout_configured_is_clean(self):
        s = self.scan({"A.java": (
            "class A { void f(){ rt.setConnectTimeout(1000); restTemplate.getForObject(u, X.class); } }\n"
            "// @Retryable CircuitBreaker fallbackMethod\n"
            "@Retryable @CircuitBreaker(name='x', fallbackMethod='fb') class B {}")})
        self.assertEqual(s.outbound_calls, 1)
        self.assertNotIn("missing-timeout", self.kinds(s))

    def test_comments_do_not_count_as_protection(self):
        s = self.scan({"A.java": "class A { void f(){ restTemplate.getForObject(u, X.class); } }\n// @Retryable CircuitBreaker\n"})
        self.assertIn("no-retry", self.kinds(s))
        self.assertIn("no-circuit-breaker", self.kinds(s))

    def test_python_requests_without_timeout(self):
        s = self.scan({"a.py": "import requests\nrequests.get('http://x')\n"})
        self.assertIn("missing-timeout", self.kinds(s))

    def test_python_requests_with_timeout(self):
        s = self.scan({"a.py": "import requests\nrequests.get('http://x', timeout=3)\n"})
        self.assertNotIn("missing-timeout", self.kinds(s))

    def test_python_timeout_none_is_flagged(self):
        s = self.scan({"a.py": "import requests\nrequests.get('http://x', timeout=None)\n"})
        self.assertIn("missing-timeout", self.kinds(s))

    def test_no_outbound_calls_no_findings(self):
        s = self.scan({"a.py": "def add(a, b):\n    return a + b\n"})
        self.assertEqual(s.findings, [])


if __name__ == "__main__":
    unittest.main()
