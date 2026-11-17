"""Config + persistence for drills tied to services found by `scan`.

hindsight.json (repo root):
{
  "services": {
    "orders-service": {
      "target": "http://localhost:8080/checkout",
      "upstream": "localhost:9000",
      "listen": 9001,
      "requests": 20, "concurrency": 4, "timeout": 4, "settle": 3
    }
  }
}
Keys are service paths as shown by `hindsight scan`. The service's dependency URL
must point at the `listen` port (default 9001).
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from .drill import ProxyThread, _hostport, run_drill

_lock = threading.Lock()


class ConfigError(ValueError):
    pass


def config_path(repo, override=None) -> Path:
    return Path(override) if override else Path(repo) / "hindsight.json"


def load_config(repo, override=None) -> dict:
    p = config_path(repo, override)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigError(f"{p}: {e}") from e
    services = raw.get("services") if isinstance(raw, dict) else None
    if not isinstance(services, dict):
        raise ConfigError(f'{p}: expected {{"services": {{...}}}}')
    for key, c in services.items():
        if not isinstance(c, dict) or not isinstance(c.get("target"), str) or not isinstance(c.get("upstream"), str):
            raise ConfigError(f'{p}: service "{key}" needs string "target" and "upstream"')
        if not c["target"].startswith(("http://", "https://")):
            raise ConfigError(f'{p}: service "{key}" target must be an http(s) URL')
        try:
            _hostport(c["upstream"])
        except ValueError as e:
            raise ConfigError(f'{p}: service "{key}" upstream must be host:port') from e
    return services


def load_config_safe(repo, override=None) -> tuple[dict, str | None]:
    try:
        return load_config(repo, override), None
    except ConfigError as e:
        return {}, str(e)


def _store(repo) -> Path:
    return Path(repo) / ".hindsight" / "drills.json"


def load_drills(repo) -> dict:
    try:
        return json.loads(_store(repo).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_drill(repo, key: str, result: dict) -> None:
    with _lock:
        data = load_drills(repo)
        data[key] = result
        p = _store(repo)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, p)


def drill_service(repo, key: str, cfg: dict, log=None) -> dict:
    """Run the drill for one configured service and persist the result."""
    uh, up = _hostport(cfg["upstream"])
    px = ProxyThread(uh, up, cfg.get("listen_host", "127.0.0.1"), int(cfg.get("listen", 9001)),
                     seed=int(cfg.get("seed", 7))).start()
    try:
        data = cfg.get("data")
        res = run_drill(
            cfg["target"], px,
            requests=int(cfg.get("requests", 20)), concurrency=int(cfg.get("concurrency", 4)),
            timeout=float(cfg.get("timeout", 4.0)), method=cfg.get("method", "GET"),
            data=data.encode() if data else None, settle=float(cfg.get("settle", 3.0)), log=log,
        )
    finally:
        px.stop()
    res["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_drill(repo, key, res)
    return res


def summarize(d: dict | None) -> dict | None:
    if not d:
        return None
    return {
        "at": d.get("at"), "score": d["score"], "grade": d["grade"], "target": d.get("target"),
        "scenarios": [
            {"scenario": s["scenario"], "verdict": s["verdict"], "points": s["points"], "note": s["note"]}
            for s in d["scenarios"]
        ],
    }
