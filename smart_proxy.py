# -*- coding: utf-8 -*-
import configparser
import json
import os
import random
import signal
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

import psycopg2
import psycopg2.pool
import psycopg2.extras
import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from logger import logger

# --- Configuration & Constants ---
CONFIG_FILE_PATH = os.path.join("./", "config.ini")
SUCCESS_STATUS_CODES = {100, 7}  # Set of status codes that indicate success


class DatabaseManager:
    """Handles all interactions with the PostgreSQL database."""

    def __init__(self, config):
        try:
            self.pool = psycopg2.pool.SimpleConnectionPool(
                minconn=2,  # Increased min connections for stats writer
                maxconn=20,
                host=config.get("database", "host"),
                port=config.get("database", "port"),
                dbname=config.get("database", "dbname"),
                user=config.get("database", "user"),
                password=config.get("database", "password"),
            )
            logger.info("Database connection pool created successfully.")
        except (configparser.NoSectionError, psycopg2.OperationalError) as e:
            logger.error(f"Database configuration error or connection failed: {e}")
            sys.exit(1)

    def _execute(self, query, params=None, fetch=None):
        """A helper to execute queries using a connection from the pool."""
        conn = None
        try:
            conn = self.pool.getconn()
            # Use a dictionary cursor for easier data handling in API endpoints
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(query, params)
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
                conn.commit()
        except psycopg2.Error as e:
            logger.error(f"Database query failed: {e}")
            if conn:
                conn.rollback()
            return None
        finally:
            if conn:
                self.pool.putconn(conn)

    def insert_proxies(self, proxies: List[Tuple[str, str, int]]):
        """Inserts a list of proxies, ignoring duplicates."""
        if not proxies:
            return
        query = "INSERT INTO proxies (protocol, ip, port) VALUES %s ON CONFLICT (protocol, ip, port) DO NOTHING;"
        conn = None
        try:
            conn = self.pool.getconn()
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, query, proxies)
                inserted_count = cur.rowcount
                conn.commit()
            if inserted_count > 0:
                logger.info(f"Inserted {inserted_count} new proxies.")
        except psycopg2.Error as e:
            logger.error(f"Database batch insert failed: {e}")
            if conn:
                conn.rollback()
        finally:
            if conn:
                self.pool.putconn(conn)

    def get_proxies_to_validate(self, interval_minutes=30) -> List[Tuple]:
        query = "SELECT id, protocol, ip, port FROM proxies WHERE last_validated_at IS NULL OR (is_active = true AND last_validated_at < NOW() - INTERVAL '%s minutes');"
        return self._execute(query, (interval_minutes,), fetch="all") or []

    def get_recent_failed_proxies(self, limit: int) -> List[Tuple]:
        query = "SELECT id, protocol, ip, port FROM proxies WHERE is_active = false ORDER BY created_at DESC LIMIT %s;"
        return self._execute(query, (limit,), fetch="all") or []

    def update_proxy_validation_result(
        self,
        proxy_id: int,
        is_active: bool,
        latency: Optional[int],
        anonymity: Optional[str],
    ):
        query = "UPDATE proxies SET is_active = %s, latency_ms = %s, anonymity_level = %s, last_validated_at = NOW() WHERE id = %s;"
        self._execute(query, (is_active, latency, anonymity, proxy_id))

    def get_active_proxies(self) -> Set[str]:
        query = "SELECT protocol, ip, port FROM proxies WHERE is_active = true;"
        rows = self._execute(query, fetch="all")
        return (
            {f"{row['protocol']}://{row['ip']}:{row['port']}" for row in rows}
            if rows
            else set()
        )

    # Flushes the in-memory feedback buffer to the database
    def flush_feedback_stats(self, stats_buffer: List[Tuple]):
        if not stats_buffer:
            return
        query = """
            INSERT INTO source_stats_by_minute (minute, source_name, success_count, failure_count)
            VALUES %s
            ON CONFLICT (minute, source_name) DO UPDATE SET
                success_count = source_stats_by_minute.success_count + EXCLUDED.success_count,
                failure_count = source_stats_by_minute.failure_count + EXCLUDED.failure_count;
        """
        conn = None
        try:
            conn = self.pool.getconn()
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, query, stats_buffer)
                conn.commit()
            logger.info(
                f"Flushed {len(stats_buffer)} minute-aggregated stats to the database."
            )
        except psycopg2.Error as e:
            logger.error(f"Failed to flush feedback stats to DB: {e}")
            if conn:
                conn.rollback()
        finally:
            if conn:
                self.pool.putconn(conn)

    # Gets daily summary stats for the dashboard
    def get_daily_stats(self, source: str, date: str):
        query = """
            SELECT 
                COALESCE(SUM(success_count), 0) as total_success,
                COALESCE(SUM(failure_count), 0) as total_failure
            FROM source_stats_by_minute
            WHERE source_name = %s AND DATE(minute) = %s;
        """
        return self._execute(query, (source, date), fetch="one")

    # Gets time-series stats for the dashboard chart
    def get_timeseries_stats(self, source: str, date: str, interval_minutes: int):
        """
        Calculates the success rate for each interval within a specific day.
        """
        query = """
            SELECT
                -- Truncate the timestamp to the desired interval
                date_trunc('hour', minute) + (EXTRACT(minute FROM minute)::int / %(interval)s * %(interval)s) * interval '1 minute' AS interval_start,
                SUM(success_count) as success,
                SUM(failure_count) as failure
            FROM source_stats_by_minute
            WHERE
                source_name = %(source)s AND
                DATE(minute) = %(date)s
            GROUP BY interval_start
            ORDER BY interval_start;
        """
        return self._execute(
            query,
            {"source": source, "date": date, "interval": interval_minutes},
            fetch="all",
        )

    # Gets a list of all unique source names from the stats table.
    def get_distinct_sources(self) -> List[str]:
        """Gets a list of all unique source names from the stats table."""
        query = "SELECT DISTINCT source_name FROM source_stats_by_minute ORDER BY source_name;"
        rows = self._execute(query, fetch="all")
        return [row["source_name"] for row in rows] if rows else []


