# **SmartProxy: An Intelligent, Self-Adapting Proxy Pool Service**

SmartProxy is a sophisticated proxy management system designed to provide reliable, high-quality proxy services. It automates the entire lifecycle of proxies, from fetching and validation to intelligent scoring and dynamic pool management, ensuring that your applications always have access to the best available proxies.

## **Core Features**

* **Automated Proxy Fetching**: Gathers proxies from multiple user-defined sources.  
* **Intelligent Validation & Scoring**: Validation is a liveness gate (`is_active`); ranking is owned entirely by client feedback, which drives a 0-100 ELO-style score over a sliding window.
* **Feedback-Driven Adaptation**: Success and failure both move the score, with small samples shrunk toward the neutral baseline so a single observation cannot crown or exile a proxy.
* **Dynamic Configuration Reloading**: A hot-reload endpoint (/reload-sources) re-reads the whole config file - sources, fetcher jobs, and every tunable - authoritatively and transactionally, without restarting the service.
* **Sustainable Validation Logic**: Employs a time-window-based attempt limit for re-validating failed proxies. This prevents proxy burnout, reduces database load, and ensures long-term service stability.  
* **Source-Specific Pools**: Maintains separate proxy pools for different sources/use cases.  
* **RESTful API**: Simple endpoints for fetching proxies and submitting feedback.  
* **Monitoring Dashboard**: A web-based UI to monitor the health and statistics of different proxy sources in real-time.

## **How It Works**

1. **Fetch**: The service periodically fetches proxy lists from various sources defined in config.ini.  
2. **Validate**: A validation cycle runs regularly. It prioritizes new and previously successful proxies. To avoid overwhelming unreliable proxies, it supplements the validation queue with failed proxies that have not been tested more than a configured number of times within a specific time window (e.g., 5 times in 30 minutes).  
3. **Score**: Proxies are managed in memory for each source, with an ELO-inspired 0-100 score built from three additive components over a sliding window of recent results:
   * **Success rate (0-60)** - the weighted success rate, shrunk toward 0.5 by a Beta prior (`elo_prior_successes` / `elo_prior_failures`). This is what keeps one lucky success from outranking a proven proxy: with the defaults and successes at 15000ms, 1 success scores about 52 and 48-of-50 successes about 79, while an untried proxy sits at the live measured pool's median score (falling back to 50 when nothing is measured).
   * **Latency (0-30)** - a linear ramp between `latency_full_score_ms` and `latency_zero_score_ms`, then **multiplied by the smoothed success rate**. Latency is only ever measured on successful requests, so on its own it says nothing about how often a proxy fails; scaling it means a fast-but-unreliable proxy cannot hold a pool slot. A window with results but zero successes scores 0 here, not a neutral value.
   * **Consistency (0-10)** - a bonus for a stable success rate across the last 10 results.

   Results are weighted by age (`elo_decay_half_life_hours`) and dropped entirely past `elo_max_result_age_hours`. Scores are recomputed for the whole pool on every sync (`rescore_on_sync_enabled`), not only when feedback arrives - otherwise idle proxies freeze at their last score, time decay never fires, and a proxy knocked down by one failure can never earn its way back. Consecutive failures are kept only as diagnostic data; candidates are not hard-deleted by a failure threshold.
4. **Select**: When a client requests a proxy for a specific source via /get-proxy, the system serves only proxies that passed the most recent validation, filters them by the optional per-proxy cooldown, and selects from the current top pool using the configured strategy (`uniform`, `tiered`, `weighted`, or `softmax`). A configurable share of requests (`exploration_ratio`) instead goes to a proxy without unexpired feedback. Never-handed proxies are preferred; once every candidate has been tried, exploration rotates to the least-recently-handed-out candidate.
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
   *(For upgrading an existing database, see the Migration Guide in `CHANGELOG.md`)*

4.  **Configure the service:**  
   * Rename or copy `config/config.example.ini` to `config/config.ini`.  
   * Edit `config.ini` with your database credentials, desired port, and proxy sources.

