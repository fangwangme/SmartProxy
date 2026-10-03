import asyncio
import copy
import importlib
import json
import os
import sys
import threading
import time
import unittest
import socket
from concurrent.futures import Future
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg2
import aiohttp

from src.api.server import create_app
from src.database.db import DatabaseManager, DatabaseWriteError
from tests.test_smart_proxy import ProxyManagerTestBase, write_config_file


class ValidationOutageContractTests(ProxyManagerTestBase):
    def test_target_failure_classes_are_stable(self):
        http_error = aiohttp.ClientResponseError(
            MagicMock(), (), status=503
        )
        connection_error = aiohttp.ClientConnectionError("injected")
        dns_error = aiohttp.ClientConnectorError(
            MagicMock(), socket.gaierror("injected")
        )
        cases = [
            (http_error, "http_status"),
            (asyncio.TimeoutError(), "timeout"),
            (connection_error, "connection"),
            (dns_error, "dns"),
            (ValueError("injected"), "malformed_response"),
        ]

        for error, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    self.manager._validation_failure_kind(error), expected
                )

    def _batch(self, results, targets, threshold):
        self.manager.validation_targets = targets
        self.manager.validation_success_threshold = threshold
        self.manager.validation_target_min_samples = 1
        proxies = [
            {"id": index + 1, "protocol": "http", "ip": f"192.0.2.{index + 1}", "port": 80}
            for index in range(len(results))
        ]
        with patch.object(
            self.manager, "_validate_proxy_async", AsyncMock(side_effect=results)
        ):
            return asyncio.run(self.manager._validate_proxies_batch_async(proxies))

    def test_failed_sole_target_fails_safe(self):
        successes, failures, metadata = self._batch(
            [
                {
                    "id": 1,
                    "success": False,
                    "target_results": [
                        {"target_index": 0, "success": False, "failure_kind": "timeout"}
                    ],
                }
            ],
            ["https://validation.invalid/echo"],
            1,
        )

        self.assertEqual((successes, failures), ([], [1]))
        self.assertFalse(metadata["quorum_healthy"])
        self.assertEqual(metadata["healthy_targets"], [False])

    def test_partial_multi_target_outage_can_retain_a_healthy_quorum(self):
        target_results = [
            {"target_index": 0, "success": True},
            {"target_index": 1, "success": True},
            {"target_index": 2, "success": False, "failure_kind": "http_status"},
        ]
        successes, failures, metadata = self._batch(
            [
                {
                    "id": 1,
                    "success": True,
                    "latency": 10,
                    "anonymity": "elite",
                    "target_results": target_results,
                }
            ],
            ["https://one.invalid", "https://two.invalid", "https://three.invalid"],
            2,
        )

        self.assertEqual([row["id"] for row in successes], [1])
        self.assertEqual(failures, [])
        self.assertTrue(metadata["quorum_healthy"])
        self.assertEqual(metadata["healthy_targets"], [True, True, False])

    def test_all_targets_healthy(self):
        successes, failures, metadata = self._batch(
            [
                {
                    "id": 1,
                    "success": True,
                    "latency": 10,
                    "anonymity": "elite",
                    "target_results": [
                        {"target_index": 0, "success": True},
                        {"target_index": 1, "success": True},
                    ],
                }
            ],
            ["https://one.invalid", "https://two.invalid"],
            2,
        )

        self.assertTrue(metadata["quorum_healthy"])
        self.assertEqual(metadata["healthy_targets"], [True, True])
        self.assertEqual(([row["id"] for row in successes], failures), ([1], []))

    def test_an_all_failure_batch_is_recorded_like_any_other(self):
        """
        Every completed check is written, however the batch as a whole went.

        Withholding the results of a batch in which nothing passed - on the
        theory that the validation target must be down - left the timestamps
        and attempt counts of the oldest rows untouched, so the oldest-first
        queues handed the same rows to every following cycle: a batch of dead
        proxies at the head of the queue kept every row behind it from ever
        being checked, and a live proxy that died stayed active. Serving does
        not depend on is_active any more, so an unreachable target costs
        ranking signal - until the failed-proxy queue reaches those rows
        again - never supply.
        """
        batch = [
            {"id": 1, "protocol": "http", "ip": "192.0.2.11", "port": 80},
            {"id": 2, "protocol": "http", "ip": "192.0.2.10", "port": 80},
        ]
        self.manager.validation_supplement_threshold = 0

        with (
            patch.object(self.manager, "_collect_validation_batch", return_value=batch),
            patch.object(
                self.manager,
                "_validate_proxies_batch_async",
                AsyncMock(
                    return_value=(
                        [],
                        [1, 2],
                        {"quorum_healthy": False, "healthy_targets": [False]},
                    )
                ),
            ),
            patch.object(self.manager, "_sync_and_select_top_proxies") as sync,
        ):
            self.manager._run_validation_cycle()

        self.mock_db_instance.batch_update_proxy_results.assert_called_once_with(
            [], [1, 2], self.manager.validation_window_minutes
        )
        sync.assert_called_once()
        self.assertIsNotNone(self.manager.last_validation_success_ts)
        self.assertFalse(self.manager.last_validation_quorum_healthy)
        self.assertFalse(self.manager.is_validating)

        self.mock_db_instance.batch_update_proxy_results.reset_mock()
        recovered = [
            {"id": 1, "latency": 11, "anonymity": "elite"},
            {"id": 2, "latency": 12, "anonymity": "elite"},
        ]
        with (
            patch.object(self.manager, "_collect_validation_batch", return_value=batch),
            patch.object(
                self.manager,
                "_validate_proxies_batch_async",
                AsyncMock(
                    return_value=(
                        recovered,
                        [],
                        {"quorum_healthy": True, "healthy_targets": [True]},
                    )
                ),
            ),
            patch.object(self.manager, "_sync_and_select_top_proxies") as sync,
        ):
            self.manager._run_validation_cycle()

        self.mock_db_instance.batch_update_proxy_results.assert_called_once_with(
            recovered, [], self.manager.validation_window_minutes
        )
        sync.assert_called_once()
        self.assertIsNotNone(self.manager.last_validation_success_ts)

    def test_a_task_that_ends_without_a_verdict_is_not_recorded(self):
        """
        Only a finished check is a result. A task that raised - cancelled, or
        broken past the per-proxy handlers - stays queued for a later cycle
        instead of being written as a failure it never measured.
        """
        finished = {
            "id": 1,
            "success": True,
            "latency": 10,
            "anonymity": "elite",
            "target_results": [{"target_index": 0, "success": True}],
        }
        for unfinished in (RuntimeError("injected"), asyncio.CancelledError()):
            with self.subTest(unfinished=type(unfinished).__name__):
                successes, failures, metadata = self._batch(
                    [finished, unfinished],
                    ["https://validation.invalid/echo"],
                    1,
                )

                self.assertEqual([row["id"] for row in successes], [1])
                self.assertEqual(failures, [])
                self.assertTrue(metadata["quorum_healthy"])

    def test_a_batch_in_which_no_check_finished_is_not_reported_as_validated(self):
        """Nothing recorded is not a validation: readiness must not count it."""
        batch = [{"id": 1, "protocol": "http", "ip": "192.0.2.12", "port": 80}]
        self.manager.validation_supplement_threshold = 0

        with (
            patch.object(self.manager, "_collect_validation_batch", return_value=batch),
            patch.object(
                self.manager,
                "_validate_proxy_async",
                AsyncMock(side_effect=asyncio.CancelledError()),
            ),
            patch.object(self.manager, "_sync_and_select_top_proxies"),
        ):
            self.manager._run_validation_cycle()

        self.mock_db_instance.batch_update_proxy_results.assert_not_called()
        self.assertIsNone(self.manager.last_validation_success_ts)
        self.assertFalse(self.manager.is_validating)

    def test_a_reload_mid_batch_applies_from_the_next_batch(self):
        """
        The requests and the summary of their results share one config copy.

        A reload that shortened the target list while tasks were still
        reporting results against the old one raised IndexError in the
        summary, and the whole batch went unrecorded.
        """
        two_targets = {
            "validator": {
                "validation_targets": "https://one.invalid/get,https://two.invalid/get",
                "validation_success_threshold": "2",
            }
        }
        manager = self.make_manager(two_targets, name="two-targets.ini")
        one_target = {
            section: dict(options) for section, options in self.config_dict.items()
        }
        one_target["validator"]["validation_targets"] = "https://one.invalid/get"
        both_passed = [
            {"target_index": 0, "success": True},
            {"target_index": 1, "success": True},
        ]

        async def validate_while_reloading(session, proxy_id, proxy_url, semaphore, settings):
            write_config_file(self.tmp_dir, one_target, name="two-targets.ini")
            manager.reload_sources()
            return {
                "id": proxy_id,
                "success": True,
                "latency": 10,
                "anonymity": "elite",
                "target_results": both_passed,
            }

        proxies = [{"id": 1, "protocol": "http", "ip": "192.0.2.5", "port": 80}]
        with patch.object(
            manager, "_validate_proxy_async", side_effect=validate_while_reloading
        ):
            successes, failures, metadata = asyncio.run(
                manager._validate_proxies_batch_async(proxies)
            )

        self.assertEqual(([row["id"] for row in successes], failures), ([1], []))
        self.assertEqual(metadata["successes_by_target"], [1, 1])
        self.assertEqual(manager.validation_targets, ["https://one.invalid/get"])
        self.assertEqual(
            manager._validation_settings().targets, ("https://one.invalid/get",)
        )

    def test_failed_validation_write_is_not_reported_as_success(self):
        batch = [{"id": 1, "protocol": "http", "ip": "192.0.2.20", "port": 80}]
        self.manager.validation_supplement_threshold = 0
        self.mock_db_instance.batch_update_proxy_results.side_effect = DatabaseWriteError(
            "batch_update_proxy_results", RuntimeError("injected")
        )
        with (
            patch.object(self.manager, "_collect_validation_batch", return_value=batch),
            patch.object(
                self.manager,
                "_validate_proxies_batch_async",
                AsyncMock(
                    return_value=(
                        [{"id": 1, "latency": 10, "anonymity": "elite"}],
                        [],
                        {"quorum_healthy": True, "healthy_targets": [True]},
                    )
                ),
            ),
            patch.object(self.manager, "_sync_and_select_top_proxies") as sync,
        ):
            with self.assertRaises(DatabaseWriteError):
                self.manager._run_validation_cycle()

        sync.assert_not_called()
        self.assertIsNone(self.manager.last_validation_success_ts)
        self.assertFalse(self.manager.is_validating)