class ProxyManager:
    """Manages the proxy lifecycle, state, and business logic."""

    def __init__(self, config_path):
        self.config_path = config_path
        self.config = configparser.ConfigParser()
        self.config.read(config_path, encoding="utf-8")

        self.db = DatabaseManager(self.config)
        self.lock = threading.Lock()

        # --- In-Memory State ---
        self.active_proxies: Set[str] = set()
        self.source_stats: Dict[str, Dict[str, Dict]] = {}
        self.available_proxies: Dict[str, List[str]] = {}

        # In-memory buffer for minute-level feedback stats
        self.feedback_buffer = defaultdict(lambda: defaultdict(int))
        self.last_flush_time = time.time()

        self.dashboard_sources: Set[str] = set()
        self.last_source_refresh_time = 0

        self._load_config()
        self._initialize_source_pools()

        self.fetcher_jobs = self._load_fetcher_jobs()
        self.scheduler_thread = None
        self.stop_scheduler_event = threading.Event()
        self.fetch_executor = ThreadPoolExecutor(
            max_workers=10, thread_name_prefix="Fetcher"
        )
        self.is_validating = False

    def _load_config(self):
        """Loads all settings from the config file."""
        self.server_port = self.config.getint("server", "port", fallback=6942)
        self.validation_workers = self.config.getint(
            "validator", "validation_workers", fallback=100
        )
        self.validation_timeout_s = self.config.getint(
            "validator", "validation_timeout_s", fallback=5
        )
        self.validation_target = self.config.get(
            "validator", "validation_target", fallback="http://httpbin.org/get"
        )
        self.validation_supplement_threshold = self.config.getint(
            "validator", "validation_supplement_threshold", fallback=1000
        )
        self.validation_interval_s = self.config.getint(
            "scheduler", "validation_interval_seconds", fallback=60
        )
        # Interval for flushing feedback buffer
        self.stats_flush_interval_s = self.config.getint(
            "scheduler", "stats_flush_interval_seconds", fallback=60
        )

        self.source_refresh_interval_s = self.config.getint(
            "scheduler", "source_refresh_interval_seconds", fallback=3600
        )

        sources_str = self.config.get(
            "sources", "predefined_sources", fallback="default"
        )
        self.predefined_sources = {
            s.strip() for s in sources_str.split(",") if s.strip()
        }
        self.default_source = self.config.get(
            "sources", "default_source", fallback="default"
        )
        if self.default_source not in self.predefined_sources:
            self.predefined_sources.add(self.default_source)

        self.max_pool_size = self.config.getint(
            "source_pool", "max_pool_size", fallback=500
        )
        penalties_str = self.config.get(
            "source_pool", "failure_penalties", fallback="-1, -10, -100"
        )
        self.failure_penalties = [int(p.strip()) for p in penalties_str.split(",")]
        logger.info("Configuration loaded.")

    def _initialize_source_pools(self):
        """Initializes in-memory structures for all predefined sources."""
        with self.lock:
            for source in self.predefined_sources:
                if source not in self.source_stats:
                    self.source_stats[source] = {}
                    self.available_proxies[source] = []
            logger.info(
                f"Initialized in-memory pools for sources: {self.predefined_sources}"
            )

    def _load_fetcher_jobs(self) -> List[Dict]:
        """Loads proxy source jobs from the config file."""
        jobs = []
        for section in self.config.sections():
            if section.startswith("proxy_source_"):
                job = {
                    "name": section,
                    "url": self.config.get(section, "url", fallback=None),
                    "interval_minutes": self.config.getint(
                        section, "update_interval_minutes", fallback=60
                    ),
                    "default_protocol": self.config.get(
                        section, "default_protocol", fallback=None
                    ),
                    "last_run": 0,
                }
                if job["url"]:
                    jobs.append(job)
        logger.info(f"Loaded {len(jobs)} proxy source jobs from config.")
        return jobs

    def _fetch_and_parse_source(self, job: Dict):
        """Fetches a source, parses proxies, and inserts them into the DB."""
        url = job["url"]
        logger.info(f"Fetching proxy source: {job['name']} from {url}")
        try:
            response = requests.get(url, timeout=15)
            response.raise_for_status()
            proxies_to_insert = []
            lines = response.text.splitlines()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    if "://" in line:
                        protocol, rest = line.split("://", 1)
                        ip, port_str = rest.rsplit(":", 1)
                        port = int(port_str)
                        proxies_to_insert.append((protocol.lower(), ip, port))
                    elif job["default_protocol"]:
                        ip, port_str = line.rsplit(":", 1)
                        port = int(port_str)
                        proxies_to_insert.append(
                            (job["default_protocol"].lower(), ip, port)
                        )
                except ValueError:
                    continue

            with self.lock:
                self.db.insert_proxies(proxies_to_insert)
        except requests.RequestException as e:
            logger.error(f"Failed to fetch from {job['name']} ({url}): {e}")

    def _validate_proxy(self, proxy_id: int, proxy_url: str):
        """Validates a single proxy and updates its status in the DB."""
        proxies = {"http": proxy_url, "https": proxy_url}
        start_time = time.time()
        try:
            response = requests.get(
                self.validation_target,
                proxies=proxies,
                timeout=self.validation_timeout_s,
            )
            response.raise_for_status()
            latency_ms = int((time.time() - start_time) * 1000)
            headers = response.json().get("headers", {})
            is_anonymous = not any(
                h in headers for h in ["X-Forwarded-For", "Via", "X-Real-Ip"]
            )
            anonymity = "elite" if is_anonymous else "transparent"
            self.db.update_proxy_validation_result(
                proxy_id, True, latency_ms, anonymity
            )
            return True
        except (requests.RequestException, json.JSONDecodeError):
            self.db.update_proxy_validation_result(proxy_id, False, None, None)
            return False

    def _run_validation_cycle(self):
        """Runs a full validation cycle with smart supplementation."""
        with self.lock:
            if self.is_validating:
                logger.warning(
                    "Validation cycle is already in progress. Skipping this scheduled run."
                )
                return
            self.is_validating = True

        try:
            proxies_to_validate = self.db.get_proxies_to_validate()
            if len(proxies_to_validate) < self.validation_supplement_threshold:
                supplement_needed = self.validation_supplement_threshold - len(
                    proxies_to_validate
                )
                logger.info(
                    f"Validation pool below threshold. Supplementing with {supplement_needed} recent failed proxies."
                )
                recent_failed = self.db.get_recent_failed_proxies(
                    limit=supplement_needed
                )
                existing_ids = {p[0] for p in proxies_to_validate}
                for p in recent_failed:
                    if p[0] not in existing_ids:
                        proxies_to_validate.append(p)

            if not proxies_to_validate:
                logger.info("Validation cycle skipped: no proxies to validate.")
                return

            total_to_validate = len(proxies_to_validate)
            logger.info(f"Starting validation for {total_to_validate} proxies...")

            success_count = 0
            processed_count = 0
            with ThreadPoolExecutor(max_workers=self.validation_workers) as executor:
                future_to_proxy = {
                    executor.submit(
                        self._validate_proxy, p[0], f"{p[1]}://{p[2]}:{p[3]}"
                    ): p
                    for p in proxies_to_validate
                }
                for future in as_completed(future_to_proxy):
                    processed_count += 1
                    try:
                        if future.result():
                            success_count += 1
                    except Exception as exc:
                        logger.error(f"Proxy validation generated an exception: {exc}")

                    if (
                        processed_count % 500 == 0
                        and processed_count < total_to_validate
                    ):
                        logger.info(
                            f"Validation progress: {processed_count}/{total_to_validate} proxies checked."
                        )
            logger.info(
                f"Validation cycle finished. Success: {success_count}, Failed: {total_to_validate - success_count}."
            )
            # Call the new sync and select method
            self._sync_and_select_top_proxies()
        finally:
            with self.lock:
                self.is_validating = False
            logger.info("Validation cycle lock released.")

    def _sync_and_select_top_proxies(self):
        """
        Syncs stats with all active proxies, resets stats for new ones,
        then sorts and selects the top K proxies for the available pool.
        """
        logger.info("Syncing and selecting Top-K proxies for all sources...")
        newly_active_proxies = self.db.get_active_proxies()

        with self.lock:
            self.active_proxies = newly_active_proxies

            for source in self.predefined_sources:
                stats_pool = self.source_stats.get(source, {})

                # 1. Add all active proxies to the stats pool and reset their stats
                for proxy_url in self.active_proxies:
                    # Whether it's new or existing, reset its stats to give it a fresh start
                    stats_pool[proxy_url] = self._get_new_proxy_stat()

                # 2. Sort the entire stats pool by score
                # Items are (proxy_url, stat_dict)
                sorted_proxies = sorted(
                    stats_pool.items(), key=lambda item: item[1]["score"], reverse=True
                )

                # 3. Select the Top K proxies and update the available pool
                top_k_proxies = [
                    proxy_url for proxy_url, _ in sorted_proxies[: self.max_pool_size]
                ]
                self.available_proxies[source] = top_k_proxies

                # Optional: Trim the main stats pool to save memory, though not strictly necessary
                self.source_stats[source] = dict(sorted_proxies)

                logger.info(
                    f"Source '{source}' synced. Total proxies in stats: {len(self.source_stats[source])}. "
                    f"Selected Top {len(self.available_proxies[source])} for active pool."
                )

    # Flushes the feedback buffer to the database
    def _flush_feedback_buffer(self):
        """Copies and clears the buffer, then writes its contents to the DB."""
        with self.lock:
            if not self.feedback_buffer:
                return

            # Create a copy of the buffer to be flushed
            buffer_copy = self.feedback_buffer
            # Reset the main buffer
            self.feedback_buffer = defaultdict(lambda: defaultdict(int))

        # Prepare data for batch insert
        records_to_flush = []
        now = datetime.now().replace(second=0, microsecond=0)  # Truncate to the minute

        for source, counts in buffer_copy.items():
            records_to_flush.append(
                (now, source, counts.get("success", 0), counts.get("failure", 0))
            )

        if records_to_flush:
            self.db.flush_feedback_stats(records_to_flush)

    # Periodically refreshes the list of sources for the dashboard from the DB.
    def _update_dashboard_sources(self):
        """Periodically refreshes the list of sources for the dashboard from the DB."""
        logger.info("Refreshing dashboard sources from database...")
        db_sources = self.db.get_distinct_sources()
        with self.lock:
            self.dashboard_sources = set(db_sources)
        logger.info(
            f"Dashboard sources updated: {len(self.dashboard_sources)} sources found."
        )

    def _scheduler_loop(self):
        """The main loop for background tasks."""
        last_validation_run = 0
        while not self.stop_scheduler_event.is_set():
            now = time.time()
            try:
                # --- Fetcher Jobs ---
                for job in self.fetcher_jobs:
                    if now - job.get("last_run", 0) >= job["interval_minutes"] * 60:
                        job["last_run"] = now
                        self.fetch_executor.submit(self._fetch_and_parse_source, job)

                # --- Validation Cycle ---
                if now - last_validation_run >= self.validation_interval_s:
                    last_validation_run = now
                    threading.Thread(
                        target=self._run_validation_cycle, daemon=True
                    ).start()

                # --- Stats Flushing ---
                if now - self.last_flush_time >= self.stats_flush_interval_s:
                    self.last_flush_time = now
                    # Run in a separate thread to avoid blocking the scheduler loop
                    threading.Thread(
                        target=self._flush_feedback_buffer, daemon=True
                    ).start()

                # --- Dashboard Source Refresh ---
                if (
                    now - self.last_source_refresh_time
                    >= self.source_refresh_interval_s
                ):
                    self.last_source_refresh_time = now
                    threading.Thread(
                        target=self._update_dashboard_sources, daemon=True
                    ).start()

                self.stop_scheduler_event.wait(5)  # Check every 5 seconds
            except Exception as e:
                logger.error(f"Error in scheduler loop: {e}", exc_info=True)
                self.stop_scheduler_event.wait(60)

    def start_scheduler(self):
        if not self.scheduler_thread or not self.scheduler_thread.is_alive():
            self.stop_scheduler_event.clear()
            self.scheduler_thread = threading.Thread(
                target=self._scheduler_loop, daemon=True
            )
            self.scheduler_thread.start()
            logger.info("Background scheduler started.")

    def stop_scheduler(self):
        logger.info("Stopping scheduler and flushing final stats...")
        self._flush_feedback_buffer()  # Final flush before exit
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            self.stop_scheduler_event.set()
            self.fetch_executor.shutdown(wait=True)
            self.scheduler_thread.join(timeout=10)
            logger.info("Background scheduler stopped.")

    def _get_new_proxy_stat(self) -> Dict:
        """Returns a clean, default state for a proxy's score."""
        return {
            "score": 0,
            "success_count": 0,
            "failure_count": 0,
            "consecutive_failures": 0,
        }

    def _get_source_or_default(self, source: str) -> str:
        """Returns the source if predefined, otherwise returns the default source."""
        return source if source in self.predefined_sources else self.default_source

    def get_proxy(self, source: str) -> Optional[str]:
        """Gets a random proxy from the pre-selected available pool for a source."""
        source = self._get_source_or_default(source)
        with self.lock:
            proxy_pool = self.available_proxies.get(source)
            if not proxy_pool:
                return None
            return random.choice(proxy_pool)

    # Processes feedback by updating the in-memory buffer and proxy score
    def process_feedback(
        self,
        source: str,
        proxy_url: str,
        status_code: int,
        response_time_ms: Optional[int] = None,
    ):
        source = self._get_source_or_default(source)

        is_success = status_code in SUCCESS_STATUS_CODES

        with self.lock:
            # --- Part 1: Update the minute-level statistics buffer ---
            if is_success:
                self.feedback_buffer[source]["success"] += 1
            else:
                self.feedback_buffer[source]["failure"] += 1

            # --- Part 2: Update the in-memory proxy score for selection logic ---
            stat = self.source_stats.get(source, {}).get(proxy_url)
            if not stat:
                return

            if is_success:
                stat["success_count"] += 1
                base_score_gain = 1
                latency_bonus = max(
                    0, round((2000 - (response_time_ms or 2000)) / 400.0, 2)
                )
                total_gain = base_score_gain + latency_bonus
                stat["score"] = (
                    total_gain
                    if stat["consecutive_failures"] > 0
                    else stat["score"] + total_gain
                )
                stat["consecutive_failures"] = 0
            else:  # Failure
                stat["failure_count"] += 1
                stat["consecutive_failures"] += 1
                penalty_index = min(
                    stat["consecutive_failures"] - 1, len(self.failure_penalties) - 1
                )
                penalty = self.failure_penalties[penalty_index]
                stat["score"] += penalty


