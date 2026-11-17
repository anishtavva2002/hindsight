"""Build a small synthetic multi-service git repo for trying Hindsight."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

POM = "<project><artifactId>{name}</artifactId></project>\n"
PYPROJECT = '[project]\nname = "{name}"\nversion = "0.1.0"\n'

INVENTORY_CLIENT = """package com.demo.orders;

import org.springframework.web.client.RestTemplate;

public class InventoryClient {
    private final RestTemplate restTemplate = new RestTemplate();

    public Stock check(String sku) {
        return restTemplate.getForObject("http://inventory/stock/" + sku, Stock.class);
    }

    public void reserve(String sku, int qty) {
        restTemplate.postForObject("http://inventory/reserve", new Reserve(sku, qty), Void.class);
    }
}
"""

PAYMENTS_APP = '''import pybreaker
import requests
from tenacity import retry, stop_after_attempt, wait_exponential

breaker = pybreaker.CircuitBreaker(fail_max=5, reset_timeout=30)


@retry(stop=stop_after_attempt(3), wait=wait_exponential())
@breaker
def charge(card_token, cents):
    r = requests.post("https://bank.example/charge", json={"t": card_token, "c": cents}, timeout=3)
    r.raise_for_status()
    return r.json()


def fallback(card_token):
    return {"status": "queued"}
'''

NOTIFY_APP = '''import requests


def send_email(to, body):
    return requests.post("https://mail.example/send", json={"to": to, "body": body})
'''


def _git(repo: Path, *args: str, env=None) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Demo", "-c", "user.email=demo@example.com",
                    *args], check=True, capture_output=True, env=env)


def _commit(repo: Path, msg: str, days_ago: float) -> None:
    ts = int(time.time() - days_ago * 86400)
    env = {**os.environ, "GIT_AUTHOR_DATE": f"{ts} +0000", "GIT_COMMITTER_DATE": f"{ts} +0000"}
    _git(repo, "add", "-A", env=env)
    _git(repo, "commit", "-q", "-m", msg, env=env)


def _write(repo: Path, rel: str, content: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def build_demo_repo(dest) -> Path:
    repo = Path(dest)
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    java = "orders-service/src/main/java/com/demo/orders/InventoryClient.java"

    _write(repo, "orders-service/pom.xml", POM.format(name="orders-service"))
    _write(repo, java, INVENTORY_CLIENT)
    _write(repo, "payments-service/pyproject.toml", PYPROJECT.format(name="payments-service"))
    _write(repo, "payments-service/app.py", PAYMENTS_APP)
    _write(repo, "notifications-service/pyproject.toml", PYPROJECT.format(name="notifications-service"))
    _write(repo, "notifications-service/app.py", NOTIFY_APP)
    _commit(repo, "initial services", 150)

    for n, (msg, days) in enumerate([
        ("fix: inventory call hangs checkout", 60),
        ("hotfix: retry storm during inventory outage", 21),
        ("fix bug: reserve double-books stock", 9),
        ("revert: reserve change", 2),
    ], 1):
        _write(repo, java, f"// rev {n}\n" + INVENTORY_CLIENT)
        _commit(repo, msg, days)

    _write(repo, "payments-service/app.py", "# idempotency\n" + PAYMENTS_APP)
    _commit(repo, "fix: charge idempotency", 100)
    _write(repo, "notifications-service/app.py", "# sms\n" + NOTIFY_APP)
    _commit(repo, "add sms channel", 40)
    return repo