5.  **Run Application**:
    ```bash
    uv run --locked run.py      # or: .venv/bin/python run.py
    ```
    *Or use the management script below.*

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
    chmod +x scripts/start.sh
    ```

2.  **Usage**:

    ```bash
    # Start service (background)
    ./scripts/start.sh start

    # Start with debug mode
    ./scripts/start.sh start --debug

    # Check status
    ./scripts/start.sh status

    # View logs
    ./scripts/start.sh logs

    # Restart service
    ./scripts/start.sh restart

    # Stop service
    ./scripts/start.sh stop
    
    # Manual backup of stats
    ./scripts/start.sh backup
    ```

## **Configuration (config.ini)**

The service is configured via the config.ini file.

* **\[database\]**: Credentials for your PostgreSQL database.  
* **\[server\]**: port for the API and dashboard.  
  * allowed\_ips: Comma-separated remote IP allowlist for external APIs and dashboard pages.
  * trust\_proxy\_headers / trusted\_proxy\_ips: Only trust X-Forwarded-For when the direct peer is explicitly trusted.
  * localhost (127.0.0.1 / ::1) is always allowed automatically.
  * internal endpoints `/health`, `/metrics`, `/reload-sources`, `/backup-stats` are localhost-only.
* **\[logging\]**:
  * log\_dir: Log directory. Relative paths are resolved from the project root. Defaults to `./.local/logs`.
* **\[validator\]**:  
  * validation\_target / validation\_targets: URL(s) used to test proxy connectivity and anonymity. Anonymity is detected by looking for `X-Forwarded-For` / `Via` / `X-Real-IP` in a `headers` object in the JSON response, so the target **must echo request headers**: `http://httpbin.org/get` does, `http://httpbin.org/ip` does not and leaves `anonymity_level` permanently `unknown`. Prefer configuring several `validation_targets` - a single target that rate-limits (httpbin does) marks good proxies dead.
  * validation\_success\_threshold: Number of targets a proxy must pass.  
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
* **\[scheduler\]**: Intervals for background tasks like fetching, validation, and flushing stats.  
* **\[sources\]**:  
  * predefined\_sources: A comma-separated list of logical names for your proxy pools (e.g., google\_search, web\_scraping).  
  * default\_source: The pool to use if a requested source doesn't exist.  
* **\[source\_pool\]**: Parameters for the scoring and selection algorithm.
  * selection\_strategy: `uniform`, `tiered`, `weighted`, or `softmax`. Note that `uniform` draws every proxy in the pool with equal probability, so the score only decides pool membership and the ranking is otherwise discarded; `softmax` is recommended.
  * softmax\_temperature: Controls temperature scaling for softmax selection (default `14.0`).
  * proxy\_cooldown\_ms: Minimum delay before the same proxy is handed out again for the same source.
  * exploration\_ratio: Share of requests spent on proxies without unexpired feedback (default `0.15`). Never-handed candidates are preferred; after all candidates have been tried, the least-recently-handed-out one is explored next. Set to `0` to disable.
  * elo\_prior\_successes / elo\_prior\_failures: Beta prior (default `0.25` / `0.75`) providing weak regularization aligned with baseline proxy success rates.
  * latency\_full\_score\_ms / latency\_zero\_score\_ms: The latency band the 30-point latency component is scaled over (default `15000` / `60000` ms). These bracket target response times via proxy. Re-check them if the proxy population changes.
  * elo\_circuit\_breaker\_multiplier: Penalty multiplier for sudden 10-consecutive failure outages (default `0.15`), dynamically capping score below untried baseline.
  * rescore\_on\_sync\_enabled: Recompute every score during pool sync so time decay applies to idle proxies.
  * ELO window/decay settings and latency thresholds (`elo_max_window`, `elo_scoring_window`, `elo_decay_half_life_hours`, `elo_max_result_age_hours`, `latency_full_score_ms`, `latency_zero_score_ms`, `max_feedback_latency_ms`).
  * elo\_max\_result\_age\_hours: How long one bad result costs a proxy its traffic. Past this age the result stops counting entirely and the proxy returns to the neutral baseline, so this is the real knob for failure recovery. Defaults to 48.
  * The untried baseline is **not** a constant: a proxy with no usable feedback scores the median score of the live proxies that do have some, recomputed each pool sync (falling back to 50.0 when nothing is measured yet). A fixed baseline inverts the whole ranking as soon as the population's real scores stop straddling it. See `docs/specs/proxy-quality-scoring.md` §2.
  * Feedback counters are also persisted to `proxies.feedback_success_count` / `feedback_failure_count` / `feedback_last_ts`, so a proxy evicted from the stats pool and later revalidated comes back with its record instead of a clean sheet.
  * max\_pool\_size x stats\_pool\_max\_multiplier: The cap on retained **dead** proxy history - not on total memory. Proxies that passed the latest validation are never evicted, because evicting one would reset its failure history to zero on the next sync, so the stats pool grows with the number of genuinely active proxies. If the live set alone reaches the cap, all dead history is dropped and a warning is logged.
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
    "http": "http://1.2.3.4:8080",  
    "https": "http://1.2.3.4:8080",
    "protocol": "http"
  }
