"""Static scan for missing resilience patterns around outbound HTTP calls.

Heuristic, regex based. Covers Java/Kotlin (RestTemplate, WebClient, Feign,
HttpClient, OkHttp) and Python (requests, httpx, aiohttp, urllib).
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

CODE_EXT = {".java", ".kt", ".py"}
CONF_EXT = {".yml", ".yaml", ".properties", ".toml", ".cfg", ".conf"}
SKIP_DIRS = {
    ".git", "node_modules", "target", "build", "dist", "__pycache__",
    ".venv", "venv", ".idea", "test", "tests", "__tests__",
}
MAX_BYTES = 1_000_000

JAVA_CALL = re.compile(
    r"\b(?:restTemplate|webClient|restClient|httpClient|okHttpClient)\s*\.\s*"
    r"(?:getForObject|getForEntity|postForObject|postForEntity|patchForObject|exchange|"
    r"execute|put|delete|retrieve|send|newCall|get|post)\s*\("
    r"|new\s+RestTemplate\s*\(\s*\)\s*\.\s*\w+\s*\(",
    re.I,
)
JAVA_FEIGN = re.compile(r"@FeignClient\b")
JAVA_URLCONN = re.compile(r"\.openConnection\s*\(")
JAVA_TIMEOUT = re.compile(
    r"setConnectTimeout|setReadTimeout|connectTimeout|readTimeout|responseTimeout|"
    r"callTimeout|\.timeout\s*\(|TimeLimiter|RequestConfig",
    re.I,
)
CONF_TIMEOUT = re.compile(
    r"connect[-_.]?timeout|read[-_.]?timeout|response[-_.]?timeout|timeout[-_.]?duration",
    re.I,
)

PY_EXPLICIT = re.compile(r"\b(requests|httpx|aiohttp)\.(?:get|post|put|delete|patch|head|request)\s*\(")
PY_SESSION = re.compile(
    r"\b(?:self\.)?(?:session|client|http_client|http)\.(?:get|post|put|delete|patch|head)\s*\("
)
PY_URLOPEN = re.compile(r"\burlopen\s*\(")

RETRY = re.compile(
    r"@Retryable|@Retry\b|RetryTemplate|RetryConfig|Retry\.of|resilience4j[.\-_]retry|"
    r"spring-retry|\btenacity\b|@retry\b|\bretry\s*\(|max_retries|urllib3\.util\.retry|\bbackoff\b",
    re.I,
)
BREAKER = re.compile(r"@CircuitBreaker|CircuitBreaker|circuit[-_]?breaker|hystrix|pybreaker", re.I)
FALLBACK = re.compile(
    r"fallbackMethod|fallbackFactory|\bfallback\b|onErrorReturn|onErrorResume|@Fallback|HystrixCommand",
    re.I,
)


@dataclass
class Finding:
    service: str
    kind: str
    severity: str
    file: str
    line: int
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ServiceScan:
    name: str
    files_scanned: int = 0
    outbound_calls: int = 0
    has_retry: bool = False
    has_breaker: bool = False
    has_fallback: bool = False
    findings: list = field(default_factory=list)

    @property
    def timeout_ok(self) -> bool:
        return not any(f.kind == "missing-timeout" for f in self.findings)


def _strip_comments(text: str) -> str:
    keep = []
    for ln in text.splitlines():
        keep.append("" if ln.lstrip().startswith(("//", "#", "*", "/*")) else ln)
    return "\n".join(keep)


def _call_args(text: str, open_idx: int, limit: int = 1500) -> str:
    depth = 0
    end = min(len(text), open_idx + limit)
    for i in range(open_idx, end):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx : i + 1]
    return text[open_idx:end]


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _java_sites(text: str) -> list[int]:
    lines = set()
    for rx in (JAVA_CALL, JAVA_FEIGN, JAVA_URLCONN):
        for m in rx.finditer(text):
            lines.add(_line_of(text, m.start()))
    return sorted(lines)


def _py_timeout_ok(lib: str, args: str, text: str) -> bool:
    has_t = "timeout" in args
    none_t = re.search(r"timeout\s*=\s*None", args) is not None
    if lib in ("requests", "urllib"):
        return has_t and not none_t
    if lib == "aiohttp":
        return (has_t or "ClientTimeout" in text) and not none_t
    # httpx defaults to a 5s timeout; only explicit None disables it
    return not (none_t or re.search(r"timeout\s*=\s*None", text))


def _python_sites(text: str) -> list[tuple[int, str, bool]]:
    libs = [
        lib for lib in ("requests", "aiohttp", "httpx")
        if re.search(rf"^\s*(?:import|from)\s+{lib}\b", text, re.M)
    ]
    has_urllib = bool(re.search(r"^\s*(?:import|from)\s+urllib", text, re.M))
    out: list[tuple[int, str, bool]] = []
    seen: set[int] = set()

    def add(pos: int, lib: str, args: str) -> None:
        line = _line_of(text, pos)
        if line in seen:
            return
        seen.add(line)
        out.append((line, lib, _py_timeout_ok(lib, args, text)))

    for m in PY_EXPLICIT.finditer(text):
        add(m.start(), m.group(1), _call_args(text, m.end() - 1))
    if libs:
        for m in PY_SESSION.finditer(text):
            add(m.start(), libs[0], _call_args(text, m.end() - 1))
    if has_urllib:
        for m in PY_URLOPEN.finditer(text):
            add(m.start(), "urllib", _call_args(text, m.end() - 1))
    return sorted(out)


def scan_service(repo: Path, service: str) -> ServiceScan:
    repo = Path(repo).resolve()
    root = repo if service == "." else repo / service
    scan = ServiceScan(name=service)
    texts: list[tuple[str, str, str]] = []

    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in CODE_EXT and ext not in CONF_EXT:
                continue
            if fn.startswith("test_") or fn.endswith(("Test.java", "Tests.java", "IT.java", "_test.py")):
                continue
            p = Path(dirpath) / fn
            try:
                if p.stat().st_size > MAX_BYTES:
                    continue
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            texts.append((p.relative_to(repo).as_posix(), _strip_comments(text), ext))

    scan.files_scanned = len(texts)
    all_text = "\n".join(t for _, t, _ in texts)
    scan.has_retry = bool(RETRY.search(all_text))
    scan.has_breaker = bool(BREAKER.search(all_text))
    scan.has_fallback = bool(FALLBACK.search(all_text))

    # Java timeouts are usually set once in a config bean/properties file, so
    # treat them as service-wide to avoid flagging every caller.
    java_timeout = any(
        (JAVA_TIMEOUT.search(t) if ext in (".java", ".kt") else CONF_TIMEOUT.search(t))
        for _, t, ext in texts
        if ext in (".java", ".kt") or ext in CONF_EXT
    )

    first_site: tuple[str, int] | None = None
    for rel, text, ext in texts:
        if ext in (".java", ".kt"):
            for line in _java_sites(text):
                scan.outbound_calls += 1
                first_site = first_site or (rel, line)
                if not java_timeout:
                    scan.findings.append(Finding(
                        service, "missing-timeout", "HIGH", rel, line,
                        "Outbound HTTP call with no connect/read timeout configured in this service",
                    ))
        elif ext == ".py":
            for line, lib, ok in _python_sites(text):
                scan.outbound_calls += 1
                first_site = first_site or (rel, line)
                if not ok:
                    scan.findings.append(Finding(
                        service, "missing-timeout", "HIGH", rel, line,
                        f"{lib} call without an explicit timeout (can hang indefinitely)",
                    ))

    if scan.outbound_calls and first_site:
        rel, line = first_site
        if not scan.has_retry:
            scan.findings.append(Finding(
                service, "no-retry", "MEDIUM", rel, line,
                "No retry/backoff found; one transient failure fails the whole request",
            ))
        if not scan.has_breaker:
            scan.findings.append(Finding(
                service, "no-circuit-breaker", "MEDIUM", rel, line,
                "No circuit breaker found; a dead dependency gets hammered and callers pile up",
            ))
        if not scan.has_fallback:
            scan.findings.append(Finding(
                service, "no-fallback", "LOW", rel, line,
                "No fallback/degraded path found for failed calls",
            ))
    return scan
