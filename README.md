# **SmartProxy: An Intelligent, Self-Adapting Proxy Pool Service**

SmartProxy is a sophisticated proxy management system designed to provide reliable, high-quality proxy services. It automates the entire lifecycle of proxies, from fetching and validation to intelligent scoring and dynamic pool management, ensuring that your applications always have access to the best available proxies.

## **Core Features**

* **Automated Proxy Fetching**: Gathers proxies from multiple user-defined sources.  
* **Intelligent Validation & Scoring**: Continuously validates proxies and assigns a dynamic score based on performance (latency, success rate) and feedback.  
* **Feedback-Driven Adaptation**: A scoring system that penalizes failures and rewards success, allowing the pool to adapt based on real-world usage.  
* **Dynamic Source Reloading**: A hot-reload endpoint (/reload-sources) allows adding or removing proxy sources without restarting the service.  
* **Sustainable Validation Logic**: Employs a time-window-based attempt limit for re-validating failed proxies. This prevents proxy burnout, reduces database load, and ensures long-term service stability.  
* **Source-Specific Pools**: Maintains separate proxy pools for different sources/use cases.  
* **RESTful API**: Simple endpoints for fetching proxies and submitting feedback.  
* **Monitoring Dashboard**: A web-based UI to monitor the health and statistics of different proxy sources in real-time.

## **How It Works**

1. **Fetch**: The service periodically fetches proxy lists from various sources defined in config.ini.  
2. **Validate**: A validation cycle runs regularly. It prioritizes new and previously successful proxies. To avoid overwhelming unreliable proxies, it supplements the validation queue with failed proxies that have not been tested more than a configured number of times within a specific time window (e.g., 5 times in 30 minutes).  
3. **Score**: Proxies are managed in memory for each source. Feedback updates an ELO-inspired 0-100 score from recent success rate, latency, consistency, and optional time decay. Consecutive failures are kept only as diagnostic data; candidates are not hard-deleted by a failure threshold.  
4. **Select**: When a client requests a proxy for a specific source via /get-proxy, the system filters candidates by optional per-proxy cooldown, then selects from the current top pool using the configured strategy (`uniform`, `tiered`, `weighted`, or `softmax`).  
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
  * validation\_target / validation\_targets: URL(s) used to test proxy connectivity and anonymity. Use an endpoint that returns request headers, such as `http://httpbin.org/get`, if anonymity classification matters.
  * validation\_success\_threshold: Number of targets a proxy must pass.  
  * validation\_workers: Number of concurrent threads for validation.  
  * validation\_batch\_limit: Maximum proxies pulled into one validation cycle.  
  * validation\_supplement\_threshold: If the number of new/active proxies to test is below this, the queue will be supplemented with failed proxies.  
  * validation\_window\_minutes: The time window (in minutes) for the validation attempt limit.  
  * max\_validations\_per\_window: The maximum number of times a failed proxy will be re-tested within the time window.  
* **\[fetcher\]**:
  * use\_curl: Defaults to `false`. Enable only for local environments that intentionally route `curl` differently from Python.
* **\[scheduler\]**: Intervals for background tasks like fetching, validation, and flushing stats.  
* **\[sources\]**:  
  * predefined\_sources: A comma-separated list of logical names for your proxy pools (e.g., google\_search, web\_scraping).  
  * default\_source: The pool to use if a requested source doesn't exist.  
* **\[source\_pool\]**: Parameters for the scoring and selection algorithm, including `selection_strategy`, `proxy_cooldown_ms`, ELO window/decay settings, and latency scoring thresholds.  
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

Triggers a hot-reload of the proxy source configuration from config.ini. This allows you to add or remove \[proxy\_source\_\*\] sections and update predefined\_sources without restarting the service.

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
      "removed_predefined_sources": []  
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