class TestHandoutAccountingAndPools(ProxyManagerTestBase):
    def _install_proven(self, source="source1", proxy="http://192.0.2.30:80", quality=0.9):
        now = time.time()
        stat = self.manager._get_new_proxy_stat(source) | {
            "score": quality * 100,
            "quality_slow": quality,
            "quality_fast": quality,
            "quality_updated_ts": now,
            "recent_results": [[now, True, None]] * 3,
            "success_count": 3,
        }
        self.manager.source_stats[source][proxy] = stat
        self.manager.active_proxies.add(proxy)
        self.manager.available_proxies[source] = {
            "top_tier": [proxy],
            "bottom_tier": [],
        }
        with self.manager.lock:
            self.manager._rebuild_candidate_pool(source)
        return proxy, stat

    def _aggregate_total(self):
        return sum(
            counts["success"] + counts["failure"]
            for by_source in self.manager.feedback_buffer.values()
            for counts in by_source.values()
        )

    def test_feedback_closes_one_handout_and_extra_reports_are_counted(self):
        proxy, stat = self._install_proven()
        self.manager.allocate_proxy("source1")
        self.manager.allocate_proxy("source1")
        self.assertEqual(stat["handout_count"], 2)
        self.assertEqual(len(stat["inflight"]), 2)

        self.manager.process_feedback("source1", proxy, 100)
        self.manager.process_feedback("source1", proxy, 100)

        self.assertEqual(len(stat["inflight"]), 0)
        self.assertEqual(self.manager.unmatched_feedback_total, 0)

        before = (
            stat["success_count"],
            self._aggregate_total(),
            self.manager.accepted_feedback_success_total,
        )
        # A third report has no handout behind it. The service is a neutral
        # referee, so it is still scored - but it is counted, because a
        # duplicate, a late report or a wrong source is the only way to
        # produce one.
        self.manager.process_feedback("source1", proxy, 100)

        self.assertEqual(self.manager.unmatched_feedback_total, 1)
        self.assertEqual(stat["success_count"], before[0] + 1)
        self.assertEqual(self._aggregate_total(), before[1] + 1)
        self.assertEqual(
            self.manager.accepted_feedback_success_total, before[2] + 1
        )

    def test_expired_leases_are_pruned_before_a_release(self):
        now = time.time()
        expired_at = now - 1
        live_until = now + 60
        stat = {"inflight": [expired_at, live_until]}

        # The pruning pass drops the expired slot, so the release that follows
        # frees the live one rather than an already-reclaimed slot.
        self.assertEqual(self.manager._lease_count(stat, now), 1)
        self.manager._release_lease(stat)

        self.assertEqual(stat["inflight"], [])

    def test_a_proxy_that_stops_scoring_well_is_still_served(self):
        """
        Issue #27: the pool re-ranks, it does not evict.

        This replaces three tests that asserted the opposite - that a proxy
        dropping below the prior was tombstoned out of the exploit set, demoted
        into a trial group, or held out by a cooldown. Each of those could empty
        the servable set, which is the defect this issue removes.
        """
        proxy, stat = self._install_proven()
        stat.update(
            {
                "score": 1.0,
                "quality_slow": 0.01,
                "quality_fast": 0.01,
                "recent_results": [[time.time(), False, None]],
            }
        )
        with self.manager.lock:
            self.manager._rebuild_candidate_pool("source1")

        handout = self.manager.allocate_proxy("source1")

        self.assertEqual(handout["proxy"], proxy)
        self.assertIn(proxy, self.manager.candidate_pools["source1"])

    def test_premium_uses_the_source_contract_and_demotes_immediately(self):
        proxy, stat = self._install_proven(quality=0.06)
        self.manager.premium_min_usage_count = 3
        self.manager._sync_premium_proxies_locked()

        handout = self.manager.allocate_premium_proxy()
        # The premium endpoint reports the pool the proxy is scored under, so
        # feedback for it lands in the right ledger instead of a guess.
        self.assertEqual(handout["source"], "source1")
        self.assertEqual(len(self.manager.source_stats["source1"][proxy]["inflight"]), 1)
        with patch.object(self.manager, "_sync_premium_proxies_locked") as sync:
            self.manager.process_feedback("source1", proxy, 4)

        sync.assert_not_called()
        self.assertFalse(self.manager._is_premium_grade(stat, "source1"))
        self.assertNotIn(proxy, self.manager.premium_proxies)
        self.assertIsNone(self.manager.allocate_premium_proxy())

    def test_a_stale_pool_serves_without_rebuilding_on_the_request_thread(self):
        """
        Staleness costs ranking accuracy, never availability.

        The refresh timer owns rebuilds. Doing one on the request path would
        put pool-sized work behind the manager lock on every handout, and a
        request that arrives while the pool is stale still has a pool.
        """
        proxy, _ = self._install_proven()
        self.manager.pool_refresh_seconds = 1.0
        self.manager.candidate_pool_built_at["source1"] = time.time() - 3600

        with patch.object(self.manager, "_rebuild_candidate_pool") as rebuild:
            first = self.manager.allocate_proxy("source1")
            second = self.manager.allocate_proxy("source1")

        self.assertEqual((first["proxy"], second["proxy"]), (proxy, proxy))
        rebuild.assert_not_called()


