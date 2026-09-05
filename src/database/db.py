# -*- coding: utf-8 -*-
import sys
import configparser
import random
import time
import uuid
from datetime import datetime, timezone
import psycopg2
import psycopg2.pool
import psycopg2.extras
from typing import List, Dict, Optional, Set, Tuple
from src.utils.logger import logger


RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})


class DatabaseWriteError(RuntimeError):
    """A database mutation that did not commit."""

    def __init__(self, operation: str, cause: Exception):
        super().__init__(f"Database write failed during {operation}")
        self.operation = operation
        self.cause = cause

class DatabaseManager:
    """Handles all interactions with the PostgreSQL database."""

    def __init__(self, config):
        try:
            # OPTIMIZATION: Increased maxconn to better handle concurrent workers.
            # The ideal number depends on your DB server's capacity.
            min_connections = config.getint("database", "min_connections", fallback=2)
            max_connections = config.getint("database", "max_connections", fallback=50)
            self.write_max_retries = config.getint(
                "database", "write_max_retries", fallback=3
            )
            self.write_retry_base_ms = config.getint(
                "database", "write_retry_base_ms", fallback=25
            )
            # Use ThreadedConnectionPool for thread-safe access in multi-threaded environment
            self.pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=min_connections,
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

    def _execute(self, query, params=None, fetch=None, operation="query"):
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
                return True
        except psycopg2.Error as e:
            logger.error(f"Database query failed: {e}")
            if conn:
                conn.rollback()
            if fetch:
                return None
            raise DatabaseWriteError(operation, e) from e
        except Exception as e:
            logger.error(
                "Database operation '{}' failed with {}.",
                operation,
                type(e).__name__,
            )
            if conn:
                conn.rollback()
            if fetch:
                return None
            raise DatabaseWriteError(operation, e) from e
        finally:
            if conn:
                self.pool.putconn(conn)

    def _run_transaction(self, operation, callback, max_attempts=None):
        """Run one mutation with bounded retry for transaction-level conflicts."""
        attempts = max(1, getattr(self, "write_max_retries", 3) + 1)
        if max_attempts is not None:
            attempts = min(attempts, max(1, int(max_attempts)))
        base_ms = max(0, getattr(self, "write_retry_base_ms", 25))
        for attempt in range(attempts):
            conn = None
            retry_delay = None
            try:
                conn = self.pool.getconn()
                callback(conn)
                conn.commit()
                return True
            except psycopg2.Error as error:
                if conn:
                    conn.rollback()
                retryable = getattr(error, "pgcode", None) in RETRYABLE_SQLSTATES
                if not retryable or attempt + 1 >= attempts:
                    logger.error(
                        "Database mutation '{}' failed after {} attempt(s), sqlstate={}.",
                        operation,
                        attempt + 1,
                        getattr(error, "pgcode", None),
                    )
                    raise DatabaseWriteError(operation, error) from error
                retry_delay = base_ms * (2**attempt) / 1000.0
                retry_delay += random.uniform(
                    0.0, retry_delay * 0.25 if retry_delay else 0.0
                )
                logger.warning(
                    "Retrying database mutation '{}' after sqlstate={} (attempt {}/{}).",
                    operation,
                    getattr(error, "pgcode", None),
                    attempt + 1,
                    attempts,
                )
            except Exception as error:
                if conn:
                    conn.rollback()
                raise DatabaseWriteError(operation, error) from error
            finally:
                if conn:
                    self.pool.putconn(conn)
            if retry_delay is not None:
                time.sleep(retry_delay)

    def ping(self) -> bool:
        """Return whether a lightweight database readiness query succeeds."""
        return self._execute("SELECT 1;", fetch="one") is not None

    def insert_proxies(self, proxies: List[Tuple[str, str, int]]) -> bool:
        """Inserts a list of proxies, ignoring duplicates."""
        if not proxies:
            return True
        query = "INSERT INTO proxies (protocol, ip, port) VALUES %s ON CONFLICT (protocol, ip, port) DO NOTHING;"
        ordered = sorted(tuple(row) for row in proxies)

        def write(conn):
            with conn.cursor() as cur:
                # page_size defaults to 100, which turns a 5k-row insert into
                # 50 round trips.
                psycopg2.extras.execute_values(cur, query, ordered, page_size=1000)
                # NOTE: cursor.rowcount is deliberately not reported here.
                # psycopg2 documents that after execute_values it "will not
                # contain a total result" - it only reflects the last page.
                logger.info(f"Committed {len(ordered)} proxy rows for insertion.")

        return self._run_transaction("insert_proxies", write)

    def get_new_proxies_to_validate(self, limit: int) -> List[Tuple]:
        """Proxies that have never been validated, oldest discovery first."""
        if limit <= 0:
            return []
        query = """
            SELECT id, protocol, ip, port
            FROM proxies
            WHERE last_validated_at IS NULL
            ORDER BY created_at ASC, id ASC
            LIMIT %(limit)s;
        """
        return self._execute(query, {"limit": limit}, fetch="all") or []

    def get_active_proxies_to_revalidate(
        self, interval_minutes: int, limit: int
    ) -> List[Tuple]:
        """Live proxies whose liveness check has gone stale, oldest first."""
        if limit <= 0:
            return []
        query = """
            SELECT id, protocol, ip, port
            FROM proxies
            WHERE is_active = true
              AND last_validated_at IS NOT NULL
              AND last_validated_at < NOW() - INTERVAL '%(window)s minutes'
            ORDER BY last_validated_at ASC, id ASC
            LIMIT %(limit)s;
        """
        params = {"window": interval_minutes, "limit": limit}
        return self._execute(query, params, fetch="all") or []

    def get_eligible_failed_proxies(
        self,
        window_minutes: int,
        max_attempts: int,
        limit: int,
        exclude_ids: Optional[List[int]] = None,
    ) -> List[Tuple]:
        """
        Gets previously failed proxies that are eligible for re-validation based
        on the time window and attempt count.

        Ordering is ASC on last_validated_at: the proxies worth re-testing are
        the ones nobody has touched in a while, not the ones that just failed
        (batch_update_proxy_results stamps those with NOW()).

        Rows with last_validated_at IS NULL are excluded - those have never been
        validated and belong to get_new_proxies_to_validate. Including them here
        made this query return candidates the main query had already claimed,
        which were then deduplicated in Python *after* the SQL LIMIT had
        already spent the budget on them.
        """
        if limit <= 0:
            return []
        query = """
            SELECT id, protocol, ip, port
            FROM proxies
            WHERE is_active = false
            AND last_validated_at IS NOT NULL
            AND (
                window_start_time IS NULL OR
                NOW() > window_start_time + INTERVAL '%(window)s minutes' OR
                validation_attempts_in_window < %(max_attempts)s
            )
            AND (%(exclude_ids)s::int[] IS NULL OR NOT (id = ANY(%(exclude_ids)s::int[])))
            ORDER BY last_validated_at ASC, created_at ASC
            LIMIT %(limit)s;
        """
        params = {
            "window": window_minutes,
            "max_attempts": max_attempts,
            "limit": limit,
            "exclude_ids": list(exclude_ids) if exclude_ids else None,
        }
        return self._execute(query, params, fetch="all") or []

    def batch_update_proxy_results(
        self,
        success_proxies: List[Dict],
        failure_proxy_ids: List[int],
        validation_window_minutes: int,
    ):
        """
        Commit validation accounting and results in one transaction.
        """
        success_proxies = sorted(success_proxies, key=lambda row: int(row["id"]))
        failure_proxy_ids = sorted(set(int(proxy_id) for proxy_id in failure_proxy_ids))
        proxy_ids = sorted(
            {int(row["id"]) for row in success_proxies} | set(failure_proxy_ids)
        )

        def write(conn):
            with conn.cursor() as cur:
                if proxy_ids:
                    cur.execute(
                        "SELECT id FROM proxies WHERE id = ANY(%s) "
                        "ORDER BY id FOR UPDATE;",
                        (proxy_ids,),
                    )
                    cur.execute(
                        """
                        UPDATE proxies
                        SET
                            validation_attempts_in_window = CASE
                                WHEN window_start_time IS NULL
                                  OR NOW() > window_start_time
                                    + INTERVAL '%(window)s minutes'
                                THEN 1
                                ELSE validation_attempts_in_window + 1
                            END,
                            window_start_time = CASE
                                WHEN window_start_time IS NULL
                                  OR NOW() > window_start_time
                                    + INTERVAL '%(window)s minutes'
                                THEN NOW()
                                ELSE window_start_time
                            END
                        WHERE id = ANY(%(ids)s);
                        """,
                        {
                            "window": validation_window_minutes,
                            "ids": proxy_ids,
                        },
                    )
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

        return self._run_transaction("batch_update_proxy_results", write)

    def get_active_proxies(self) -> Optional[Set[str]]:
        query = "SELECT protocol, ip, port FROM proxies WHERE is_active = true;"
        rows = self._execute(query, fetch="all")
        if rows is None:
            return None
        return {f"{row['protocol']}://{row['ip']}:{row['port']}" for row in rows}

    def get_source_backoff_states(self) -> Optional[Dict[str, Dict]]:
        rows = self._execute(
            """
            SELECT source_name, failure_count,
                   EXTRACT(EPOCH FROM next_attempt_at) AS next_attempt_at,
                   failure_class
            FROM proxy_source_fetch_state
            ORDER BY source_name;
            """,
            fetch="all",
        )
        if rows is None:
            return None
        return {
            row["source_name"]: {
                "failure_count": int(row["failure_count"]),
                "next_attempt_at": float(row["next_attempt_at"]),
                "failure_class": row["failure_class"],
            }
            for row in rows
        }

    def upsert_source_backoff(
        self, source_name: str, failure_count: int, next_attempt_at: float,
        failure_class: str
    ) -> bool:
        return self._execute(
            """
            INSERT INTO proxy_source_fetch_state (
                source_name, failure_count, next_attempt_at, failure_class
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT (source_name) DO UPDATE SET
                failure_count = EXCLUDED.failure_count,
                next_attempt_at = EXCLUDED.next_attempt_at,
                failure_class = EXCLUDED.failure_class;
            """,
            (
                source_name,
                failure_count,
                datetime.fromtimestamp(next_attempt_at, tz=timezone.utc),
                failure_class,
            ),
            operation="upsert_source_backoff",
        )

    def clear_source_backoff(self, source_name: str) -> bool:
        return self._execute(
            "DELETE FROM proxy_source_fetch_state WHERE source_name = %s;",
            (source_name,),
            operation="clear_source_backoff",
        )

    def flush_feedback_stats(
        self,
        stats_buffer: List[Tuple],
        flush_id: Optional[str] = None,
        deadline: Optional[float] = None,
    ) -> bool:
        if not stats_buffer:
            return True
        query = """
            INSERT INTO source_stats_by_minute (minute, source_name, success_count, failure_count)
            VALUES %s
            ON CONFLICT (minute, source_name) DO UPDATE SET
                success_count = source_stats_by_minute.success_count + EXCLUDED.success_count,
                failure_count = source_stats_by_minute.failure_count + EXCLUDED.failure_count;
        """
        ordered = sorted(stats_buffer, key=lambda row: (row[0], row[1]))
        flush_id = flush_id or str(uuid.uuid4())

        def write(conn):
            with conn.cursor() as cur:
                def set_remaining_statement_timeout():
                    if deadline is None:
                        return
                    remaining_ms = int((deadline - time.monotonic()) * 1000)
                    if remaining_ms <= 0:
                        raise TimeoutError(
                            "feedback flush shutdown deadline exhausted"
                        )
                    cur.execute(
                        "SET LOCAL statement_timeout = %s;",
                        (remaining_ms,),
                    )

                set_remaining_statement_timeout()
                cur.execute(
                    """
                    INSERT INTO feedback_flush_commits (flush_id)
                    VALUES (%s::uuid)
                    ON CONFLICT (flush_id) DO NOTHING
                    RETURNING flush_id;
                    """,
                    (flush_id,),
                )
                if cur.fetchone() is None:
                    return
                # execute_values may otherwise emit several SQL statements
                # under one stale timeout. Bound each explicit page by the
                # remaining shutdown budget instead.
                page_size = 100
                for offset in range(0, len(ordered), page_size):
                    set_remaining_statement_timeout()
                    page = ordered[offset : offset + page_size]
                    psycopg2.extras.execute_values(
                        cur,
                        query,
                        page,
                        page_size=len(page),
                    )
                set_remaining_statement_timeout()
                cur.execute(
                    "DELETE FROM feedback_flush_commits "
                    "WHERE committed_at < NOW() - INTERVAL '7 days';"
                )
            flushed_minutes = sorted(
                list({item[0].strftime("%H:%M") for item in ordered})
            )
            logger.info(
                f"Flushed stats for {len(ordered)} source-minute combination(s). Minutes: {flushed_minutes}"
            )

        return self._run_transaction(
            "flush_feedback_stats",
            write,
            max_attempts=1 if deadline is not None else None,
        )

    def get_daily_stats(self, source: str, date: str):
        # Range predicate rather than DATE(minute) = %s: the function call is
        # not indexable, so the old form forced a bitmap heap scan and filtered
        # every row of the source. Semantics are unchanged -- both forms resolve
        # the date boundary in the session TimeZone.
        query = """
            SELECT COALESCE(SUM(success_count), 0) as total_success,
                   COALESCE(SUM(failure_count), 0) as total_failure
            FROM source_stats_by_minute
            WHERE source_name = %s
              AND minute >= %s::date AND minute < %s::date + 1;
        """
        return self._execute(query, (source, date, date), fetch="one")

    def get_timeseries_stats(self, source: str, date: str, interval_minutes: int):
        query = """
            SELECT
                date_trunc('hour', minute) + (EXTRACT(minute FROM minute)::int / %(interval)s * %(interval)s) * interval '1 minute' AS interval_start,
                SUM(success_count) as success,
                SUM(failure_count) as failure
            FROM source_stats_by_minute
            WHERE source_name = %(source)s
              AND minute >= %(date)s::date AND minute < %(date)s::date + 1
            GROUP BY interval_start ORDER BY interval_start;
        """
        return self._execute(
            query,
            {"source": source, "date": date, "interval": interval_minutes},
            fetch="all",
        )

    def get_overview_stats(self, date: str, interval_minutes: int):
        daily_query = """
            SELECT
                source_name,
                COALESCE(SUM(success_count), 0) as total_success,
                COALESCE(SUM(failure_count), 0) as total_failure
            FROM source_stats_by_minute
            WHERE minute >= %s::date AND minute < %s::date + 1
            GROUP BY source_name
            ORDER BY source_name;
        """
        timeseries_query = """
            SELECT
                source_name,
                date_trunc('hour', minute) + (EXTRACT(minute FROM minute)::int / %(interval)s * %(interval)s) * interval '1 minute' AS interval_start,
                SUM(success_count) as success,
                SUM(failure_count) as failure
            FROM source_stats_by_minute
            WHERE minute >= %(date)s::date AND minute < %(date)s::date + 1
            GROUP BY source_name, interval_start
            ORDER BY source_name, interval_start;
        """
        daily_rows = self._execute(daily_query, (date, date), fetch="all")
        if daily_rows is None:
            return None

        timeseries_rows = self._execute(
            timeseries_query,
            {"date": date, "interval": interval_minutes},
            fetch="all",
        )
        if timeseries_rows is None:
            return None

        return {"daily": daily_rows, "timeseries": timeseries_rows}

    def get_distinct_sources(self) -> Optional[List[str]]:
        query = "SELECT DISTINCT source_name FROM source_stats_by_minute ORDER BY source_name;"
        rows = self._execute(query, fetch="all")
        if rows is None:
            return None
        return [row["source_name"] for row in rows]
