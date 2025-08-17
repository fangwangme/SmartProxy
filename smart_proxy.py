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
FAILED_STATUS_CODES = {
    0,
    4,
}  # Set of status codes that indicate success


class DatabaseManager:
    """Handles all interactions with the PostgreSQL database."""

    def __init__(self, config):
        try:
            # OPTIMIZATION: Increased maxconn to better handle concurrent workers.
            # The ideal number depends on your DB server's capacity.
            max_connections = config.getint("database", "max_connections", fallback=50)
            self.pool = psycopg2.pool.SimpleConnectionPool(
                minconn=2,
                maxconn=max_connections,
                host=config.get("database", "host"),
                port=config.get("database", "port"),
                dbname=config.get("database", "dbname"),
                user=config.get("database", "user"),
                password=config.get("database", "password"),
            )
            logger.info(
                f"Database connection pool created successfully (max_conn={max_connections})."
            )
        except (configparser.NoSectionError, psycopg2.OperationalError) as e:
            logger.error(f"Database configuration error or connection failed: {e}")
            sys.exit(1)

    def _execute(self, query, params=None, fetch=None):
        """A helper to execute queries using a connection from the pool."""
        conn = None
        try:
            conn = self.pool.getconn()
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
                if cur.rowcount > 0:
                    logger.info(f"Inserted {cur.rowcount} new proxies.")
                conn.commit()
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

    def get_eligible_failed_proxies(
        self, window_minutes: int, max_attempts: int, limit: int
    ) -> List[Tuple]:
        """
        Gets recently failed proxies that are eligible for re-validation based on the time window and attempt count.
        """
        query = """
            SELECT id, protocol, ip, port
            FROM proxies
            WHERE is_active = false
            AND (
                window_start_time IS NULL OR
                NOW() > window_start_time + INTERVAL '%(window)s minutes' OR
                validation_attempts_in_window < %(max_attempts)s
            )
            ORDER BY last_validated_at DESC NULLS FIRST, created_at DESC
            LIMIT %(limit)s;
        """
        params = {
            "window": window_minutes,
            "max_attempts": max_attempts,
            "limit": limit,
        }
        return self._execute(query, params, fetch="all") or []

    def update_validation_counters(self, proxy_ids: List[int], window_minutes: int):
        """
        Updates the validation counters for a batch of proxies before they are validated.
        Resets the counter and window if the window has expired.
        """
        if not proxy_ids:
            return

        query = """
            UPDATE proxies
            SET
                validation_attempts_in_window = CASE
                    WHEN window_start_time IS NULL OR NOW() > window_start_time + INTERVAL '%(window)s minutes'
                    THEN 1
                    ELSE validation_attempts_in_window + 1
                END,
                window_start_time = CASE
                    WHEN window_start_time IS NULL OR NOW() > window_start_time + INTERVAL '%(window)s minutes'
                    THEN NOW()
                    ELSE window_start_time
                END
            WHERE id = ANY(%(ids)s);
        """
        params = {"window": window_minutes, "ids": proxy_ids}
        self._execute(query, params)
        logger.debug(f"Updated validation counters for {len(proxy_ids)} proxies.")

    def batch_update_proxy_results(
        self, success_proxies: List[Dict], failure_proxy_ids: List[int]
    ):
        """
        OPTIMIZATION: Batch update results of a validation cycle.
        """
        conn = None
        try:
            conn = self.pool.getconn()
            with conn.cursor() as cur:
                # Batch update successful proxies
                if success_proxies:
                    update_query_success = """
                        UPDATE proxies SET
                            is_active = true,
                            latency_ms = data.latency_ms,
                            anonymity_level = data.anonymity_level,
                            last_validated_at = NOW()
                        FROM (VALUES %s) AS data(id, latency_ms, anonymity_level)
                        WHERE proxies.id = data.id;
                    """
                    psycopg2.extras.execute_values(
                        cur,
                        update_query_success,
                        [
                            (p["id"], p["latency"], p["anonymity"])
                            for p in success_proxies
                        ],
                    )
                    logger.info(
                        f"Batch updated {len(success_proxies)} successful proxies."
                    )

                # Batch update failed proxies
                if failure_proxy_ids:
                    update_query_failure = """
                        UPDATE proxies SET
                            is_active = false,
                            latency_ms = NULL,
                            anonymity_level = NULL,
                            last_validated_at = NOW()
                        WHERE id = ANY(%s);
                    """
                    cur.execute(update_query_failure, (failure_proxy_ids,))
                    logger.info(
                        f"Batch updated {len(failure_proxy_ids)} failed proxies."
                    )

                conn.commit()
        except psycopg2.Error as e:
            logger.error(f"Database batch update for validation results failed: {e}")
            if conn:
                conn.rollback()
        finally:
            if conn:
                self.pool.putconn(conn)

    def get_active_proxies(self) -> Set[str]:
        query = "SELECT protocol, ip, port FROM proxies WHERE is_active = true;"
        rows = self._execute(query, fetch="all")
        return (
            {f"{row['protocol']}://{row['ip']}:{row['port']}" for row in rows}
            if rows
            else set()
        )

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
            flushed_minutes = sorted(
                list({item[0].strftime("%H:%M") for item in stats_buffer})
            )
            logger.info(
                f"Flushed stats for {len(stats_buffer)} source-minute combination(s). Minutes: {flushed_minutes}"
            )
        except psycopg2.Error as e:
            logger.error(f"Failed to flush feedback stats to DB: {e}")
            if conn:
                conn.rollback()
        finally:
            if conn:
                self.pool.putconn(conn)

    def get_daily_stats(self, source: str, date: str):
        query = "SELECT COALESCE(SUM(success_count), 0) as total_success, COALESCE(SUM(failure_count), 0) as total_failure FROM source_stats_by_minute WHERE source_name = %s AND DATE(minute) = %s;"
        return self._execute(query, (source, date), fetch="one")

    def get_timeseries_stats(self, source: str, date: str, interval_minutes: int):
        query = """
            SELECT
                date_trunc('hour', minute) + (EXTRACT(minute FROM minute)::int / %(interval)s * %(interval)s) * interval '1 minute' AS interval_start,
                SUM(success_count) as success,
                SUM(failure_count) as failure
            FROM source_stats_by_minute
            WHERE source_name = %(source)s AND DATE(minute) = %(date)s
            GROUP BY interval_start ORDER BY interval_start;
        """
        return self._execute(
            query,
            {"source": source, "date": date, "interval": interval_minutes},
            fetch="all",
        )

    def get_distinct_sources(self) -> List[str]:
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

        self.active_proxies: Set[str] = set()
        self.source_stats: Dict[str, Dict[str, Dict]] = {}
        self.available_proxies: Dict[str, List[str]] = {}

        self.feedback_buffer = defaultdict(
            lambda: defaultdict(lambda: defaultdict(int))
        )

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
        self.validation_window_minutes = self.config.getint(
            "validator", "validation_window_minutes", fallback=30
        )
        self.max_validations_per_window = self.config.getint(
            "validator", "max_validations_per_window", fallback=5
        )
        self.validation_interval_s = self.config.getint(
            "scheduler", "validation_interval_seconds", fallback=60
        )
        self.stats_flush_interval_s = self.config.getint(
            "scheduler", "stats_flush_interval_seconds", fallback=60
        )
        self.source_refresh_interval_s = self.config.getint(
            "scheduler", "source_refresh_interval_seconds", fallback=300
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
        self.stats_pool_max_multiplier = self.config.getint(
            "source_pool", "stats_pool_max_multiplier", fallback=20
        )
        penalties_str = self.config.get(
            "source_pool", "failure_penalties", fallback="-1, -5, -20"
        )
        self.failure_penalties = [int(p.strip()) for p in penalties_str.split(",")]
        logger.info("Configuration loaded.")

    def _initialize_source_pools(self):
        with self.lock:
            for source in self.predefined_sources:
                if source not in self.source_stats:
                    self.source_stats[source] = {}
                    self.available_proxies[source] = []
            logger.info(
                f"Initialized in-memory pools for sources: {self.predefined_sources}"
            )

    def _load_fetcher_jobs(self) -> List[Dict]:
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
        return jobs

    def reload_sources(self) -> Dict:
        """
        Dynamically reloads proxy sources and fetcher jobs from the config file.
        """
        logger.info("Attempting to reload sources from config file...")
        with self.lock:
            self.config.read(self.config_path, encoding="utf-8")

            old_job_names = {job["name"] for job in self.fetcher_jobs}
            self.fetcher_jobs = self._load_fetcher_jobs()
            new_job_names = {job["name"] for job in self.fetcher_jobs}
            added_jobs = list(new_job_names - old_job_names)
            removed_jobs = list(old_job_names - new_job_names)
            if added_jobs or removed_jobs:
                logger.info(
                    f"Fetcher jobs reloaded. Added: {added_jobs}, Removed: {removed_jobs}"
                )
            else:
                logger.info("Fetcher jobs reloaded. No changes detected.")

            old_predefined_sources = self.predefined_sources.copy()
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

            added_sources = list(self.predefined_sources - old_predefined_sources)
            removed_sources = list(old_predefined_sources - self.predefined_sources)

            if added_sources:
                logger.info(f"Predefined sources changed. Added: {added_sources}")
                for source in added_sources:
                    if source not in self.source_stats:
                        self.source_stats[source] = {}
                        self.available_proxies[source] = []
                        logger.info(
                            f"Initialized new in-memory pool for source: {source}"
                        )

            if removed_sources:
                logger.info(f"Predefined sources changed. Removed: {removed_sources}")
                for source in removed_sources:
                    self.source_stats.pop(source, None)
                    self.available_proxies.pop(source, None)
                    logger.info(
                        f"Cleaned up in-memory pool for removed source: {source}"
                    )

            if not added_sources and not removed_sources:
                logger.info("Predefined sources reloaded. No changes detected.")

        return {
            "added_fetcher_jobs": added_jobs,
            "removed_fetcher_jobs": removed_jobs,
            "added_predefined_sources": added_sources,
            "removed_predefined_sources": removed_sources,
        }

    def _fetch_and_parse_source(self, job: Dict) -> List:
        """
        DEADLOCK FIX: This method now returns a list of proxies instead of writing to the DB.
        """
        url = job["url"]
        logger.info(f"Fetching proxy source: {job['name']} from {url}")
        proxies_to_insert = []
        try:
            response = requests.get(url, timeout=15)
            response.raise_for_status()
            for line in response.text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    if "://" in line:
                        protocol, rest = line.split("://", 1)
                        ip, port_str = rest.rsplit(":", 1)
                        proxies_to_insert.append((protocol.lower(), ip, int(port_str)))
                    elif job["default_protocol"]:
                        ip, port_str = line.rsplit(":", 1)
                        proxies_to_insert.append(
                            (job["default_protocol"].lower(), ip, int(port_str))
                        )
                except ValueError:
                    continue
        except requests.RequestException as e:
            logger.error(f"Failed to fetch from {job['name']} ({url}): {e}")
        return proxies_to_insert

    def _handle_fetch_results(self, futures: List):
        """
        DEADLOCK FIX: New method to consolidate results from all fetchers and insert in a single batch.
        """
        all_new_proxies = []
        for future in as_completed(futures):
            try:
                proxies = future.result()
                if proxies:
                    all_new_proxies.extend(proxies)
            except Exception as e:
                logger.error(f"A fetcher job raised an exception: {e}")

        if not all_new_proxies:
            logger.info("No new proxies were fetched in this cycle.")
            return

        # Remove duplicates before inserting to reduce DB load
        unique_proxies_set = {tuple(p) for p in all_new_proxies}
        unique_proxies_list = [list(p) for p in unique_proxies_set]

        logger.info(
            f"Consolidated {len(unique_proxies_list)} unique proxies from all sources for insertion."
        )
        self.db.insert_proxies(unique_proxies_list)

    def _validate_proxy(self, proxy_id: int, proxy_url: str) -> Dict:
        """
        OPTIMIZATION: This method now returns a result dict instead of calling the DB directly.
        """
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
            return {
                "id": proxy_id,
                "success": True,
                "latency": latency_ms,
                "anonymity": anonymity,
            }
        except (requests.RequestException, json.JSONDecodeError):
            return {"id": proxy_id, "success": False}

    def _run_validation_cycle(self):
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
                    f"Validation pool below threshold. Supplementing with eligible failed proxies."
                )
                eligible_failed = self.db.get_eligible_failed_proxies(
                    window_minutes=self.validation_window_minutes,
                    max_attempts=self.max_validations_per_window,
                    limit=supplement_needed,
                )
                existing_ids = {p["id"] for p in proxies_to_validate}
                for p in eligible_failed:
                    if p["id"] not in existing_ids:
                        proxies_to_validate.append(p)

            if not proxies_to_validate:
                logger.info("Validation cycle skipped: no proxies to validate.")
                return

            proxy_ids_to_update = [p["id"] for p in proxies_to_validate]
            self.db.update_validation_counters(
                proxy_ids_to_update, self.validation_window_minutes
            )

            total_to_validate = len(proxies_to_validate)
            logger.info(f"Starting validation for {total_to_validate} proxies...")

            success_proxies = []
            failure_proxy_ids = []

            with ThreadPoolExecutor(max_workers=self.validation_workers) as executor:
                future_to_proxy = {
                    executor.submit(
                        self._validate_proxy,
                        p["id"],
                        f"{p['protocol']}://{p['ip']}:{p['port']}",
                    ): p
                    for p in proxies_to_validate
                }
                for future in as_completed(future_to_proxy):
                    try:
                        result = future.result()
                        if result["success"]:
                            success_proxies.append(result)
                        else:
                            failure_proxy_ids.append(result["id"])
                    except Exception as exc:
                        proxy_info = future_to_proxy[future]
                        failure_proxy_ids.append(proxy_info["id"])
                        logger.error(
                            f"Proxy validation for {proxy_info['id']} generated an exception: {exc}"
                        )

            logger.info(
                f"Validation cycle finished. Success: {len(success_proxies)}, Failed: {len(failure_proxy_ids)}."
            )

            # OPTIMIZATION: Perform batch updates after all threads are complete
            self.db.batch_update_proxy_results(success_proxies, failure_proxy_ids)

            self._sync_and_select_top_proxies()
        finally:
            with self.lock:
                self.is_validating = False
            logger.info("Validation cycle lock released.")

    def _sync_and_select_top_proxies(self):
        """
        Syncs the stats pool with newly validated proxies and selects the
        Top-K proxies based purely on their score for the available pool.
        """
        logger.info(
            "Syncing and selecting Top-K proxies for all sources based on score..."
        )
        newly_active_proxies = self.db.get_active_proxies()

        with self.lock:
            self.active_proxies = newly_active_proxies
            for source in self.predefined_sources:
                stats_pool = self.source_stats.get(source, {})

                for proxy_url in self.active_proxies:
                    if proxy_url not in stats_pool:
                        logger.debug(
                            f"Adding new active proxy {proxy_url} to stats pool for source '{source}'."
                        )
                        stats_pool[proxy_url] = self._get_new_proxy_stat()

                sorted_proxies = sorted(
                    stats_pool.items(), key=lambda item: item[1]["score"], reverse=True
                )

                max_stats_size = self.max_pool_size * self.stats_pool_max_multiplier
                if len(sorted_proxies) > max_stats_size:
                    proxies_to_delete_count = len(sorted_proxies) - max_stats_size
                    logger.info(
                        f"Stats pool for source '{source}' exceeds limit ({len(sorted_proxies)} > {max_stats_size}). "
                        f"Removing {proxies_to_delete_count} lowest-scoring proxies."
                    )
                    sorted_proxies = sorted_proxies[:max_stats_size]
                    self.source_stats[source] = dict(sorted_proxies)
                else:
                    self.source_stats[source] = dict(sorted_proxies)

                top_k_proxies = [
                    proxy_url for proxy_url, _ in sorted_proxies[: self.max_pool_size]
                ]
                self.available_proxies[source] = top_k_proxies

                logger.info(
                    f"Source '{source}' synced. "
                    f"Total proxies with stats: {len(self.source_stats[source])}. "
                    f"Selected Top {len(self.available_proxies[source])} proxies based on score for the active pool."
                )

    def _update_dashboard_sources(self):
        logger.info("Refreshing dashboard sources from database...")
        db_sources = self.db.get_distinct_sources()
        with self.lock:
            self.dashboard_sources = set(db_sources)
        logger.info(
            f"Dashboard sources updated: {len(self.dashboard_sources)} sources found."
        )

    def _scheduler_loop(self):
        last_validation_run = 0
        last_flush_time = 0
        while not self.stop_scheduler_event.is_set():
            now = time.time()
            try:
                with self.lock:
                    current_jobs = list(self.fetcher_jobs)

                # DEADLOCK FIX: Submit jobs and handle results in a separate thread
                fetch_futures = []
                for job in current_jobs:
                    if now - job.get("last_run", 0) >= job["interval_minutes"] * 60:
                        job["last_run"] = now
                        fetch_futures.append(
                            self.fetch_executor.submit(
                                self._fetch_and_parse_source, job
                            )
                        )

                if fetch_futures:
                    logger.info(f"Submitted {len(fetch_futures)} fetcher jobs.")
                    threading.Thread(
                        target=self._handle_fetch_results,
                        args=(fetch_futures,),
                        daemon=True,
                    ).start()

                if now - last_validation_run >= self.validation_interval_s:
                    last_validation_run = now
                    threading.Thread(
                        target=self._run_validation_cycle, daemon=True
                    ).start()
                if now - last_flush_time >= self.stats_flush_interval_s:
                    last_flush_time = now
                    threading.Thread(
                        target=self._flush_feedback_buffer, daemon=True
                    ).start()
                if (
                    now - self.last_source_refresh_time
                    >= self.source_refresh_interval_s
                ):
                    self.last_source_refresh_time = now
                    threading.Thread(
                        target=self._update_dashboard_sources, daemon=True
                    ).start()
                self.stop_scheduler_event.wait(5)
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
        self._flush_feedback_buffer()
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            self.stop_scheduler_event.set()
            self.fetch_executor.shutdown(wait=True)
            self.scheduler_thread.join(timeout=10)
            logger.info("Background scheduler stopped.")

    def _get_new_proxy_stat(self) -> Dict:
        return {
            "score": 0,
            "success_count": 0,
            "failure_count": 0,
            "consecutive_failures": 0,
        }

    def _get_source_or_default(self, source: str) -> str:
        with self.lock:
            is_defined = source in self.predefined_sources
        return source if is_defined else self.default_source

    def get_proxy(self, source: str) -> Optional[str]:
        source = self._get_source_or_default(source)
        with self.lock:
            proxy_pool = self.available_proxies.get(source)
            if not proxy_pool:
                logger.warning(f"No available proxy for source '{source}'")
                return None
            proxy = random.choice(proxy_pool)
            logger.debug(f"Providing proxy {proxy} for source '{source}'")
            return proxy

    def _flush_feedback_buffer(self):
        """Flushes stats for all fully completed minutes to the database."""
        current_minute_start = datetime.now().replace(second=0, microsecond=0)

        records_to_flush = []
        minutes_to_clear = []

        with self.lock:
            buffer_keys = list(self.feedback_buffer.keys())
            for minute_timestamp in buffer_keys:
                if minute_timestamp < current_minute_start:
                    logger.debug(
                        f"Preparing to flush stats for completed minute: {minute_timestamp.strftime('%Y-%m-%d %H:%M')}"
                    )
                    for source, counts in self.feedback_buffer[
                        minute_timestamp
                    ].items():
                        records_to_flush.append(
                            (
                                minute_timestamp,
                                source,
                                counts.get("success", 0),
                                counts.get("failure", 0),
                            )
                        )
                    minutes_to_clear.append(minute_timestamp)

            for minute in minutes_to_clear:
                del self.feedback_buffer[minute]

        if records_to_flush:
            self.db.flush_feedback_stats(records_to_flush)
        else:
            logger.debug(
                "Flush stats task ran, but no completed minutes were found in the buffer."
            )

    def process_feedback(
        self,
        source: str,
        proxy_url: str,
        status_code: int,
        response_time_ms: Optional[int] = None,
    ):
        source = self._get_source_or_default(source)
        is_success = status_code not in FAILED_STATUS_CODES

        current_minute = datetime.now().replace(second=0, microsecond=0)

        with self.lock:
            if is_success:
                self.feedback_buffer[current_minute][source]["success"] += 1
            else:
                self.feedback_buffer[current_minute][source]["failure"] += 1

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
            else:
                stat["failure_count"] += 1
                stat["consecutive_failures"] += 1
                penalty_index = min(
                    stat["consecutive_failures"] - 1, len(self.failure_penalties) - 1
                )
                penalty = self.failure_penalties[penalty_index]
                stat["score"] += penalty

            logger.info(
                f"Computed Score for {source:<15} | {proxy_url:<30} | {status_code:<4} | {response_time_ms:<12.3f}ms -> {stat['score']:<10.4f}"
            )