def load_proxy_manager(config_path: str) -> ProxyManager:
    """Loads ProxyManager and initializes its state."""
    logger.info("Initializing ProxyManager...")
    manager = ProxyManager(config_path)
    manager._sync_and_select_top_proxies()
    if not manager.active_proxies:
        logger.warning(
            "Cold start detected. Running initial synchronous fetch and validation..."
        )
        fetch_futures = [
            manager.fetch_executor.submit(manager._fetch_and_parse_source, job)
            for job in manager.fetcher_jobs
        ]
        for future in as_completed(fetch_futures):
            pass
        # This will run validation and then the new _sync_and_select_top_proxies method
        manager._run_validation_cycle()
        logger.info("Initial validation complete.")
    return manager


# --- Flask API Server ---
app = Flask(__name__, static_folder="dashboard/dist")
CORS(app)
proxy_manager = load_proxy_manager(CONFIG_FILE_PATH)


# --- API Endpoints ---
@app.route("/get-proxy", methods=["GET"])
def get_proxy():
    source = request.args.get("source")
    if not source:
        return jsonify({"error": "Query parameter 'source' is required."}), 400
    proxy_url = proxy_manager.get_proxy(source)
    if proxy_url:
        return jsonify({"http": proxy_url, "https": proxy_url})
    else:
        return (
            jsonify(
                {"error": f"No available proxy for source '{source}' at the moment."}
            ),
            404,
        )


