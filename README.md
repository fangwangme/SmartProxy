# SmartProxy

SmartProxy runs a pool of free proxies for scrapers. It fetches proxy lists,
checks which proxies are alive, hands them out per use case ("source"), and
learns from the scrapers' feedback which proxies actually work for that source.

- **Always answers.** `/get-proxy` returns a proxy whenever the database holds
  any, including on a cold start with nothing validated or scored yet.
- **Learns per source.** The same proxy can be good for one target site and
  blocked by another. Each source keeps its own scores.
- **Concentrates on what works.** Traffic is drawn by score, with a fixed share
  kept for trying newcomers and proxies that may have recovered.

## How it works

1. **Fetch.** Each `[proxy_source_*]` list is downloaded on its own interval
   and stored in PostgreSQL.
2. **Validate.** A background cycle checks proxies against
   `validation_target`, splitting its budget between never-checked proxies and
   re-checks of live ones. The result is `is_active`. It decides only what is
   tried first, never what may be served.
3. **Score.** Every `/feedback` report updates the proxy's score for that
   source: `score = 100 × min(slow, fast)`, two moving averages that start at a
   5% prior. The fast one drops quickly on failures and the slow one stops a
   single success from looking like a track record. Scores drift back toward
   the prior over time (24 h half-life).
4. **Select.** Each source keeps a candidate pool of 300 proxies, rebuilt every
   60 s: 270 ranked by current score and 30 exploration slots that rotate over
   everything else, longest-untried first. A handout comes from the exploration
   slots 10% of the time, chosen at random, and otherwise from the ranked slots
   weighted by score (softmax).

Scores live in memory and are saved to a JSON backup every hour and on
shutdown. On startup the backup is restored if it is at most 12 hours old;
otherwise every proxy starts again from the prior.

The design and the reasons behind it are in
[docs/specs/proxy-quality-scoring.md](docs/specs/proxy-quality-scoring.md).

## Quick start

