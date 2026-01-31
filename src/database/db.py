# -*- coding: utf-8 -*-
import sys
import configparser
import psycopg2
import psycopg2.pool
import psycopg2.extras
from typing import List, Dict, Set, Tuple
from src.utils.logger import logger

class DatabaseManager:
    """Handles all interactions with the PostgreSQL database."""

    def __init__(self, config):
        try:
            # OPTIMIZATION: Increased maxconn to better handle concurrent workers.
            # The ideal number depends on your DB server's capacity.
            max_connections = config.getint("database", "max_connections", fallback=50)
            # Use ThreadedConnectionPool for thread-safe access in multi-threaded environment
            self.pool = psycopg2.pool.ThreadedConnectionPool(
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
                logger.info(f"Inserted {cur.rowcount}/ {len(proxies)} new proxies.")
                conn.commit()
        except Exception as e:
            logger.error(f"Database batch insert failed: {e}")
            if conn:
                conn.rollback()
        finally:
            if conn:
                self.pool.putconn(conn)

    def get_proxies_to_validate(self, interval_minutes=30, limit=2000) -> List[Tuple]:
        query = "SELECT id, protocol, ip, port FROM proxies WHERE last_validated_at IS NULL OR (is_active = true AND last_validated_at < NOW() - INTERVAL '%s minutes') LIMIT %s;"
        return self._execute(query, (interval_minutes, limit), fetch="all") or []

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