def load_proxy_manager(config_path: str) -> ProxyManager:
    logger.info("Initializing ProxyManager...")
    manager = ProxyManager(config_path)
    manager._sync_and_select_top_proxies()
    manager._update_dashboard_sources()
    if not manager.active_proxies:
        logger.warning(
            "Cold start detected. Running initial synchronous fetch and validation..."
        )
        initial_jobs = manager._load_fetcher_jobs()
        manager.fetcher_jobs = initial_jobs
        fetch_futures = [
            manager.fetch_executor.submit(manager._fetch_and_parse_source, job)
            for job in initial_jobs
        ]

        # DEADLOCK FIX: Consolidate results before inserting
        all_proxies = []
        for future in as_completed(fetch_futures):
            try:
                proxies = future.result()
                if proxies:
                    all_proxies.extend(proxies)
            except Exception as e:
                logger.error(f"Initial fetcher job failed: {e}")

        if all_proxies:
            unique_proxies_set = {tuple(p) for p in all_proxies}
            unique_proxies_list = [list(p) for p in unique_proxies_set]
            logger.info(
                f"Initial fetch: Consolidated {len(unique_proxies_list)} unique proxies for insertion."
            )
            manager.db.insert_proxies(unique_proxies_list)

        manager._run_validation_cycle()
        logger.info("Initial validation complete.")
    return manager


