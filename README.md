# Hindsight

Find the microservices most likely to hurt you, then prove it.

Hindsight combines three signals that are usually separate:

1. **Git history**: which services keep getting fixed, reverted and hot-patched.
2. **Static scan**: which outbound HTTP calls lack timeouts, retries, circuit breakers or fallbacks.
3. **Chaos drills**: a fault-injecting proxy runs six failure scenarios against the live service and scores how it actually behaves.

Zero runtime dependencies. Python 3.10+. Java/Kotlin and Python services are scanned.

## Install

```bash
pipx install git+https://github.com/anishtavva2002/hindsight
# or from a clone: pipx install .   (pip install . also works)
hindsight --version
```

Without installing: `python -m hindsight ...` from the repo root.

## 60-second tour

```bash
hindsight demo /tmp/demo              # synthetic multi-service git repo
hindsight scan /tmp/demo              # ranked risk report
hindsight why /tmp/demo orders-service/src/main/java/com/demo/orders/InventoryClient.java
hindsight serve /tmp/demo             # web UI at http://127.0.0.1:8765
hindsight drill-demo                  # naive vs resilient service, side by side (~45s)
```

## Commands

| Command | What it does |
|---|---|
| `scan <repo>` | Discovers services, mines history, scans for resilience gaps, ranks by priority. `--json`, `--md FILE`, `--fail-on-high` (CI gate). |
| `why <repo> <path>` | Timeline and story of a file or directory: origin, fixes, biggest change. |
| `serve <repo>` | Local web UI: ranked services, timelines, drill results, a Run drill button. Binds to localhost. |
| `chaos --upstream host:port ...` | Standalone fault proxy: `--latency`, `--jitter`, `--error-rate`, `--reset-rate`, `--corrupt-rate`, `--blackhole-rate`. |
| `drill --target URL --upstream host:port` | Runs the scenario suite against one service and scores it. `--min-score N` exits 1 below N. |
| `drill-all <repo>` | Drills every service in `hindsight.json`, riskiest first. `--top`, `--only`, `--min-score`. |
| `demo <dir>` / `drill-demo` | Sample repo and sample services for trying things out. |

## How a drill works

The service under test must reach its dependency **through the proxy**. Point the service's dependency URL at `127.0.0.1:<listen>` (default 9001) and give Hindsight the real dependency as `--upstream`.

Scenarios (after a baseline that must be healthy):

| Scenario | Passes when |
|---|---|
| slow dependency | requests return within the probe timeout (dependency call has its own timeout) |
| flaky 30% | at least 80% of requests still succeed (retries mask blips) |
| outage (reset), outage (503) | no hangs; ideally a degraded 2xx; no retry storm (more than 2.5 dependency calls per request is penalised) |
| corrupt bodies | garbled payloads are handled, not turned into 5xx |
| black hole | same as slow: bounded, not hung |

Score is the mean of the six scenarios (0-100), graded A to F. Drills abort if the baseline is unhealthy or the target never touched the proxy, so a misrouted setup can't produce a fake A.

### Configure services: `hindsight.json`

```json
{
  "services": {
    "orders-service": {
      "target": "http://localhost:8080/checkout",
      "upstream": "localhost:9000",
      "listen": 9001,
      "requests": 20,
      "concurrency": 4,
      "timeout": 4,
      "settle": 3
    }
  }
}
```

Keys are service paths as shown by `hindsight scan`. Results are stored in `.hindsight/drills.json` (add `.hindsight/` to your `.gitignore`). Once a service has a measured score, it replaces the static gap estimate in the priority ranking.

## Use in CI

```yaml
- run: pipx install git+https://github.com/anishtavva2002/hindsight
- run: hindsight scan . --fail-on-high
- run: hindsight drill-all . --min-score 70   # needs your services running
```

## Limits

- The scanner is regex-based: it favours finding gaps over being exact, and covers Java/Kotlin and Python only.
- The proxy speaks HTTP/1.x and picks one fault per connection.
- The drill judges behaviour from HTTP responses only; it doesn't read your code.
- 20 probes is noisy for the flaky scenario. Raise `requests` for stable CI numbers.
- Only one drill runs at a time (fixed proxy ports).
- Drill results aren't marked stale after code changes. Re-run them.

## Security notes

- `serve` binds to 127.0.0.1 by default. Don't expose it: it can start drills against the targets in your config.
- Drill-triggering requests need an `X-Hindsight` header, which blocks drive-by requests from other web pages.
- Commit messages and file names shown in the UI are escaped.

## Development

```bash
python -m unittest discover -s tests -v
```

## License

MIT. See [LICENSE](LICENSE).
