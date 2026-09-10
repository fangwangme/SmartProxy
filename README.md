# **SmartProxy: An Intelligent, Self-Adapting Proxy Pool Service**

SmartProxy is a sophisticated proxy management system designed to provide reliable, high-quality proxy services. It automates the entire lifecycle of proxies, from fetching and validation to intelligent scoring and dynamic pool management, ensuring that your applications always have access to the best available proxies.

## **Core Features**

* **Automated Proxy Fetching**: Gathers proxies from multiple user-defined sources.  
* **Intelligent Validation & Scoring**: Validation is a liveness gate (`is_active`); ranking is owned entirely by client feedback, which drives a 0-100 online reliability score.
* **Feedback-Driven Adaptation**: Slow and fast estimators learn on every result from a fixed 5% prior. Learning decides *which* proxy is served, never *whether* one is served, so a cold pool and a ~1% success rate are both fully serviceable.
* **Dynamic Configuration Reloading**: A hot-reload endpoint (/reload-sources) re-reads the whole config file - sources, fetcher jobs, and every tunable - authoritatively and transactionally, without restarting the service.
* **Sustainable Validation Logic**: Employs a time-window-based attempt limit for re-validating failed proxies. This prevents proxy burnout, reduces database load, and ensures long-term service stability.  
* **Source-Specific Pools**: Maintains separate proxy pools for different sources/use cases.  
* **RESTful API**: Simple endpoints for fetching proxies and submitting feedback.  
* **Monitoring Dashboard**: A web-based UI to monitor the health and statistics of different proxy sources in real-time.

## **How It Works**

1. **Fetch**: The service periodically fetches proxy lists from various sources defined in config.ini.  
2. **Validate**: A validation cycle runs regularly. It prioritizes new and previously successful proxies. To avoid overwhelming unreliable proxies, it supplements the validation queue with failed proxies that have not been tested more than a configured number of times within a specific time window (e.g., 5 times in 30 minutes).  
3. **Score**: Each source keeps independent slow and fast reliability estimates. Both start at `reliability_prior` (5% by default), update on every feedback event, and expose `score = 100 × min(slow, fast)`. The slow estimator limits one-hit promotion; the fast estimator reacts quickly to deterioration. Old state decays toward the same fixed prior. Latency is observable but never enters reliability or selection order; it is recorded as `avg_latency_ms` and nothing more.
4. **Select**: Each source keeps a fixed-size candidate pool, rebuilt every `pool_refresh_seconds` and filled best-first from three tiers - proxies with successful feedback on record, then proxies that passed validation, then everything else the database holds including never-validated rows. `/get-proxy` is a uniform random pick from that pool. Score decides who makes the cut and in what order; it never decides whether a proxy may serve, and there is no threshold anywhere on the routing path. Because the pool is always filled to `min(candidate_pool_size, everything available)`, the service can only fail to answer when the database holds no proxies at all - a cold start, a lost reputation snapshot, and a new source all serve immediately. `exploration_slots` of the slots are reserved for never-measured proxies so a pool saturated by today's best still admits tomorrow's better one.
5. **Adapt**: Through continuous validation and feedback, low-quality proxies are phased out, and high-performing ones are prioritized, ensuring the overall quality of the pool constantly improves.

## **Project Structure**

- **`.local/`**: Contains local data, build outputs, and temporary files (git-ignored).
- **`.venv/`**: Local Python virtual environment.
- **`dashboard/`**: React-based frontend application.

## **Installation and Setup**

### **Prerequisites**