```

* **Error Response (404)**: Returned if no proxies are currently available for the requested source.

**Note**: the `https` field is the same proxy URL, offered for convenience as the `https` entry of a `requests`-style proxies dict. Validation only exercises the configured `validation_target`(s); it never tests `CONNECT` tunnelling, so HTTPS support through a returned proxy is not verified. Report failures via `/feedback` so the score reflects it.

### **POST /feedback**

Submits feedback on a proxy's performance. This is crucial for the scoring system.

* **Request Body** (JSON):  
  * source (string, required): The source pool the proxy belongs to.  
  * proxy (string, required): The full proxy URL (e.g., http://1.2.3.4:8080).  
  * status (integer, required): 0 and 4 are legacy failures; 1/2/3 and HTTP 1xx-3xx are successes; HTTP 4xx-5xx are failures; other values are rejected.  
  * response\_time\_ms (integer, optional): The response time in milliseconds for successful requests. Lower times result in a higher score bonus. Must be finite, non-negative, and no larger than `max_feedback_latency_ms` (defaults to one day, `86400000`); anything else is rejected with a 400.
  * failure\_kind (string, optional): One of `timeout`, `proxy_error`, `dead`, `blocked`, `slow`, or `content_error`. `dead` applies the failure to every source where that proxy is tracked; other kinds affect only the reported source.
* **Success Response (200)**:  

```json
  { "message": "Feedback received." }
```

### **POST /reload-sources**

Triggers a hot-reload of config.ini. Every tunable is re-read - `[source_pool]`, `[validator]`, `[scheduler]`, `[backup]`, `[sources]` and the `[proxy_source_*]` sections - so you can add or remove proxy sources, update `predefined_sources`, and retune scoring or selection without restarting the service.

The reload is **authoritative**: the file is re-parsed into a fresh parser, so a key or a whole `[proxy_source_*]` section you delete from the file is genuinely dropped and reverts to its built-in default, rather than keeping its old in-memory value.

It is also **transactional**: the new configuration is applied as a unit. If any value fails to parse - including an `update_interval_minutes` in a `[proxy_source_*]` section, which is parsed separately from the tunables - the service rolls back to the configuration it was running and returns an error, instead of being left on a mix of old and new settings.

Three settings are **not** reloadable, because they are consumed once at startup: the `[database]` connection pool, `[server] port`, and `[logging]`. Changing any of them requires a restart; the response lists them under `restart_required_for`.

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
  "http": "http://1.2.3.4:8080",  
  "https": "http://1.2.3.4:8080",
  "premium": true
}
```

* **Error Response (404)**: Returned if no premium proxies are currently available.

**Note**: Premium proxies are selected from proxies with at least 50 uses (configurable via `premium_min_usage_count`) and sorted by score. This ensures only battle-tested, high-quality proxies are returned.

### **GET /health**

Health check endpoint for monitoring.

```bash
curl http://127.0.0.1:6942/health
```

* **Success Response (200)**:

```json
{
  "status": "healthy",
  "active_proxies": 1500,
  "premium_proxies": 50,
  "sources": 4,
  "is_validating": false
}
```

### **GET /metrics**

Prometheus-compatible metrics endpoint.

```bash
curl http://127.0.0.1:6942/metrics
```

* **Success Response (200)**: Returns metrics in Prometheus text format.
