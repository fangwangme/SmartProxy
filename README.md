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
   * **Success rate (0-60)** - the weighted success rate, shrunk toward 0.5 by a Beta prior (`elo_prior_successes` / `elo_prior_failures`). This is what keeps one lucky success from outranking a proven proxy: with the defaults, 1 success scores about 59 and 48-of-50 successes about 90, while an untried proxy sits at the neutral 50.
   * **Latency (0-30)** - a linear ramp between `latency_full_score_ms` and `latency_zero_score_ms`, then **multiplied by the smoothed success rate**. Latency is only ever measured on successful requests, so on its own it says nothing about how often a proxy fails; scaling it means a fast-but-unreliable proxy cannot hold a pool slot. A window with results but zero successes scores 0 here, not a neutral value.
   * **Consistency (0-10)** - a bonus for a stable success rate across the last 10 results.

   Results are weighted by age (`elo_decay_half_life_hours`) and dropped entirely past `elo_max_result_age_hours`. Scores are recomputed for the whole pool on every sync (`rescore_on_sync_enabled`), not only when feedback arrives - otherwise idle proxies freeze at their last score, time decay never fires, and a proxy knocked down by one failure can never earn its way back. Consecutive failures are kept only as diagnostic data; candidates are not hard-deleted by a failure threshold.
4. **Select**: When a client requests a proxy for a specific source via /get-proxy, the system serves only proxies that passed the most recent validation, filters them by the optional per-proxy cooldown, and selects from the current top pool using the configured strategy (`uniform`, `tiered`, `weighted`, or `softmax`). A configurable share of requests (`exploration_ratio`) instead goes to a proxy that has never been handed out, so newly discovered candidates can earn a ranking rather than being stuck behind the incumbent pool forever.
5. **Adapt**: Through continuous validation and feedback, low-quality proxies are phased out, and high-performing ones are prioritized, ensuring the overall quality of the pool constantly improves.

## **Project Structure**

- **`.local/`**: Contains local data, build outputs, and temporary files (git-ignored).
- **`.venv/`**: Local Python virtual environment.
- **`dashboard/`**: React-based frontend application.

## **Installation and Setup**

### **Prerequisites**

* Python 3.8+  
* PostgreSQL
* Node.js / Bun (for Dashboard)

### **Backend Setup (Python)**

1.  **Activate Virtual Environment**:

    ```bash
    source .venv/bin/activate
    ```

2.  **Install Dependencies** (if needed):

    ```bash
    pip install -r requirements.txt
    ```

3.  **Set up the database:**  
   * Ensure your PostgreSQL server is running.  
   * Create a database and a user.  
   * Execute the `config/database_setup.sql` script to create the necessary tables and indexes.  
     ```bash
     psql -U your_user -d your_db -f config/database_setup.sql
     ```

4.  **Configure the service:**  
   * Rename or copy `config/config.example.ini` to `config/config.ini`.  
   * Edit `config.ini` with your database credentials, desired port, and proxy sources.

5.  **Run Application**:
    ```bash
    python run.py
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
  * use\_curl: Defaults to `false`. Enable only for local environments that intentionally route `curl` differently from Python.
* **\[scheduler\]**: Intervals for background tasks like fetching, validation, and flushing stats.  
* **\[sources\]**:  
  * predefined\_sources: A comma-separated list of logical names for your proxy pools (e.g., google\_search, web\_scraping).  
  * default\_source: The pool to use if a requested source doesn't exist.  
* **\[source\_pool\]**: Parameters for the scoring and selection algorithm.  
  * selection\_strategy: `uniform`, `tiered`, `weighted`, or `softmax`. Note that `uniform` draws every proxy in the pool with equal probability, so the score only decides pool membership and the ranking is otherwise discarded; `weighted` is recommended.  
  * proxy\_cooldown\_ms: Minimum delay before the same proxy is handed out again for the same source.  
  * exploration\_ratio: Share of requests spent on proxies that have never been handed out. Set to `0` to disable.  
  * elo\_prior\_successes / elo\_prior\_failures: Beta prior that shrinks small samples toward the neutral score.  
  * rescore\_on\_sync\_enabled: Recompute every score during pool sync so time decay applies to idle proxies.  
  * ELO window/decay settings and latency scoring thresholds (`elo_max_window`, `elo_scoring_window`, `elo_decay_half_life_hours`, `elo_max_result_age_hours`, `latency_full_score_ms`, `latency_zero_score_ms`).  
* **\[proxy\_source\_\*\]**: Define your proxy sources here. Each source should have its own section (e.g., \[proxy\_source\_freeproxies\]).  
  * url: The URL to fetch the proxy list from.  
  * update\_interval\_minutes: How often to fetch from this source.  
  * default\_protocol: The protocol (http, https etc.) if not specified in the source file.

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
  * response\_time\_ms (integer, optional): The response time in milliseconds for successful requests. Lower times result in a higher score bonus.  
  * failure\_kind (string, optional): One of `timeout`, `proxy_error`, `dead`, `blocked`, `slow`, or `content_error`. `dead` applies the failure to every source where that proxy is tracked; other kinds affect only the reported source.
* **Success Response (200)**:  

```json
  { "message": "Feedback received." }
```

### **POST /reload-sources**

Triggers a hot-reload of config.ini. Every tunable is re-read - `[source_pool]`, `[validator]`, `[scheduler]`, `[backup]`, `[sources]` and the `[proxy_source_*]` sections - so you can add or remove proxy sources, update `predefined_sources`, and retune scoring or selection without restarting the service.

The reload is **authoritative**: the file is re-parsed into a fresh parser, so a key or a whole `[proxy_source_*]` section you delete from the file is genuinely dropped and reverts to its built-in default, rather than keeping its old in-memory value.

It is also **transactional**: the new configuration is applied as a unit. If any value fails to parse, the service rolls back to the configuration it was running and returns an error, instead of being left on a mix of old and new settings.

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