@app.route("/feedback", methods=["POST"])
def feedback_route():
    data = request.json
    source = data.get("source")
    proxy_url = data.get("proxy")
    status_code = data.get("status")  # [MODIFIED]
    resp_time = data.get("response_time_ms")
    logger.info(
        f"Handled feedback: {source} - {status_code} - {proxy_url} - {resp_time}"
    )

    if not all([source, proxy_url]) or not isinstance(status_code, int):
        return (
            jsonify(
                {
                    "error": "Invalid feedback data. 'source', 'proxy', and 'status_code' (int) are required."
                }
            ),
            400,
        )

    proxy_manager.process_feedback(source, proxy_url, status_code, resp_time)
    return jsonify({"message": "Feedback received."})


@app.route("/reload-sources", methods=["POST"])
def reload_sources():
    """Dynamically reloads the source configuration from the config file."""
    result = proxy_manager.reload_sources_from_config()
    return jsonify(result)


# --- Dashboard API Endpoints ---
@app.route("/api/sources", methods=["GET"])
def get_sources():
    """Returns the list of source names found in the stats database (cached)."""
    return jsonify(sorted(list(proxy_manager.dashboard_sources)))


@app.route("/api/stats/daily", methods=["GET"])
def get_daily_stats_route():
    source = request.args.get("source")
    date = request.args.get("date")  # Expected format: YYYY-MM-DD
    if not all([source, date]):
        return (
            jsonify({"error": "'source' and 'date' query parameters are required."}),
            400,
        )

    stats = proxy_manager.db.get_daily_stats(source, date)
    if stats:
        total = stats["total_success"] + stats["total_failure"]
        success_rate = (stats["total_success"] / total * 100) if total > 0 else 0
        return jsonify(
            {
                "total_requests": total,
                "total_success": stats["total_success"],
                "success_rate": round(success_rate, 2),
            }
        )
    return jsonify({"total_requests": 0, "total_success": 0, "success_rate": 0})