# --- Flask API Server ---
app = Flask(__name__, static_folder="dashboard/dist")
CORS(app)
proxy_manager = load_proxy_manager(CONFIG_FILE_PATH)


@app.route("/get-proxy", methods=["GET"])
def get_proxy_route():
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
    status_code = data.get("status")
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
def reload_sources_route():
    """
    API endpoint to dynamically reload proxy sources from the config file.
    """
    try:
        result = proxy_manager.reload_sources()
        return (
            jsonify(
                {
                    "status": "success",
                    "message": "Configuration and sources reloaded.",
                    "details": result,
                }
            ),
            200,
        )
    except Exception as e:
        logger.error(f"Error during source reload via API: {e}", exc_info=True)
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "An internal error occurred during reload.",
                }
            ),
            500,
        )


@app.route("/api/sources", methods=["GET"])
def get_sources():
    return jsonify(sorted(list(proxy_manager.dashboard_sources)))


@app.route("/api/stats/daily", methods=["GET"])
def get_daily_stats_route():
    source = request.args.get("source")
    date = request.args.get("date")
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
    valid_intervals = [2, 5, 10, 30, 60]
    if interval not in valid_intervals:
        return jsonify({"error": f"'interval' must be one of {valid_intervals}."}), 400
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
                "success_count": row["success"],
            }
        )
    return jsonify(results)


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