Requirements: Python 3.14 with [uv](https://docs.astral.sh/uv/), PostgreSQL,
and Bun for the dashboard.

```bash
uv sync --locked                                   # creates .venv
cp config/config.example.ini config/config.ini     # then set [database], port, allowed_ips
psql -U <user> -d <db> -f config/database_setup.sql
(cd dashboard && bun install && bun run build)     # dashboard served at /
./scripts/start_proxy.sh start
```

The service listens on port 6942 by default. The dashboard is at
`http://localhost:6942/` once it has been built.

Without uv: `python3.14 -m venv .venv && .venv/bin/pip install -r
requirements.txt`. `requirements.txt` is generated from `uv.lock`; declare
dependencies in `pyproject.toml`.

**Database schema.** `config/database_setup.sql` is the only schema
definition. It drops and recreates every table, so an upgrade means running it
again. That discards stored proxies and the per-minute statistics: proxies are
refetched within one source interval, and scores survive because they live in
the JSON backup, not the database.

## Client integration

One handout, one report:

1. `GET /get-proxy?source=<name>` returns a proxy.
2. Use it for one task.
3. `POST /feedback` with the outcome.

```python
import time, requests

BASE = "http://127.0.0.1:6942"
SOURCE = "insolvencydirect"

handout = requests.get(f"{BASE}/get-proxy", params={"source": SOURCE}, timeout=5).json()
proxies = {"http": handout["http"], "https": handout["https"]}

started = time.monotonic()
try:
    page = requests.get(url, proxies=proxies, timeout=20)
    page.raise_for_status()
    status = 100 if parse(page.text) else 7     # fetched / fetched but no data
except Exception:
    status = 4                                  # the request failed

requests.post(f"{BASE}/feedback", timeout=5, json={
    "source": handout["source"],
    "proxy": handout["http"],
    "status": status,
    "response_time_ms": int((time.monotonic() - started) * 1000),
})
```

### GET /get-proxy

| Parameter | |
|---|---|
| `source` (required) | One of `[sources] predefined_sources`. An unknown name is served from `default_source`. |

```json
{"http": "http://192.0.2.10:8080", "https": "http://192.0.2.10:8080", "protocol": "http", "source": "insolvencydirect"}
```

- `https` is the same proxy URL, provided so the response can be used directly
  as a `requests` proxies dict. Validation does not test HTTPS tunnelling
  (`CONNECT`).
- Send `source` back unchanged in `/feedback`.
- `404` means the `proxies` table is empty. It never means quality is low.

### POST /feedback

| Field | |
|---|---|
| `source` (string, required) | The `source` from the handout. |
| `proxy` (string, required) | The proxy URL as handed out. |
| `status` (integer, required) | The outcome. See the table below. |
| `response_time_ms` (number, optional) | Recorded for diagnostics only; it never affects scoring. Must be finite, ≥ 0 and ≤ `max_feedback_latency_ms` (default one day). |
| `failure_kind` (string, optional) | `timeout`, `proxy_error`, `dead`, `blocked`, `slow` or `content_error`. `dead` applies the failure to every source that tracks the proxy. |

**Status codes**

| `status` | Meaning | Scored as |
|---|---|---|
| `100` | Page fetched and parsed. | success |
| `7` | Page fetched, but the expected data was missing. The proxy did its job. | success |
| `4` | The request failed: connection, timeout, proxy, TLS or non-2xx error. | failure |
| anything else | Not part of the protocol (for example `0`, `1`/`2`/`3`, `10`, `200`). | **not scored** |

A status outside the protocol is still accepted. The response is
`{"message": "Feedback received; status not scored.", "scored": false, ...}`.
The handout is closed, the proxy's score and the statistics are left
unchanged, and the report is counted in
`smartproxy_feedback_accepted_total{outcome="unscored"}`. The first
occurrence of each status per source is logged as a warning.

Responses: `200` `{"message": "Feedback received."}`. `400` for a malformed
body: a missing or wrong-typed field, or an invalid `response_time_ms`.

The service records what the client reports and does not check it. Two
consequences:

- Reporting a block or captcha page as `100` teaches the service that the proxy
  works.
- Feedback that arrives with no outstanding handout (a duplicate, a report
  later than `proxy_inflight_timeout_seconds`, or a wrong `source`) is still
  scored, and is counted in `smartproxy_feedback_unmatched_total`. Each restart
  also adds about one count per request that was in flight at the time.

### GET /get-premium-proxy

Returns one of the best proxies across all sources, for callers that would
rather get nothing than an unproven proxy. A proxy qualifies when it passed
its last validation, has at least `premium_min_usage_count` (50) reports, and
scores above the prior. `404` when none qualifies. This endpoint is opt-in
and, unlike `/get-proxy`, filters by design.

```json
{"http": "http://192.0.2.10:8080", "https": "http://192.0.2.10:8080", "protocol": "http", "premium": true, "source": "insolvencydirect"}
```

## Operations

### Service script

```bash
./scripts/start_proxy.sh start      # background; logs to .local/logs/
./scripts/start_proxy.sh stop       # backs up scores, then stops
./scripts/start_proxy.sh restart
./scripts/start_proxy.sh status
./scripts/start_proxy.sh logs
./scripts/start_proxy.sh backup     # write the score backup now
```

`start --debug` runs Flask's development server. `start --no-restore` skips
the score restore and writes to a separate backup file, for experiments only.
The service runs as a single Waitress process: leases and scores are
in-process state, so do not run several workers.

### Internal endpoints

These are localhost-only. They also refuse any request that carries a
forwarding header (`X-Forwarded-For`, `Forwarded`, `X-Real-IP`), so call them
directly on the service port, never through a reverse proxy.

| Endpoint | |
|---|---|
| `GET /live` | The process is serving HTTP. |
| `GET /ready` | `200` when the database, scheduler, latest validation batch, feedback flush and usable pool are all healthy, otherwise `503`. The usable pool counts everything `/get-proxy` can hand out, so a cold start with nothing validated is still usable. |
| `GET /health` | `/ready` plus pool counts; `200` healthy or `503` degraded. |
| `GET /metrics` | Prometheus text. See below. |
| `POST /reload-sources` | Re-reads `config.ini` and applies it as a whole or not at all. Settings marked restart-only below are reported in `restart_required_for` instead of applied. |
| `POST /backup-stats` | Writes the score backup now. |

Metrics: `smartproxy_feedback_accepted_total{outcome=success|failure|unscored}`,
`smartproxy_feedback_unmatched_total`, `smartproxy_success_rate_percent`,
`smartproxy_active_proxies`, `smartproxy_premium_proxies`,
`smartproxy_sources_total`, `smartproxy_is_validating`,
`smartproxy_source_outage_guard_active` and `..._paused_updates_total`,
`smartproxy_validation_target_failures_total`,
`smartproxy_backup_duration_seconds`, `smartproxy_manager_lock_hold_seconds`,
and `smartproxy_plan_refresh_duration_seconds` (the candidate-pool rebuild).

### Dashboard

`/` serves the dashboard: success rate (solid line) and request volume (dashed
line) per source, for one day at a time. It reads `/api/sources` and
`/api/stats/*`, which answer `503` when PostgreSQL is unavailable rather than
reporting zeros. The build lives in `.local/dist`, which is git-ignored, so
every checkout needs its own `bun run build`. Development: see
[dashboard/README.md](dashboard/README.md).

## Configuration

Everything is in `config/config.ini`. `config/config.example.ini` is the
reference: every key, its default, and a comment. The keys that matter most:

**`[server]`**
- `port` (6942), `production_threads`, `background_workers`: restart-only.
- `connection_limit` (1000): maximum open connections; restart-only.
- `allowed_ips`: remote addresses allowed to use the API and dashboard.
  Localhost is always allowed.
- `trust_proxy_headers` and `trusted_proxy_ips`: honour `X-Forwarded-For`
  only from these peers.
- `shutdown_deadline_seconds`: the time allowed to drain, flush and back up on
  stop.
- `readiness_*`: thresholds for `/ready`. `readiness_validation_max_age_seconds`
  (2400) must exceed `validation_window_minutes` plus
  `validation_interval_seconds`.

**`[database]`**: connection settings and pool size; restart-only.

**`[logging]`**: `log_dir` (`./.local/logs`) and the log file name; restart-only.

**`[sources]`**
- `predefined_sources`: the source names clients may request.
- `default_source`: the source that serves unknown names.

**`[source_pool]`: routing and scoring**
- `candidate_pool_size` (300), `pool_refresh_seconds` (60),
  `exploration_slots` (30): pool size, rebuild interval, and the rotating
  slots. Exploration gets `exploration_slots / candidate_pool_size` of
  handouts. No value of these can stop the pool from serving.
- `selection_strategy` (`softmax`), `softmax_temperature` (14),
  `selection_weight_floor`, `top_tier_load_percentage`: how the ranked slots
  are weighted. Options are `uniform`, `tiered`, `weighted` and `softmax`. At
  14, a proxy scoring 14 points higher is drawn e times as often.
- `reliability_prior` (0.05), `reliability_slow_alpha` (0.12),
  `reliability_fast_alpha` (0.30), `reliability_decay_half_life_hours` (24):
  the scoring model.
- `proxy_inflight_timeout_seconds` (120): how long a handout waits for its
  report before it is closed.
- `premium_pool_size` and `premium_min_usage_count`: the premium endpoint.
- `outage_guard_*`: pauses score updates for a source when nearly every proxy
  fails at once, which points to the target site or the client rather than the
  proxies.
- `max_pool_size`, `top_tier_size`, `stats_pool_max_multiplier`: bound the
  reported tier lists and the retained history of dead proxies.

**`[validator]`**
- `validation_target` and `validation_targets`: URLs that must return JSON with
  a `headers` mapping.
- `validation_workers`, `validation_batch_limit`, `validation_new_proxy_ratio`:
  concurrency, cycle size, and the share of each cycle for never-checked
  proxies.
- `validation_window_minutes` and `max_validations_per_window`: how often a
  failed proxy may be re-checked.

**`[scheduler]`**: intervals for validation, stats flush and source refresh.

**`[fetcher]`**: curl timeouts, retries and backoff. A transient failure backs
off up to 300 s; a permanent one (for example a 404) backs off up to 1800 s.

**`[backup]`**: `stats_backup_interval_seconds` (3600), `stats_backup_path`,
and `stats_restore_max_age_hours` (12). The age is read from the timestamp
inside the file, never from its mtime.

**`[proxy_source_<name>]`**: `url`, `update_interval_minutes`, and
`default_protocol` for lines that are a bare `ip:port`. Hostnames are not
accepted; IPv6 addresses are stored bracketed.

## Development

```bash
.venv/bin/python -m pytest tests/ -q
```

Branching, worktrees, the changelog policy and test conventions are in
[AGENTS.md](AGENTS.md).