class TestPersistenceAndTransactions(ProxyManagerTestBase):
    def test_old_backup_diagnostics_are_accepted_but_not_reserialized(self):
        proxy = "http://192.0.2.41:80"
        backup_path = Path(self.tmp_dir) / "legacy-extra-fields.json"
        backup_path.write_text(
            json.dumps(
                {
                    "scoring_version": 2,
                    "timestamp": datetime.now().astimezone().isoformat(),
                    "source_stats": {
                        "source1": {
                            proxy: {
                                "score": 80.0,
                                "quality_slow": 0.8,
                                "quality_fast": 0.8,
                                "quality_updated_ts": time.time(),
                                "success_count": 3,
                                "failure_count": 0,
                                "completed_feedback_count": 3,
                                "consecutive_failures": 0,
                                "recent_results": [[time.time(), True, None]] * 3,
                                "handout_count": 3,
                                "trial_handout_count": 0,
                                "retry_after_ts": 1234.0,
                                "inflight": [],
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.manager.stats_backup_path = backup_path

        self.assertEqual(self.manager.restore_stats()["status"], "success")
        restored = self.manager.source_stats["source1"][proxy]
        self.assertNotIn("completed_feedback_count", restored)
        self.assertNotIn("consecutive_failures", restored)
        # The #27 gate fields are now in the same class: a snapshot written by
        # an older build still loads, and its gate bookkeeping is dropped
        # rather than rejected.
        self.assertNotIn("trial_handout_count", restored)
        self.assertNotIn("retry_after_ts", restored)
        self.assertEqual(self.manager.backup_stats()["status"], "success")
        serialized = backup_path.read_text(encoding="utf-8")
        self.assertNotIn("completed_feedback_count", serialized)
        self.assertNotIn("consecutive_failures", serialized)
        self.assertNotIn("trial_handout_count", serialized)
        self.assertNotIn("retry_after_ts", serialized)

    def test_fetch_backoff_is_restored_and_success_clears_durable_state(self):
        next_attempt = time.time() + 120
        self.mock_db_instance.get_source_backoff_states.return_value = {
            "proxy_source_A": {
                "failure_count": 4,
                "next_attempt_at": next_attempt,
                "failure_class": "persistent",
            }
        }
        jobs = self.manager._load_fetcher_jobs()
        job = next(item for item in jobs if item["name"] == "proxy_source_A")
        self.assertEqual(job["failure_count"], 4)
        self.assertAlmostEqual(
            job["last_run"] + job["interval_minutes"] * 60,
            next_attempt,
            places=3,
        )

        with patch.object(
            self.manager, "_fetch_source_text", return_value="192.0.2.42:80"
        ):
            self.manager._fetch_and_parse_source(job)
        self.mock_db_instance.clear_source_backoff.assert_called_once_with(
            "proxy_source_A"
        )

    def test_failed_flush_requeues_and_concurrent_increment_survives_commit(self):
        minute = datetime.now().replace(second=0, microsecond=0) - timedelta(minutes=1)
        self.manager.feedback_buffer[minute]["source1"]["success"] = 2
        self.mock_db_instance.flush_feedback_stats.return_value = False

        self.assertFalse(self.manager._flush_feedback_buffer())
        self.assertEqual(self.manager.feedback_buffer[minute]["source1"]["success"], 2)

        def commit_with_concurrent_feedback(_records, _flush_id):
            with self.manager.lock:
                self.manager.feedback_buffer[minute]["source1"]["success"] += 1
            return True

        self.mock_db_instance.flush_feedback_stats.side_effect = commit_with_concurrent_feedback
        self.assertTrue(self.manager._flush_feedback_buffer())
        self.assertEqual(self.manager.feedback_buffer[minute]["source1"]["success"], 1)
        first_flush_id = self.mock_db_instance.flush_feedback_stats.call_args_list[0].args[1]
        second_flush_id = self.mock_db_instance.flush_feedback_stats.call_args_list[1].args[1]
        self.assertEqual(first_flush_id, second_flush_id)

    def test_current_minute_waits_for_shutdown_flush(self):
        minute = datetime.now().replace(second=0, microsecond=0)
        self.manager.feedback_buffer[minute]["source1"]["failure"] = 1

        self.assertTrue(self.manager._flush_feedback_buffer())
        self.mock_db_instance.flush_feedback_stats.assert_not_called()
        self.assertIn(minute, self.manager.feedback_buffer)

        self.assertTrue(self.manager._flush_feedback_buffer(include_current=True))
        self.mock_db_instance.flush_feedback_stats.assert_called_once()
        self.assertNotIn(minute, self.manager.feedback_buffer)

    def test_flush_retry_uses_one_ledger_id_and_applies_aggregate_once(self):
        db = object.__new__(DatabaseManager)
        cursor = MagicMock()
        cursor.fetchone.side_effect = [("committed",), None]
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor

        def retry_once(_operation, callback, **_kwargs):
            callback(connection)
            callback(connection)
            return True

        db._run_transaction = MagicMock(side_effect=retry_once)
        minute = datetime(2026, 1, 1, 0, 0)
        records = [(minute, "source-b", 1, 0), (minute, "source-a", 2, 1)]
        with patch("src.database.db.psycopg2.extras.execute_values") as execute_values:
            self.assertTrue(db.flush_feedback_stats(records))

        execute_values.assert_called_once()
        self.assertEqual(execute_values.call_args.args[2], list(reversed(records)))
        ledger_calls = [
            invocation
            for invocation in cursor.execute.call_args_list
            if "INSERT INTO feedback_flush_commits" in invocation.args[0]
        ]
        self.assertEqual(len(ledger_calls), 2)
        self.assertEqual(ledger_calls[0].args[1], ledger_calls[1].args[1])

    def test_shutdown_writes_backup_before_flushing_current_minute(self):
        """
        Reputation is the only durable state without a second copy, and a
        stalled database is the case where the flush burns the budget and
        fails anyway while the local backup would still have landed.
        """
        self.manager.stats_backup_enabled = True
        events = []
        original_flush = self.manager._flush_stats

        def record_flush(include_current=False, deadline=None):
            events.append(("flush", include_current, self.manager.accepting_background_tasks))
            return True

        self.manager.fetch_executor = MagicMock()
        self.manager.background_executor = MagicMock()
        with (
            patch.object(self.manager, "_flush_stats", side_effect=record_flush),
            patch.object(
                self.manager,
                "backup_stats",
                side_effect=lambda deadline=None: (
                    events.append(("backup",)) or {"status": "success"}
                ),
            ),
        ):
            self.manager.stop_scheduler()

        self.assertIsNotNone(original_flush)
        self.assertEqual(events, [("backup",), ("flush", True, False)])
        self.manager.fetch_executor.shutdown.assert_called_once_with(
            wait=False, cancel_futures=True
        )
        self.manager.background_executor.shutdown.assert_called_once_with(
            wait=False, cancel_futures=True
        )

    def test_shutdown_wait_is_bounded_and_unfinished_work_is_cancelled(self):
        unfinished = MagicMock()
        self.manager.background_futures = {unfinished}
        self.manager.shutdown_deadline_s = 2.0
        self.manager.fetch_executor = MagicMock()
        self.manager.background_executor = MagicMock()
        with (
            patch("src.core.proxy_manager.wait", return_value=(set(), {unfinished})) as wait_for_work,
            patch.object(self.manager, "_flush_stats"),
        ):
            self.manager.stop_scheduler()

        timeout = wait_for_work.call_args.kwargs["timeout"]
        self.assertGreaterEqual(timeout, 0.0)
        self.assertLessEqual(timeout, 1.0)
        unfinished.cancel.assert_called_once()

    def test_shutdown_flush_does_not_wait_past_its_deadline_for_the_lock(self):
        self.manager.feedback_flush_lock.acquire()
        try:
            started = time.monotonic()
            flushed = self.manager._flush_feedback_buffer(
                include_current=True,
                deadline=started + 0.01,
            )
        finally:
            self.manager.feedback_flush_lock.release()

        self.assertFalse(flushed)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_shutdown_flush_passes_the_absolute_database_deadline(self):
        minute = datetime.now().replace(second=0, microsecond=0)
        self.manager.feedback_buffer[minute]["source1"]["success"] = 1
        deadline = time.monotonic() + 1.0

        self.assertTrue(
            self.manager._flush_feedback_buffer(
                include_current=True,
                deadline=deadline,
            )
        )

        self.assertEqual(
            self.mock_db_instance.flush_feedback_stats.call_args.kwargs["deadline"],
            deadline,
        )

    def test_shutdown_flush_recomputes_sql_budget_and_disables_retries(self):
        db = object.__new__(DatabaseManager)
        cursor = MagicMock()
        cursor.fetchone.return_value = ("committed",)
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor

        def run_once(_operation, callback, max_attempts=None):
            self.assertEqual(max_attempts, 1)
            callback(connection)
            return True

        db._run_transaction = MagicMock(side_effect=run_once)
        minute = datetime(2026, 1, 1, 0, 0)
        records = [(minute, f"source-{index}", 1, 0) for index in range(101)]
        with (
            patch("src.database.db.time.monotonic", side_effect=[1.0, 2.0, 3.0, 4.0]),
            patch("src.database.db.psycopg2.extras.execute_values") as execute_values,
        ):
            self.assertTrue(db.flush_feedback_stats(records, deadline=10.0))

        timeout_calls = [
            invocation
            for invocation in cursor.execute.call_args_list
            if invocation.args[0] == "SET LOCAL statement_timeout = %s;"
        ]
        self.assertEqual(
            [invocation.args[1][0] for invocation in timeout_calls],
            [9000, 8000, 7000, 6000],
        )
        self.assertEqual(execute_values.call_count, 2)

    def test_overlapping_writers_use_deterministic_order(self):
        db = object.__new__(DatabaseManager)
        db.pool = MagicMock()
        connection = MagicMock()
        cursor = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        db.pool.getconn.return_value = connection
        db.write_max_retries = 0
        db.write_retry_base_ms = 0

        successes = [
            {"id": 3, "latency": 30, "anonymity": "elite"},
            {"id": 1, "latency": 10, "anonymity": "elite"},
        ]
        with patch("src.database.db.psycopg2.extras.execute_values") as execute_values:
            db.batch_update_proxy_results(successes, [5, 2, 5], 30)
        counter_call = next(
            invocation
            for invocation in cursor.execute.call_args_list
            if "validation_attempts_in_window" in invocation.args[0]
        )
        self.assertEqual(counter_call.args[1]["ids"], [1, 2, 3, 5])
        self.assertEqual([row[0] for row in execute_values.call_args.args[2]], [1, 3])
        failure_call = next(
            invocation
            for invocation in cursor.execute.call_args_list
            if "is_active = false" in invocation.args[0]
        )
        self.assertEqual(failure_call.args[1], ([2, 5],))

    def test_retryable_transactions_are_bounded_and_deadlocks_are_retried(self):
        class Deadlock(psycopg2.Error):
            @property
            def pgcode(self):
                return "40P01"

        db = object.__new__(DatabaseManager)
        db.pool = MagicMock()
        db.pool.getconn.return_value = MagicMock()
        db.write_max_retries = 2
        db.write_retry_base_ms = 0
        attempts = 0

        def succeeds_on_third(_connection):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise Deadlock("injected")

        def assert_connection_was_returned(_delay):
            self.assertEqual(db.pool.putconn.call_count, attempts)

        with patch(
            "src.database.db.time.sleep",
            side_effect=assert_connection_was_returned,
        ) as sleep:
            self.assertTrue(db._run_transaction("ordered-write", succeeds_on_third))
        self.assertEqual(attempts, 3)
        self.assertEqual(db.pool.getconn.return_value.rollback.call_count, 2)
        self.assertEqual(sleep.call_count, 2)

        attempts = 0
        db.write_max_retries = 1

        def always_deadlocks(_connection):
            nonlocal attempts
            attempts += 1
            raise Deadlock("injected")

        with patch("src.database.db.time.sleep"):
            with self.assertRaises(DatabaseWriteError):
                db._run_transaction("ordered-write", always_deadlocks)
        self.assertEqual(attempts, 2)

class TestConfigurationBoundaries(ProxyManagerTestBase):
    def test_invalid_startup_values_are_rejected_before_database_creation(self):
        invalid_cases = [
            ({"server": {"port": "0"}}, "server port"),
            ({"server": {"production_threads": "0"}}, "production workers"),
            ({"server": {"shutdown_deadline_seconds": "0"}}, "shutdown deadline"),
            ({"server": {"readiness_validation_max_age_seconds": "0"}}, "validation age"),
            ({"server": {"readiness_flush_max_age_seconds": "0"}}, "flush age"),
            ({"server": {"background_workers": "0"}}, "background workers"),
            ({"server": {"connection_limit": "0"}}, "connection limit"),
            ({"server": {"allowed_ips": "not-an-address"}}, "allowed address"),
            ({"server": {"trusted_proxy_ips": "not-an-address"}}, "trusted address"),
            ({"database": {"min_connections": "0"}}, "database minimum"),
            ({"database": {"max_connections": "0"}}, "database maximum"),
            ({"database": {"min_connections": "6", "max_connections": "5"}}, "database bounds"),
            ({"database": {"write_max_retries": "-1"}}, "database retries"),
            ({"database": {"write_retry_base_ms": "-1"}}, "database retry delay"),
            ({"validator": {"validation_workers": "0"}}, "validation workers"),
            ({"validator": {"validation_timeout_s": "0"}}, "validation timeout"),
            ({"validator": {"validation_targets": "https://one.invalid", "validation_success_threshold": "2"}}, "target threshold"),
            ({"validator": {"validation_targets": "https://same.invalid,https://same.invalid"}}, "target uniqueness"),
            ({"validator": {"validation_targets": "not-a-url"}}, "target URL"),
            ({"validator": {"validation_batch_limit": "0"}}, "batch limit"),
            ({"validator": {"validation_new_proxy_ratio": "-0.1"}}, "negative batch ratio"),
            ({"validator": {"validation_new_proxy_ratio": "1.1"}}, "batch ratio over 100 percent"),
            ({"validator": {"validation_supplement_threshold": "-1"}}, "negative supplement"),
            ({"validator": {"validation_batch_limit": "10", "validation_supplement_threshold": "11"}}, "supplement over batch"),
            ({"validator": {"validation_window_minutes": "0"}}, "validation window"),
            ({"validator": {"max_validations_per_window": "0"}}, "validation attempts"),
            ({"validator": {"validation_target_min_samples": "0"}}, "target samples"),
            ({"validator": {"validation_batch_limit": "2", "validation_supplement_threshold": "0", "validation_target_min_samples": "3"}}, "target samples over batch"),
            ({"scheduler": {"validation_interval_seconds": "0"}}, "validation interval"),
            ({"scheduler": {"stats_flush_interval_seconds": "0"}}, "flush interval"),
            ({"scheduler": {"source_refresh_interval_seconds": "0"}}, "source interval"),
            ({"sources": {"predefined_sources": ""}}, "empty sources"),
            ({"sources": {"predefined_sources": "x" * 51, "default_source": "default"}}, "source length"),
            ({"sources": {"default_source": "x" * 51}}, "default source length"),
            ({"source_pool": {"max_pool_size": "0"}}, "pool size"),
            ({"source_pool": {"stats_pool_max_multiplier": "0"}}, "pool multiplier"),
            ({"source_pool": {"top_tier_size": "-1"}}, "tier size"),
            ({"source_pool": {"max_pool_size": "10", "top_tier_size": "11"}}, "tier over pool"),
            ({"source_pool": {"top_tier_load_percentage": "101"}}, "percentage over 100"),
            ({"source_pool": {"proxy_inflight_timeout_seconds": "0"}}, "lease expiry"),
            ({"source_pool": {"candidate_pool_size": "0"}}, "candidate pool size"),
            ({"source_pool": {"pool_refresh_seconds": "0"}}, "pool refresh"),
            ({"source_pool": {"exploration_slots": "-1"}}, "exploration slots"),
            ({"source_pool": {"selection_weight_floor": "0"}}, "weight floor"),
            ({"source_pool": {"softmax_temperature": "0"}}, "temperature"),
            ({"source_pool": {"max_feedback_latency_ms": "0"}}, "latency bound"),
            ({"source_pool": {"premium_pool_size": "-1"}}, "premium pool"),
            ({"source_pool": {"premium_min_usage_count": "-1"}}, "premium evidence"),
            ({"source_pool": {"reliability_prior": "1.1"}}, "prior"),
            ({"source_pool": {"reliability_slow_alpha": "0"}}, "slow alpha"),
            ({"source_pool": {"reliability_slow_alpha": "0.5", "reliability_fast_alpha": "0.4"}}, "alpha ordering"),
            ({"source_pool": {"reliability_decay_half_life_hours": "0"}}, "half life"),
            ({"source_pool": {"reliability_recent_results_limit": "0"}}, "history limit"),
            ({"source_pool": {"outage_window_size": "0"}}, "outage window"),
            ({"source_pool": {"outage_window_size": "4", "outage_min_distinct_proxies": "5"}}, "outage distinct"),
            ({"source_pool": {"outage_healthy_baseline_ratio": "1.1"}}, "healthy ratio"),
            ({"source_pool": {"outage_failure_baseline_ratio": "1.1"}}, "failure ratio"),
            ({"source_pool": {"outage_recovery_baseline_ratio": "1.1"}}, "recovery ratio"),
            ({"source_pool": {"outage_failure_baseline_ratio": "0.6", "outage_recovery_baseline_ratio": "0.5"}}, "outage ratio ordering"),
            ({"source_pool": {"outage_baseline_alpha": "0"}}, "baseline alpha"),
            ({"source_pool": {"outage_false_positive_budget": "0.6"}}, "outage budget"),
            ({"source_pool": {"outage_window_size": "20", "outage_window_max_size": "19"}}, "outage maximum"),
            ({"fetcher": {"connect_timeout_s": "0"}}, "connect timeout"),
            ({"fetcher": {"total_timeout_s": "0"}}, "total timeout"),
            ({"fetcher": {"connect_timeout_s": "2", "total_timeout_s": "1"}}, "timeout ordering"),
            ({"fetcher": {"curl_retries": "-1"}}, "fetch retries"),
            ({"fetcher": {"curl_retry_delay_s": "-1"}}, "fetch retry delay"),
            ({"fetcher": {"backoff_base_s": "0"}}, "backoff base"),
            ({"fetcher": {"backoff_base_s": "30", "backoff_max_s": "20"}}, "backoff maximum"),
            ({"fetcher": {"backoff_base_s": "30", "backoff_max_s": "60", "backoff_transient_max_s": "61"}}, "transient backoff"),
            ({"proxy_source_A": {"update_interval_minutes": "0"}}, "source interval"),
            ({"backup": {"stats_backup_interval_seconds": "0"}}, "backup interval"),
        ]

        for index, (overrides, label) in enumerate(invalid_cases):
            with self.subTest(label=label):
                self.MockDatabaseManager.reset_mock()
                with self.assertRaises((ValueError, OverflowError)):
                    self.make_manager(overrides, name=f"invalid-{index}.ini")
                self.MockDatabaseManager.assert_not_called()

    def test_invalid_reload_rolls_back_all_active_values(self):
        old_workers = self.manager.validation_workers
        old_sources = set(self.manager.predefined_sources)
        invalid = {
            section: dict(values) for section, values in self.config_dict.items()
        }
        invalid["validator"]["validation_workers"] = "0"
        invalid["sources"]["predefined_sources"] = "changed"
        write_config_file(self.tmp_dir, invalid, name="config.ini")

        with self.assertRaises(ValueError):
            self.manager.reload_sources()

        self.assertEqual(self.manager.validation_workers, old_workers)
        self.assertEqual(self.manager.predefined_sources, old_sources)

    def test_restart_only_values_are_reported_but_not_applied_on_reload(self):
        old_values = (
            self.manager.server_port,
            self.manager.production_threads,
            self.manager.background_workers,
            self.manager.server_connection_limit,
            self.manager.proxy_inflight_timeout_s,
        )
        changed = {
            section: dict(values) for section, values in self.config_dict.items()
        }
        changed["server"].update(
            {
                "port": "7001",
                "production_threads": "12",
                "background_workers": "6",
                "connection_limit": "77",
            }
        )
        changed["source_pool"]["proxy_inflight_timeout_seconds"] = "15"
        write_config_file(self.tmp_dir, changed, name="config.ini")

        result = self.manager.reload_sources()

        self.assertEqual(
            (
                self.manager.server_port,
                self.manager.production_threads,
                self.manager.background_workers,
                self.manager.server_connection_limit,
                self.manager.proxy_inflight_timeout_s,
            ),
            old_values,
        )
        self.assertIn(
            "[server] production_threads / background_workers / connection_limit",
            result["restart_required_for"],
        )
        self.assertIn(
            "[source_pool] proxy_inflight_timeout_seconds",
            result["restart_required_for"],
        )

        changed["validator"]["validation_workers"] = "0"
        write_config_file(self.tmp_dir, changed, name="config.ini")
        with self.assertRaises(ValueError):
            self.manager.reload_sources()
        self.assertEqual(
            (
                self.manager.server_port,
                self.manager.production_threads,
                self.manager.background_workers,
                self.manager.server_connection_limit,
                self.manager.proxy_inflight_timeout_s,
            ),
            old_values,
        )

    def test_reload_reads_source_backoff_before_taking_the_manager_lock(self):
        self.mock_db_instance.get_source_backoff_states.reset_mock()

        def assert_unlocked():
            self.assertFalse(self.manager.lock._is_owned())
            return {}

        self.mock_db_instance.get_source_backoff_states.side_effect = assert_unlocked

        self.manager.reload_sources()

        self.mock_db_instance.get_source_backoff_states.assert_called_once()


class TestApiAndLifecycleContracts(ProxyManagerTestBase):
    def setUp(self):
        super().setUp()
        self.app = create_app(self.manager)
        self.client = self.app.test_client()

    def test_liveness_and_readiness_degrade_and_recover_with_stable_shapes(self):
        degraded = {
            "status": "not_ready",
            "ready": False,
            "dependencies": {
                "database": False,
                "scheduler": True,
                "validation": True,
                "feedback_flush": True,
                "usable_pool": True,
            },
            "usable_proxies": 1,
            "minimum_usable_proxies": 1,
        }
        ready = copy.deepcopy(degraded)
        ready.update({"status": "ready", "ready": True})
        ready["dependencies"]["database"] = True

        with patch.object(self.manager, "readiness_status", return_value=degraded):
            health = self.client.get("/health")
            readiness = self.client.get("/ready")
        live = self.client.get("/live")
        with patch.object(self.manager, "readiness_status", return_value=ready):
            recovered_health = self.client.get("/health")
            recovered_ready = self.client.get("/ready")

        self.assertEqual((health.status_code, readiness.status_code), (503, 503))
        self.assertEqual(health.get_json()["status"], "degraded")
        self.assertEqual(readiness.get_json(), degraded)
        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.get_json(), {"serving": True, "status": "live"})
        self.assertEqual((recovered_health.status_code, recovered_ready.status_code), (200, 200))
        self.assertEqual(recovered_health.get_json()["status"], "healthy")
        self.assertEqual(recovered_ready.get_json(), ready)

    def _serving_normally(self):
        """A manager whose scheduler runs, database answers and flushes land."""
        stop = threading.Event()
        self.manager.scheduler_thread = threading.Thread(target=stop.wait, daemon=True)
        self.manager.scheduler_thread.start()
        self.addCleanup(stop.set)
        self.mock_db_instance.ping.return_value = True
        self.manager.last_flush_success_ts = time.time()

    def test_readiness_waits_out_a_revalidation_window_without_failing(self):
        """
        Once every proxy has been checked, nothing is due again for a whole
        validation window, and an empty batch records nothing - honestly, it
        validated nothing. The freshness limit has to cover that wait; at 600s
        it reported a healthy, idle service unready for most of every window.
        """
        self._serving_normally()
        self.mock_db_instance.get_active_proxies.return_value = {"http://192.0.2.70:80"}
        self.manager._sync_and_select_top_proxies()
        recorded_at = time.time() - 20 * 60
        self.manager.last_validation_success_ts = recorded_at
        self.mock_db_instance.get_new_proxies_to_validate.return_value = []
        self.mock_db_instance.get_active_proxies_to_revalidate.return_value = []
        self.mock_db_instance.get_eligible_failed_proxies.return_value = []

        self.manager._run_validation_cycle()

        self.assertEqual(self.manager.last_validation_success_ts, recorded_at)
        self.assertTrue(self.manager.readiness_status()["ready"])
        self.manager.last_validation_success_ts = (
            time.time() - self.manager.readiness_validation_max_age_s - 1
        )
        self.assertFalse(self.manager.readiness_status()["dependencies"]["validation"])

    def test_readiness_counts_the_fallback_and_reports_quorum_as_a_diagnostic(self):
        """
        /get-proxy serves never-validated and failed rows when nothing is
        active, so a pool of them is a usable pool. What the last checks said
        is reported, not required; an empty table is still unready.
        """
        self._serving_normally()
        self.manager.last_validation_success_ts = time.time()
        self.manager.last_validation_quorum_healthy = False
        self.mock_db_instance.get_active_proxies.return_value = set()
        self.mock_db_instance.get_reserve_proxies.return_value = [
            f"http://192.0.2.{index}:80" for index in range(71, 76)
        ]
        self.manager._sync_and_select_top_proxies()

        status = self.manager.readiness_status()

        self.assertTrue(status["ready"], status)
        self.assertEqual(status["usable_proxies"], 5)
        self.assertEqual(status["active_proxies"], 0)
        self.assertFalse(status["validation_quorum_healthy"])

        self.mock_db_instance.get_reserve_proxies.return_value = []
        self.manager._sync_and_select_top_proxies()
        status = self.manager.readiness_status()
        self.assertFalse(status["dependencies"]["usable_pool"])
        self.assertIsNone(self.manager.allocate_proxy("source1"))

    def test_a_freshness_limit_shorter_than_the_revalidation_window_warns(self):
        with patch("src.core.proxy_manager.logger.warning") as warn:
            self.make_manager(
                {"server": {"readiness_validation_max_age_seconds": "600"}},
                name="short-readiness.ini",
            )
        self.assertIn("one revalidation window", str(warn.call_args_list))

        with patch("src.core.proxy_manager.logger.warning") as warn:
            self.make_manager({}, name="default-readiness.ini")
        self.assertNotIn("one revalidation window", str(warn.call_args_list))

    def test_stats_validate_before_database_and_backend_failures_are_503(self):
        invalid_requests = [
            ("/api/stats/daily?source=source1&date=bad", "get_daily_stats"),
            ("/api/stats/timeseries?source=source1&date=bad&interval=5", "get_timeseries_stats"),
            ("/api/stats/overview?date=bad&interval=5", "get_overview_stats"),
        ]
        for path, method in invalid_requests:
            with self.subTest(path=path):
                getattr(self.mock_db_instance, method).reset_mock()
                response = self.client.get(path)
                self.assertEqual(response.status_code, 400)
                getattr(self.mock_db_instance, method).assert_not_called()

        failures = [
            ("/api/stats/daily?source=source1&date=2026-01-01", "get_daily_stats"),
            ("/api/stats/timeseries?source=source1&date=2026-01-01&interval=5", "get_timeseries_stats"),
            ("/api/stats/overview?date=2026-01-01&interval=5", "get_overview_stats"),
        ]
        for path, method in failures:
            with self.subTest(path=path):
                getattr(self.mock_db_instance, method).return_value = None
                response = self.client.get(path)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.get_json()["status"], "error")

    def test_metrics_have_exact_families_and_monotonic_feedback_counters(self):
        proxy = "http://192.0.2.50:80"
        for source in ("source1", "source2"):
            self.manager.source_stats[source][proxy] = self.manager._get_new_proxy_stat(source)
        self.manager.process_feedback("source1", proxy, 4, failure_kind="dead")
        self.assertEqual(self.manager.accepted_feedback_failure_total, 1)
        self.manager.source_stats.clear()

        response = self.client.get("/metrics")
        body = response.get_data(as_text=True)
        expected_families = [
            "smartproxy_feedback_accepted_total",
            "smartproxy_feedback_unmatched_total",
            "smartproxy_source_outage_guard_active",
            "smartproxy_source_outage_guard_paused_updates_total",
            "smartproxy_validation_target_failures_total",
            "smartproxy_backup_duration_seconds",
            "smartproxy_manager_lock_hold_seconds",
            "smartproxy_plan_refresh_duration_seconds",
        ]
        for family in expected_families:
            with self.subTest(family=family):
                self.assertEqual(body.count(f"# HELP {family} "), 1)
                self.assertEqual(body.count(f"# TYPE {family} "), 1)
        self.assertIn(
            'smartproxy_feedback_accepted_total{outcome="failure"} 1', body
        )
        self.assertIn("smartproxy_feedback_unmatched_total 1", body)

    def test_get_routes_preserve_fields_and_add_source(self):
        handout = {"proxy": "http://192.0.2.51:8080", "source": "source1"}
        with patch.object(self.manager, "allocate_proxy", return_value=handout):
            normal = self.client.get("/get-proxy?source=source1")
        with patch.object(
            self.manager, "allocate_premium_proxy", return_value=handout
        ):
            premium = self.client.get("/get-premium-proxy")

        for response in (normal, premium):
            payload = response.get_json()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(payload["http"], handout["proxy"])
            self.assertEqual(payload["https"], handout["proxy"])
            self.assertEqual(payload["source"], "source1")
            self.assertNotIn("allocation_id", payload)

    def test_scheduler_submits_one_initial_fetch_and_tracks_work(self):
        job = {
            "name": "proxy_source_A",
            "url": "https://source.invalid/list",
            "interval_minutes": 1,
            "last_run": 0,
            "failure_count": 0,
        }
        self.manager.fetcher_jobs = [job]
        fetch_future = MagicMock()
        self.manager.fetch_executor.submit = MagicMock(return_value=fetch_future)
        submitted = []

        def submit(callback, *args):
            submitted.append((callback, args))
            return MagicMock()

        def stop_after_first_wait(_seconds):
            self.manager.stop_scheduler_event.set()
            return True

        with (
            patch.object(self.manager, "_submit_background", side_effect=submit),
            patch.object(self.manager, "refresh_candidate_pools"),
            patch.object(
                self.manager.stop_scheduler_event,
                "wait",
                side_effect=stop_after_first_wait,
            ),
        ):
            self.manager._scheduler_loop()

        self.manager.fetch_executor.submit.assert_called_once_with(
            self.manager._fetch_and_parse_source, job
        )
        self.assertEqual(
            sum(callback == self.manager._handle_fetch_results for callback, _ in submitted),
            1,
        )
        self.assertEqual(
            sum(callback == self.manager._run_validation_cycle for callback, _ in submitted),
            0,
        )
        handle_call = next(
            args
            for callback, args in submitted
            if callback == self.manager._handle_fetch_results
        )
        self.assertEqual(handle_call, ([fetch_future], True))

    def test_cold_start_validates_after_fetched_rows_are_inserted(self):
        events = []
        future = Future()
        future.set_result([("http", "192.0.2.60", 80)])
        self.mock_db_instance.insert_proxies.side_effect = (
            lambda _rows: events.append("insert")
        )

        with patch.object(
            self.manager,
            "_run_validation_cycle",
            side_effect=lambda: events.append("validate"),
        ):
            self.manager._handle_fetch_results(
                [future],
                validate_after_insert=True,
            )

        self.assertEqual(events, ["insert", "validate"])

    def test_cold_start_insert_failure_still_validates_existing_rows(self):
        future = Future()
        future.set_result([("http", "192.0.2.60", 80)])
        self.mock_db_instance.insert_proxies.side_effect = DatabaseWriteError(
            "insert_proxies",
            RuntimeError("injected"),
        )

        with patch.object(self.manager, "_run_validation_cycle") as validate:
            self.manager._handle_fetch_results(
                [future],
                validate_after_insert=True,
            )

        validate.assert_called_once_with()

    def test_a_slow_source_does_not_hold_back_a_fast_one(self):
        """
        Each source's rows commit when its own fetch returns. Waiting for every
        fetch first let one source spending its whole curl retry budget - about
        three minutes by default - keep a cold install from serving anything
        the fast sources had already found.
        """
        fast_row = ("http", "192.0.2.61", 80)
        table = []
        self.mock_db_instance.insert_proxies.side_effect = (
            lambda rows: table.extend(tuple(row) for row in rows)
        )
        self.mock_db_instance.get_reserve_proxies.side_effect = lambda _limit: [
            f"{protocol}://{ip}:{port}" for protocol, ip, port in table
        ]
        fast, slow = Future(), Future()
        fast.set_result([fast_row, fast_row])
        validate = self.enterContext(
            patch.object(self.manager, "_run_validation_cycle")
        )
        worker = threading.Thread(
            target=self.manager._handle_fetch_results,
            args=([fast, slow], True),
            daemon=True,
        )
        worker.start()
        # However the test ends, the slow fetch completes and the worker exits
        # while the patch still stands: a failed assertion must not leave it
        # blocked on the future for good.
        self.addCleanup(worker.join, 5)
        self.addCleanup(lambda: slow.done() or slow.set_result([]))

        deadline = time.monotonic() + 5
        while not table and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertEqual(table, [fast_row])
        self.assertEqual(
            self.manager.allocate_proxy("source1")["proxy"], "http://192.0.2.61:80"
        )
        validate.assert_not_called()

        slow.set_result([("http", "192.0.2.62", 80)])
        worker.join(timeout=5)

        self.assertEqual(len(table), 2)
        validate.assert_called_once_with()

    def test_one_source_failing_to_commit_keeps_the_others(self):
        committed = []

        def insert(rows):
            if rows == [["http", "192.0.2.63", 80]]:
                raise DatabaseWriteError("insert_proxies", RuntimeError("injected"))
            committed.extend(rows)

        self.mock_db_instance.insert_proxies.side_effect = insert
        failing, broken, good = Future(), Future(), Future()
        failing.set_result([("http", "192.0.2.63", 80)])
        broken.set_exception(RuntimeError("fetch failed"))
        good.set_result([("http", "192.0.2.64", 80)])

        self.manager._handle_fetch_results([failing, broken, good])

        self.assertEqual(committed, [["http", "192.0.2.64", 80]])

    def test_production_entry_uses_single_process_waitress(self):
        import src.main as main_module

        fake_manager = MagicMock()
        fake_manager.debug_mode = False
        fake_manager.server_port = 7000
        fake_manager.production_threads = 9
        fake_manager.server_connection_limit = 900
        fake_app = MagicMock()
        with (
            patch.object(sys, "argv", ["smartproxy"]),
            patch.object(main_module, "configure_logging_from_file"),
            patch.object(main_module, "load_proxy_manager", return_value=fake_manager),
            patch.object(main_module, "create_app", return_value=fake_app),
            patch.object(main_module, "create_server") as create_server,
            patch.object(main_module.signal, "signal"),
        ):
            main_module.main()

        fake_manager.start_scheduler.assert_called_once()
        fake_manager.stop_scheduler.assert_called_once()
        # connection_limit is passed explicitly: waitress defaults it to 100,
        # and exhausting that turns any refusal into a connection-level outage.
        # asyncore_use_poll goes with it: under the default select() loop, a
        # descriptor numbered 1024 or above raises out of the server - with a
        # thousand connections plus the database pool, that is reachable.
        create_server.assert_called_once_with(
            fake_app,
            host="0.0.0.0",
            port=7000,
            threads=9,
            connection_limit=900,
            asyncore_use_poll=True,
            # Forwarding headers reach the app, whose trusted_proxy_ips list
            # is the one authority on them; waitress would strip them first.
            clear_untrusted_proxy_headers=False,
        )
        server = create_server.return_value
        # The poll loop that never subscribes to POLLPRI, not waitress's own.
        self.assertIs(server.asyncore, main_module._PollLoop)
        server.run.assert_called_once()
        fake_app.run.assert_not_called()

    def test_importing_logger_does_not_initialize_a_persistent_sink(self):
        import src.utils.logger as logger_module

        with patch.object(logger_module.logger, "add") as add:
            importlib.reload(logger_module)
        add.assert_not_called()


class RestoreAgeTests(ProxyManagerTestBase):
    """
    Startup restores a snapshot only while it is at most 12 hours old.

    The age is the generation time recorded inside the snapshot, never the
    file's mtime, and it is the snapshot's age alone: a fresh snapshot brings
    back every record in it, however old the feedback behind that record.
    """

    NOW = 1_800_000_000.0
    PROXY = "http://192.0.2.80:80"

    def write_snapshot(self, timestamp, results=None):
        results = results or [[self.NOW - 3 * 86400, True, 120]]
        path = Path(self.tmp_dir) / "stats.json"
        snapshot = {
            "scoring_version": 2,
            "source_stats": {
                "source1": {
                    self.PROXY: {
                        "success_count": sum(1 for result in results if result[1]),
                        "failure_count": sum(1 for result in results if not result[1]),
                        "recent_results": results,
                        "last_feedback_ts": results[-1][0],
                    }
                }
            },
        }
        if timestamp is not None:
            snapshot["timestamp"] = timestamp
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        self.manager.stats_backup_path = path
        return path

    def restore_at(self, now):
        with patch("src.core.proxy_manager.time.time", return_value=now):
            return self.manager.restore_stats()

    def generated(self, hours_ago):
        return datetime.fromtimestamp(self.NOW - hours_ago * 3600).astimezone().isoformat()

    def test_a_snapshot_up_to_twelve_hours_old_is_restored(self):
        for hours_ago in (0, 11 + 59 / 60, 12):
            with self.subTest(hours_ago=hours_ago):
                self.manager.source_stats["source1"] = {}
                self.write_snapshot(self.generated(hours_ago))

                self.assertEqual(self.restore_at(self.NOW)["status"], "success")
                self.assertIn(self.PROXY, self.manager.source_stats["source1"])

    def test_an_older_snapshot_is_skipped_however_new_the_file_looks(self):
        path = self.write_snapshot(self.generated(12 + 1 / 3600))
        # A copy made just now: the mtime says new, the snapshot says old.
        copied_at = time.time()
        os.utime(path, (copied_at, copied_at))

        result = self.restore_at(self.NOW)

        self.assertEqual(result["status"], "skipped")
        self.assertNotIn(self.PROXY, self.manager.source_stats["source1"])

    def test_a_timestamp_without_an_offset_is_local_time(self):
        """How backups were written before offsets were: naive local time."""
        naive_local = datetime.fromtimestamp(self.NOW - 3600).isoformat()
        self.write_snapshot(naive_local)

        self.assertEqual(self.restore_at(self.NOW)["status"], "success")

        self.manager.source_stats["source1"] = {}
        self.write_snapshot(datetime.fromtimestamp(self.NOW - 13 * 3600).isoformat())
        self.assertEqual(self.restore_at(self.NOW)["status"], "skipped")

    def test_a_missing_or_unreadable_timestamp_skips_the_restore(self):
        for timestamp in (None, "fixture", 1_799_999_000):
            with self.subTest(timestamp=timestamp):
                self.write_snapshot(timestamp)

                result = self.restore_at(self.NOW)

                self.assertEqual(result["status"], "skipped")
                self.assertNotIn(self.PROXY, self.manager.source_stats["source1"])

    def test_a_fresh_snapshot_keeps_its_older_feedback(self):
        results = [
            [self.NOW - 3 * 86400, True, 100],
            [self.NOW - 2 * 86400, False, None],
            [self.NOW - 13 * 3600, True, 90],
        ]
        self.write_snapshot(self.generated(1), results=results)

        self.assertEqual(self.restore_at(self.NOW)["status"], "success")

        stat = self.manager.source_stats["source1"][self.PROXY]
        self.assertEqual([result[0] for result in stat["recent_results"]], [r[0] for r in results])
        self.assertEqual((stat["success_count"], stat["failure_count"]), (2, 1))

    def test_a_skipped_restore_still_serves_from_the_database(self):
        self.write_snapshot(self.generated(24))
        self.assertEqual(self.restore_at(self.NOW)["status"], "skipped")

        self.mock_db_instance.get_active_proxies.return_value = set()
        self.mock_db_instance.get_reserve_proxies.return_value = [self.PROXY]
        self.manager._sync_and_select_top_proxies()

        self.assertEqual(self.manager.allocate_proxy("source1")["proxy"], self.PROXY)

    def test_the_backup_records_its_generation_time_with_an_offset(self):
        self.manager.stats_backup_path = Path(self.tmp_dir) / "written.json"
        self.manager.backup_stats()

        written = json.loads(self.manager.stats_backup_path.read_text(encoding="utf-8"))
        self.assertIsNotNone(datetime.fromisoformat(written["timestamp"]).utcoffset())
        self.assertEqual(self.manager.restore_stats()["status"], "success")


if __name__ == "__main__":
    unittest.main()