@app.route("/api/stats/timeseries", methods=["GET"])
def get_timeseries_stats_route():
    source = request.args.get("source")
    date = request.args.get("date")
    interval = request.args.get("interval", "10", type=int)

    if not all([source, date]):
        return (
            jsonify({"error": "'source' and 'date' query parameters are required."}),
            400,
        )
    if interval not in [5, 10, 60]:
        return jsonify({"error": "'interval' must be 5, 10, or 60."}), 400

    stats = proxy_manager.db.get_timeseries_stats(source, date, interval)

    results = []
    for row in stats:
        total = row["success"] + row["failure"]
        success_rate = (row["success"] / total * 100) if total > 0 else 0
        results.append(
            {
                "time": row["interval_start"].strftime("%H:%M"),
                "success_rate": round(success_rate, 2),
                "total_requests": total,
            }
        )
    return jsonify(results)


# --- Frontend Serving ---
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path):
    if path != "" and os.path.exists(os.path.join(app.static_folder, path)):
        return send_from_directory(app.static_folder, path)
    else:
        return send_from_directory(app.static_folder, "index.html")


def handle_shutdown(signal, frame):
    logger.info("Shutdown signal received. Performing graceful shutdown...")
    proxy_manager.stop_scheduler()
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    proxy_manager.start_scheduler()
    app.run(host="0.0.0.0", port=proxy_manager.server_port, debug=False)