* Python 3.14, managed with [uv](https://docs.astral.sh/uv/) (a plain `venv` + `pip` also works — see below)
* PostgreSQL
* Node.js / Bun (for Dashboard)

### **Backend Setup (Python)**

1.  **Create the environment and install dependencies**:

    ```bash
    uv sync --locked
    ```

    This reads `pyproject.toml` and `uv.lock` and builds `.venv` on the
    interpreter pinned in `.python-version`, fetching that Python itself if the
    machine does not have it.

    Without uv, the pip path installs the same pinned set:

    ```bash
    python3.14 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    ```

    `requirements.txt` is generated from the lockfile (`uv export --frozen
    --no-hashes --no-emit-project -o requirements.txt`) and exists only for
    that fallback — declare dependencies in `pyproject.toml`, not there.

2.  **Run commands through the venv**:

    ```bash
    .venv/bin/python -m pytest tests/ -q
    ```

    Prefer `.venv/bin/...` over activating the shell, so the project interpreter
    is used regardless of the current shell state.

3.  **Set up the database:**  
   * Ensure your PostgreSQL server is running.  
   * Create a database and a user.  
   * Initialize the database schema:
     ```bash
     psql -U your_user -d your_db -f config/database_setup.sql
     ```

   `database_setup.sql` is the only schema definition, and it is authoritative
   rather than incremental: it drops and recreates every table, so an existing
   database is upgraded by running the same file again. There are no migration
   scripts to apply in order.

   Rerunning it discards all stored proxies and minute aggregates. That is the
   intended upgrade path: proxy rows are rediscovered by the fetchers within one
   source-refresh interval, and proxy reputation lives in the JSON backup rather
   than the database, so it survives the rebuild and is restored on startup.
   Back up `source_stats_by_minute` first if you need the historical dashboard
   series.

4.  **Configure the service:**  
   * Rename or copy `config/config.example.ini` to `config/config.ini`.  
   * Edit `config.ini` with your database credentials, desired port, and proxy sources.

5.  **Run Application**:
    ```bash
    uv run --locked run.py      # or: .venv/bin/python run.py
    ```
    *Or use the management script below.*

    Non-debug mode uses one Waitress process with a configurable thread pool.
    Do not add multiple WSGI worker processes while leases and scoring state
    remain process-local. Debug mode retains Flask's server.

### **Frontend Setup (Dashboard)**

1.  **Navigate to dashboard**:

    ```bash
    cd dashboard
    ```

2.  **Install Dependencies**:
    We use **Bun** for package management.

    ```bash
    bun install
    ```

3.  **Start Dev Server**:
    ```bash
    bun run dev
    ```

## **Service Management**

You can use the provided shell script to manage the backend service (start, stop, restart, etc.).

1.  **Make executable** (first time only):
    ```bash
    chmod +x scripts/start_proxy.sh
    ```

2.  **Usage**:

    ```bash
    # Start service (background)
    ./scripts/start_proxy.sh start

    # Start with debug mode
    ./scripts/start_proxy.sh start --debug

    # Skip JSON restore and write to an isolated backup
    ./scripts/start_proxy.sh start --no-restore

    # Check status
    ./scripts/start_proxy.sh status

    # View logs
    ./scripts/start_proxy.sh logs

    # Restart service (add --no-restore only on purpose)
    ./scripts/start_proxy.sh restart

    # Stop service
    ./scripts/start_proxy.sh stop
    
    # Manual backup of stats
    ./scripts/start_proxy.sh backup
    ```

## **Configuration (config.ini)**

The service is configured via the config.ini file.

* **\[database\]**: Credentials for your PostgreSQL database.  
* **\[server\]**: port for the API and dashboard.  
  * production\_threads / background\_workers: Thread counts for the single-process WSGI server and tracked background work.
  * connection\_limit: Maximum simultaneously open connections (default `1000`). Waitress defaults this to `100`; a client that retries without backoff exhausts that in seconds and turns any slow answer into a connection-level outage. The server loop runs on `poll()` rather than waitress's default `select()`, which cannot watch descriptors numbered 1024 or above and would crash the loop once connections plus the database pool reach that line. Restart-only.
  * shutdown\_deadline\_seconds: Deadline for stopping scheduling, draining or cancelling tracked work, flushing current feedback, and writing the final backup. Size it above normal drain/flush time plus the observed `smartproxy_backup_duration_seconds`; the launcher enforces the outer cutoff and backup replacement is atomic.
  * readiness\_*: Maximum dependency ages and minimum usable-pool threshold for `/ready`.
  * allowed\_ips: Comma-separated remote IP allowlist for external APIs and dashboard pages.
  * trust\_proxy\_headers / trusted\_proxy\_ips: Only trust X-Forwarded-For when the direct peer is explicitly trusted. The server passes forwarding headers through to the application, so this list is the one authority on them (waitress's own `trusted_proxy` accepts only a single address).
  * localhost (including IPv4-mapped loopback) is always allowed automatically.
  * internal endpoints `/health`, `/live`, `/ready`, `/metrics`, `/reload-sources`, `/backup-stats` are localhost-only, and refuse any request carrying forwarding headers (`X-Forwarded-For`, `Forwarded`, `X-Real-IP`, ...). A reverse proxy on the same host connects from loopback too; those headers are how it shows it is relaying someone else. Reach these endpoints directly on the service port, never through a proxy - a proxy configured to add no forwarding header at all cannot be told apart from a local caller.
* **\[logging\]**:
  * log\_dir: Log directory. Relative paths are resolved from the project root. Defaults to `./.local/logs`.
* **\[validator\]**:  
  * validation\_target / validation\_targets: URL(s) used to test proxy connectivity and anonymity. Every target must return JSON with a `headers` mapping. Production deployments should configure multiple independently operated targets; do not copy the same endpoint under several URLs merely to meet the count.
  * validation\_success\_threshold / validation\_target\_min\_samples: Proxy pass threshold and target-health evidence threshold. If target quorum is unavailable, existing active proxies keep their last-known-good liveness. Never-validated candidates are recorded as failed so an oldest bad batch cannot starve newer discoveries; the ordinary failed-proxy retry window reconsiders them after recovery.
  * validation\_workers: Number of concurrent threads for validation.  
  * validation\_batch\_limit: Maximum proxies pulled into one validation cycle.  
  * validation\_new\_proxy\_ratio: Share of that budget reserved for never-validated proxies; the rest re-checks proxies that are currently alive. Unused budget is donated to the other side. Defaults to `0.5`.
  * validation\_supplement\_threshold: If the number of new/active proxies to test is below this, the queue will be supplemented with failed proxies.  
  * validation\_window\_minutes: The time window (in minutes) for the validation attempt limit.  
  * max\_validations\_per\_window: The maximum number of times a failed proxy will be re-tested within the time window.  
* **\[fetcher\]**:
  * Proxy-list downloads always use curl; there is no transport switch or fallback. The validator is unaffected — it dials proxy IPs through aiohttp and inspects response headers.
  * curl\_retries / curl\_retry\_delay\_s: `--retry` arguments for curl. The subprocess timeout is sized to cover every attempt, since `--max-time` bounds one attempt each.
  * backoff\_base\_s / backoff\_max\_s / backoff\_transient\_max\_s: Backoff after a failed fetch doubles per consecutive failure from `backoff_base_s`, capped by what kind of failure it was. A reset connection or a timeout is transient and stops at `backoff_transient_max_s` (300s); an HTTP 404 or a malformed URL is the source itself saying no and waits `backoff_max_s` (1800s). Without the split, an intermittently reset connection compounds into a near-total supply outage — a 35% fetch failure rate put every source into a 16-32 minute backoff.
  * Fetch failure count, class, and next-attempt time are persisted, so a restart does not immediately retry a source whose circuit is still open.
* **\[scheduler\]**: Intervals for background tasks like fetching, validation, and flushing stats.  
* **\[sources\]**:  
  * predefined\_sources: A comma-separated list of logical names for your proxy pools (e.g., google\_search, web\_scraping).  
  * default\_source: The pool to use if a requested source doesn't exist.  
* **\[source\_pool\]**: Parameters for the scoring and selection algorithm.
  * candidate\_pool\_size / pool\_refresh\_seconds / exploration\_slots: The whole routing layer (defaults `200`, `60`, `20`). How many slots the pool holds, how often it is refilled, and how many slots are reserved for never-measured proxies. **None of the three can empty the pool at any accepted value** - that is the design rule, not a coincidence: a parameter whose wrong value degrades performance is a tunable, a parameter whose wrong value empties the servable set is a defect. `exploration_slots = 0` disables the reservation; a value above `candidate_pool_size` still fills the pool.
  * proxy\_inflight\_timeout\_seconds: The outstanding-handout lease. It is bookkeeping only - nothing on the selection path reads it - and exists so feedback can be told apart from a duplicate or a late report. It releases a slot the client never reported. Restart-only so the lease list stays sorted by expiry.
  * selection\_strategy / softmax\_temperature / selection\_weight\_floor / top\_tier\_load\_percentage: **Dormant.** The draw inside the pool is uniform, so these are parsed and validated but not consulted. They remain so that re-introducing weights on top of the pool is a code change rather than a config migration.
  * reliability\_prior / reliability\_slow\_alpha / reliability\_fast\_alpha: Fixed prior and two online update speeds (defaults `0.05`, `0.12`, `0.30`).
  * reliability\_decay\_half\_life\_hours: Wall-clock forgiveness toward the fixed prior.
  * outage\_guard\_*: Requires a healthy completed window before a broad distinct-proxy failure spike can pause and roll back proxy-level reputation updates. Aggregate traffic metrics continue, and a completed recovery window resumes learning.
  * max\_feedback\_latency\_ms: Input-safety boundary for diagnostic latency. Latency never affects reliability or ordering.
  * Online reputation stays source-local in the manager and is included in the existing JSON backup. If no backup is restored, proxies start from the fixed prior and relearn through normal traffic; there is no reputation database migration or double-write path.
  * max\_pool\_size / top\_tier\_size: Bound the ranked tier lists, which are reporting and the input a later weighting change would use. They are not an eligibility gate and are not what `/get-proxy` reads.
  * max\_pool\_size x stats\_pool\_max\_multiplier: The cap on retained **dead** proxy history - not on total memory. Proxies the pool can serve - those that passed the latest validation and those in the reserve page - are never evicted, because the pool would re-seed a blank record for one on the next sync and launder its failure history, so the stats pool grows with the servable set. If the servable set alone reaches the cap, all other history is dropped and a warning is logged.
* **\[proxy\_source\_\*\]**: Define your proxy sources here. Each source should have its own section (e.g., \[proxy\_source\_freeproxies\]).  
  * url: The URL to fetch the proxy list from.  
  * update\_interval\_minutes: How often to fetch from this source.  
  * default\_protocol: The protocol (http, https etc.) if not specified in the source file.

Proxy list lines are parsed into `(protocol, ip, port)` and `ip` must be an IP literal, not a hostname. IPv6 literals are stored bracketed (`[2001:db8::1]`) whether or not the source list brackets them, so the `protocol://ip:port` URL built from them everywhere downstream still has a parseable port.

## **API Documentation**

### **GET /get-proxy**

Fetches an available proxy for a specific use case.

* **Query Parameters**:  
  * source (required): The name of the source pool to get a proxy from (must match one in predefined\_sources).  
* **Success Response (200)**:  
  
```json
  {  
    "http": "http://192.0.2.10:8080",
    "https": "http://192.0.2.10:8080",
    "protocol": "http",
    "source": "example"
  }
```

* **Error Response (404)**: Returned only when the `proxies` table is empty. Since issue #27 the handout path holds no threshold, so a pool that is cold, unvalidated, or scoring badly still answers `200` - reputation decides *which* proxy comes back, never *whether* one does. A `404` here means the fetchers have supplied nothing at all, not that quality is low.

**Note**: the `https` field is the same proxy URL, offered for convenience as the `https` entry of a `requests`-style proxies dict. Validation only exercises the configured `validation_target`(s); it never tests `CONNECT` tunnelling, so HTTPS support through a returned proxy is not verified. Return the `source` field verbatim in `/feedback` so the result is scored against the pool the proxy was drawn from - for a premium proxy this is the only way to know which pool that is.

### **POST /feedback**

Submits feedback on a proxy's performance. This is crucial for the scoring system.

* **Request Body** (JSON):  
  * source (string, required): The source pool the proxy belongs to.  
  * proxy (string, required): The exact proxy URL that was handed out.
  * status (integer, required): 0 and 4 are legacy failures; 1/2/3 and HTTP 1xx-3xx are successes; HTTP 4xx-5xx are failures; other values are rejected.  
  * response\_time\_ms (integer, optional): Diagnostic response time in milliseconds. It does not affect reliability or selection; it is recorded as `avg_latency_ms` for observability only. Must be finite, non-negative, and no larger than `max_feedback_latency_ms` (defaults to one day, `86400000`); anything else is rejected with a 400.
  * failure\_kind (string, optional): One of `timeout`, `proxy_error`, `dead`, `blocked`, `slow`, or `content_error`. `dead` applies the failure to every source where that proxy is tracked; other kinds affect only the reported source.

* **Request Example**:

```json
{
  "source": "example",
  "proxy": "http://192.0.2.10:8080",
  "status": 200,
  "response_time_ms": 120
}
```

* **Success Response (200)**: `{"message": "Feedback received."}`

Everything that passes boundary validation is scored. The service is a neutral
referee: it records what the client reports, and the client owns whether that
report is correct. It does not authenticate a report against the handout it
belongs to, because the errors that would catch - a duplicate delivery, a very
late report, a wrong `source` - are the client's to prevent, and the errors that
actually damage a score - calling a soft-block page a success - cannot be caught
here at all. What the service does do is *count*: feedback that arrives with no
outstanding handout to close is exported as
`smartproxy_feedback_unmatched_total`, which is the number to watch if you
suspect your client duplicates or delays its reports. Read it with two caveats:
leases do not survive a restart, so every restart adds roughly one count per
request that was in flight at the time, and a report that arrives later than
`proxy_inflight_timeout_seconds` counts too, because its lease has already been
reclaimed.

### **POST /reload-sources**

Triggers a hot-reload of config.ini. Every tunable is re-read - `[source_pool]`, `[validator]`, `[scheduler]`, `[backup]`, `[sources]` and the `[proxy_source_*]` sections - so you can add or remove proxy sources, update `predefined_sources`, and retune scoring or selection without restarting the service.

The reload is **authoritative**: the file is re-parsed into a fresh parser, so a key or a whole `[proxy_source_*]` section you delete from the file is genuinely dropped and reverts to its built-in default, rather than keeping its old in-memory value.

It is also **transactional**: the new configuration is applied as a unit. If
any value fails parsing or semantic validation—including an
`update_interval_minutes` in a `[proxy_source_*]` section—the service rolls back
to its previous configuration instead of being left on a mix of old and new
settings.

Connection-pool settings, `[server] port`, `production_threads`,
`background_workers`, and `[logging]` are consumed once at startup and require a
restart. Every reload is semantically validated before active values change;
invalid counts, timeouts, intervals, percentages, pool bounds, or cross-field
relationships reject the entire reload.

```bash
curl -X POST -H "Content-Type: application/json" http://127.0.0.1:6942/reload-sources
```

* **Request Body**: Empty  
* **Success Response (200)**:  

```json
  {  
    "status": "success",  
    "message": "Configuration and sources reloaded.",  
    "details": {  
      "added_fetcher_jobs": ["proxy_source_new"],  
      "removed_fetcher_jobs": [],  
      "added_predefined_sources": ["new_pool"],  
      "removed_predefined_sources": [],
      "restart_required_for": [
        "[database] connection pool",
        "[server] port",
        "[server] production_threads / background_workers",
        "[logging] log_dir / log_file_base_name"
      ]
    }  
  }  
```

### **POST /backup-stats**

Manually triggers a backup of the in-memory proxy statistics to a JSON file.

```bash
curl -X POST http://127.0.0.1:6942/backup-stats
```

* **Request Body**: Empty  
* **Success Response (200)**:  

```json
{
  "status": "success",
  "path": "./data/proxy_stats_backup.json",
  "sources": 4,
  "total_proxies": 1500
}
```

### **GET /get-premium-proxy**

Fetches a premium (highest quality) proxy for Playwright and other high-reliability use cases. Returns one of the top-scoring proxies across all sources.

```bash
curl http://127.0.0.1:6942/get-premium-proxy
```

* **Query Parameters**: None required  
* **Success Response (200)**:  

```json
{
  "http": "http://192.0.2.10:8080",
  "https": "http://192.0.2.10:8080",
  "premium": true,
  "source": "example"
}
```

* **Error Response (404)**: Returned if no premium proxies are currently available.

**Note**: Premium proxies are selected from proxies with at least 50 uses (configurable via `premium_min_usage_count`) and sorted by score. This ensures only battle-tested, high-quality proxies are returned.

### **GET /live, /ready, and /health**

`/live` reports only that the process can serve HTTP:

```json
{"status": "live", "serving": true}
```

`/ready` checks the database, scheduler, age of the last healthy validation
quorum, age of the last successful feedback flush, and minimum usable pool. It
returns `200` when ready or `503` otherwise:

```json
{
  "status": "ready",
  "ready": true,
  "dependencies": {
    "database": true,
    "scheduler": true,
    "validation": true,
    "feedback_flush": true,
    "usable_pool": true
  },
  "usable_proxies": 10,
  "minimum_usable_proxies": 1
}
```

`/health` is the compatibility endpoint. It includes the existing pool counts
plus the same readiness state, returning `200` with `status: healthy` or `503`
with `status: degraded`; a dependency failure is never reported as healthy.

```bash
curl http://127.0.0.1:6942/health
```

* **Success Response (200)**:

```json
{
  "status": "healthy",
  "ready": true,
  "dependencies": {
    "database": true,
    "scheduler": true,
    "validation": true,
    "feedback_flush": true,
    "usable_pool": true
  },
  "active_proxies": 1500,
  "premium_proxies": 50,
  "sources": 4,
  "is_validating": false
}
```

Statistics endpoints validate parameters before querying PostgreSQL. A backend
failure returns `503` with `{"status": "error", "error": "Statistics backend
unavailable."}` rather than an all-zero success payload. API responses include
`X-Server-Time` as a full timezone-aware timestamp; the dashboard advances that
clock locally and uses its calendar date for “today” and moving windows.

### **GET /metrics**

Prometheus-compatible metrics endpoint.

```bash
curl http://127.0.0.1:6942/metrics
```

* **Success Response (200)**: Returns metrics in Prometheus text format.
