import asyncio
import copy
import importlib
import json
import sys
import threading
import time
import unittest
import socket
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

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

        self.assertEqual((successes, failures), ([], []))
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

    def test_cycle_preserves_revalidation_and_pending_new_proxy_then_recovers(self):
        old_proxy = "http://192.0.2.10:80"
        self.manager.active_proxies = {old_proxy}
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
                        [],
                        {"quorum_healthy": False, "healthy_targets": [False]},
                    )
                ),
            ),
            patch.object(self.manager, "_sync_and_select_top_proxies") as sync,
        ):
            self.manager._run_validation_cycle()

        self.mock_db_instance.batch_update_proxy_results.assert_not_called()
        sync.assert_not_called()
        self.assertEqual(self.manager.active_proxies, {old_proxy})
        self.assertIsNone(self.manager.last_validation_success_ts)

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
            recovered, []
        )
        sync.assert_called_once()
        self.assertIsNotNone(self.manager.last_validation_success_ts)

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


class TestAllocationIdentityAndPlans(ProxyManagerTestBase):
    def _install_qualified(self, source="source1", proxy="http://192.0.2.30:80", quality=0.9):
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
            self.manager._build_serving_plan(source)
        return proxy, stat

    def _aggregate_total(self):
        return sum(
            counts["success"] + counts["failure"]
            for by_source in self.manager.feedback_buffer.values()
            for counts in by_source.values()
        )

    def test_out_of_order_feedback_is_exactly_once_and_duplicate_is_inert(self):
        proxy, stat = self._install_qualified()
        first = self.manager.allocate_proxy("source1")
        second = self.manager.allocate_proxy("source1")
        self.assertNotEqual(first["allocation_id"], second["allocation_id"])
        self.assertEqual(stat["handout_count"], 2)
        self.assertEqual(len(stat["inflight"]), 2)

        second_result = self.manager.process_feedback(
            "source1", proxy, 200, allocation_id=second["allocation_id"]
        )
        first_result = self.manager.process_feedback(
            "source1", proxy, 200, allocation_id=first["allocation_id"]
        )
        before_duplicate = (
            stat["success_count"],
            copy.deepcopy(stat["recent_results"]),
            self._aggregate_total(),
            self.manager.accepted_feedback_success_total,
        )
        duplicate = self.manager.process_feedback(
            "source1", proxy, 200, allocation_id=first["allocation_id"]
        )

        self.assertTrue(second_result["accepted"] and first_result["accepted"])
        self.assertFalse(duplicate["accepted"])
        self.assertEqual(duplicate["reason"], "duplicate_allocation_id")
        self.assertEqual(len(stat["inflight"]), 0)
        self.assertEqual(stat["handout_count"], 2)
        self.assertEqual(
            before_duplicate,
            (
                stat["success_count"],
                stat["recent_results"],
                self._aggregate_total(),
                self.manager.accepted_feedback_success_total,
            ),
        )

    def test_unknown_cross_source_and_cross_proxy_ids_change_nothing(self):
        proxy, stat = self._install_qualified()
        allocation = self.manager.allocate_proxy("source1")
        before = (
            stat["score"],
            stat["success_count"],
            stat["failure_count"],
            list(stat["inflight"]),
            self._aggregate_total(),
        )

        results = [
            self.manager.process_feedback(
                "source1", proxy, 200, allocation_id="unknown-token"
            ),
            self.manager.process_feedback(
                "source2", proxy, 200, allocation_id=allocation["allocation_id"]
            ),
            self.manager.process_feedback(
                "unknown-source",
                proxy,
                200,
                allocation_id=allocation["allocation_id"],
            ),
            self.manager.process_feedback(
                "source1",
                "http://192.0.2.31:80",
                200,
                allocation_id=allocation["allocation_id"],
            ),
        ]

        self.assertEqual(
            [result["reason"] for result in results],
            [
                "unknown_allocation_id",
                "allocation_source_mismatch",
                "allocation_source_mismatch",
                "allocation_proxy_mismatch",
            ],
        )
        self.assertTrue(all(not result["accepted"] for result in results))
        self.assertEqual(
            before,
            (
                stat["score"],
                stat["success_count"],
                stat["failure_count"],
                stat["inflight"],
                self._aggregate_total(),
            ),
        )

    def test_expired_allocation_is_rejected_without_mutation(self):
        proxy, stat = self._install_qualified()
        allocation = self.manager.allocate_proxy("source1")
        self.manager.allocations[allocation["allocation_id"]]["expires_at"] = (
            time.time() - 1
        )
        before = (stat["score"], stat["success_count"], self._aggregate_total())

        result = self.manager.process_feedback(
            "source1", proxy, 200, allocation_id=allocation["allocation_id"]
        )

        self.assertEqual(result, {"accepted": False, "reason": "expired"})
        self.assertEqual(len(stat["inflight"]), 0)
        self.assertEqual(
            before, (stat["score"], stat["success_count"], self._aggregate_total())
        )

    def test_completed_allocation_retention_is_time_bounded(self):
        proxy, _ = self._install_qualified()
        allocation = self.manager.allocate_proxy("source1")
        self.manager.process_feedback(
            "source1", proxy, 200, allocation_id=allocation["allocation_id"]
        )
        self.manager.completed_allocations[allocation["allocation_id"]][
            "completed_at"
        ] = time.time() - self.manager.completed_allocation_retention_s - 1

        self.manager._cleanup_allocations_locked(time.time())

        self.assertNotIn(
            allocation["allocation_id"], self.manager.completed_allocations
        )

    def test_compatibility_and_strict_modes_are_explicit(self):
        proxy, stat = self._install_qualified()
        allocation = self.manager.allocate_proxy("source1")
        compatibility = self.manager.process_feedback("source1", proxy, 200)

        self.assertEqual(compatibility["reason"], "accepted_legacy")
        self.assertEqual(self.manager.legacy_feedback_total, 1)
        self.assertNotIn(allocation["allocation_id"], self.manager.allocations)
        self.assertEqual(len(stat["inflight"]), 0)

        strict_allocation = self.manager.allocate_proxy("source1")
        before = (stat["score"], stat["success_count"], len(stat["inflight"]))
        self.manager.allow_legacy_feedback = False
        strict = self.manager.process_feedback("source1", proxy, 200)

        self.assertEqual(
            strict, {"accepted": False, "reason": "allocation_id_required"}
        )
        self.assertIn(strict_allocation["allocation_id"], self.manager.allocations)
        self.assertEqual(before, (stat["score"], stat["success_count"], len(stat["inflight"])))

    def test_stale_exploit_member_is_tombstoned_before_handout(self):
        proxy, stat = self._install_qualified()
        self.manager.serving_plan_max_age_s = 60.0
        stat.update(
            {
                "score": 1.0,
                "quality_slow": 0.01,
                "quality_fast": 0.01,
                "recent_results": [[time.time(), False, None]],
            }
        )

        self.assertIsNone(self.manager.allocate_proxy("source1"))
        plan = self.manager.serving_plans["source1"]
        self.assertNotIn(proxy, plan["exploit"])
        self.assertNotIn(proxy, plan["exploit_members"])

    def test_demotion_moves_from_exploit_to_trial_atomically(self):
        proxy, stat = self._install_qualified(quality=0.06)
        allocation = self.manager.allocate_proxy("source1")
        result = self.manager.process_feedback(
            "source1", proxy, 500, allocation_id=allocation["allocation_id"]
        )
        plan = self.manager.serving_plans["source1"]

        self.assertTrue(result["accepted"])
        self.assertFalse(self.manager._is_qualified(stat))
        self.assertNotIn(proxy, plan["exploit_members"])
        self.assertNotIn(proxy, plan["exploit"])
        self.assertIn(proxy, plan["members"])
        self.assertEqual(plan["fallback"].count(proxy), 1)

    def test_trial_cooldown_prevents_immediate_reinsertion(self):
        proxy = "http://192.0.2.32:80"
        stat = self.manager._get_new_proxy_stat("source1")
        self.manager.source_stats["source1"][proxy] = stat
        self.manager.active_proxies = {proxy}
        self.manager.available_proxies["source1"] = {
            "top_tier": [proxy],
            "bottom_tier": [],
        }
        self.manager.proxy_cooldown_ms = 1000
        with self.manager.lock:
            self.manager._build_serving_plan("source1")

        allocation = self.manager.allocate_proxy("source1")
        self.manager.process_feedback(
            "source1", proxy, 200, allocation_id=allocation["allocation_id"]
        )

        plan = self.manager.serving_plans["source1"]
        self.assertNotIn(proxy, plan["members"])
        self.assertNotIn(proxy, plan["discovery"])
        self.assertNotIn(proxy, plan["fallback"])

    def test_premium_uses_source_allocation_and_demotes_immediately(self):
        proxy, stat = self._install_qualified(quality=0.06)
        self.manager.premium_min_usage_count = 3
        self.manager._sync_premium_proxies()

        allocation = self.manager.allocate_premium_proxy()
        self.assertEqual(allocation["source"], "source1")
        self.assertTrue(self.manager.allocations[allocation["allocation_id"]]["premium"])
        self.manager.process_feedback(
            "source1", proxy, 500, allocation_id=allocation["allocation_id"]
        )

        self.assertFalse(self.manager._is_qualified(stat))
        self.assertNotIn(proxy, self.manager.premium_proxies)
        self.assertIsNone(self.manager.allocate_premium_proxy())

    def test_expired_plan_serves_without_synchronous_rebuild_and_schedules_once(self):
        proxy, _ = self._install_qualified()
        self.manager.serving_plan_max_age_s = 1.0
        self.manager.serving_plans["source1"]["built_at"] = time.time() - 10

        with (
            patch.object(self.manager, "_build_serving_plan") as build,
            patch.object(self.manager, "_submit_background") as submit,
        ):
            first = self.manager.allocate_proxy("source1")
            second = self.manager.allocate_proxy("source1")

        self.assertEqual((first["proxy"], second["proxy"]), (proxy, proxy))
        build.assert_not_called()
        submit.assert_called_once()
        self.assertIn("source1", self.manager.plan_refreshing)


class TestPersistenceAndTransactions(ProxyManagerTestBase):
    def test_old_backup_diagnostics_are_accepted_but_not_reserialized(self):
        proxy = "http://192.0.2.41:80"
        backup_path = Path(self.tmp_dir) / "legacy-extra-fields.json"
        backup_path.write_text(
            json.dumps(
                {
                    "scoring_version": 2,
                    "timestamp": "2026-01-01T00:00:00+00:00",
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
        self.assertEqual(self.manager.backup_stats()["status"], "success")
        serialized = backup_path.read_text(encoding="utf-8")
        self.assertNotIn("completed_feedback_count", serialized)
        self.assertNotIn("consecutive_failures", serialized)

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

        def retry_once(_operation, callback):
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

    def test_shutdown_flushes_current_minute_then_reputation_then_backup(self):
        self.manager.stats_backup_enabled = True
        events = []
        original_flush = self.manager._flush_stats

        def record_flush(include_current=False):
            events.append(("flush", include_current, self.manager.accepting_background_tasks))

        self.manager.fetch_executor = MagicMock()
        self.manager.background_executor = MagicMock()
        with (
            patch.object(self.manager, "_flush_stats", side_effect=record_flush),
            patch.object(self.manager, "backup_stats", side_effect=lambda: events.append(("backup",))),
        ):
            self.manager.stop_scheduler()

        self.assertIsNotNone(original_flush)
        self.assertEqual(events, [("flush", True, False), ("backup",)])
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
        self.assertLessEqual(timeout, 2.0)
        unfinished.cancel.assert_called_once()

    def test_overlapping_writers_use_deterministic_order(self):
        db = object.__new__(DatabaseManager)
        db.pool = MagicMock()
        connection = MagicMock()
        cursor = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        db.pool.getconn.return_value = connection
        db.write_max_retries = 0
        db.write_retry_base_ms = 0

        db.update_validation_counters([3, 1, 3, 2], 30)
        self.assertEqual(cursor.execute.call_args.args[1]["ids"], [1, 2, 3])

        cursor.reset_mock()
        successes = [
            {"id": 3, "latency": 30, "anonymity": "elite"},
            {"id": 1, "latency": 10, "anonymity": "elite"},
        ]
        with patch("src.database.db.psycopg2.extras.execute_values") as execute_values:
            db.batch_update_proxy_results(successes, [5, 2, 5])
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

        with patch("src.database.db.time.sleep") as sleep:
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

    def test_source_reputation_round_trip_does_not_cross_contaminate(self):
        db = object.__new__(DatabaseManager)
        proxy = "http://192.0.2.40:80"
        db._execute = MagicMock(
            return_value=[
                {
                    "protocol": "http",
                    "ip": "192.0.2.40",
                    "port": 80,
                    "source_name": "source1",
                    "success_count": 9,
                    "failure_count": 1,
                    "last_feedback_ts": 1.0,
                    "quality_slow": 0.9,
                    "quality_fast": 0.8,
                    "quality_updated_ts": 1.0,
                    "recent_results": [[1.0, True, None]],
                },
                {
                    "protocol": "http",
                    "ip": "192.0.2.40",
                    "port": 80,
                    "source_name": "source2",
                    "success_count": 1,
                    "failure_count": 9,
                    "last_feedback_ts": 2.0,
                    "quality_slow": 0.1,
                    "quality_fast": 0.2,
                    "quality_updated_ts": 2.0,
                    "recent_results": [[2.0, False, None]],
                },
            ]
        )

        history = db.get_active_feedback_history("source1")

        self.assertEqual(history["source1"][proxy]["success_count"], 9)
        self.assertEqual(history["source2"][proxy]["success_count"], 1)
        self.assertNotEqual(
            history["source1"][proxy]["recent_results"],
            history["source2"][proxy]["recent_results"],
        )
        query = db._execute.call_args.args[0]
        self.assertIn("NOT EXISTS", query)
        self.assertIn("%(default_source)s AS source_name", query)


class TestConfigurationBoundaries(ProxyManagerTestBase):
    def test_invalid_startup_values_are_rejected_before_database_creation(self):
        invalid_cases = [
            ({"server": {"port": "0"}}, "server port"),
            ({"server": {"production_threads": "0"}}, "production workers"),
            ({"server": {"shutdown_deadline_seconds": "0"}}, "shutdown deadline"),
            ({"server": {"readiness_validation_max_age_seconds": "0"}}, "validation age"),
            ({"server": {"readiness_flush_max_age_seconds": "0"}}, "flush age"),
            ({"server": {"background_workers": "0"}}, "background workers"),
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
            ({"source_pool": {"proxy_cooldown_ms": "-1"}}, "negative cooldown"),
            ({"source_pool": {"exploration_min_ratio": "-0.1"}}, "exploration minimum"),
            ({"source_pool": {"exploration_max_ratio": "1.1"}}, "exploration maximum"),
            ({"source_pool": {"exploration_min_ratio": "0.5", "exploration_max_ratio": "0.4"}}, "exploration bounds"),
            ({"source_pool": {"exploration_target_qualified": "0"}}, "exploration target"),
            ({"source_pool": {"exploration_target_qualified_ratio": "1.1"}}, "qualified ratio"),
            ({"source_pool": {"exploration_discovery_share": "1.1"}}, "discovery share"),
            ({"source_pool": {"qualification_min_results": "0"}}, "qualification evidence"),
            ({"source_pool": {"qualification_min_results": "4", "probation_attempts": "3"}}, "probation evidence"),
            ({"source_pool": {"retry_attempts": "-1"}}, "retry attempts"),
            ({"source_pool": {"retry_delay_seconds": "-1"}}, "retry delay"),
            ({"source_pool": {"probation_forgiveness_hours": "0"}}, "forgiveness"),
            ({"source_pool": {"proxy_inflight_timeout_seconds": "0"}}, "allocation expiry"),
            ({"source_pool": {"proxy_max_inflight": "-1"}}, "capacity"),
            ({"source_pool": {"exploit_draw_attempts": "0"}}, "draw attempts"),
            ({"source_pool": {"serving_plan_max_age_seconds": "0"}}, "plan age"),
            ({"source_pool": {"selection_weight_floor": "0"}}, "weight floor"),
            ({"source_pool": {"softmax_temperature": "0"}}, "temperature"),
            ({"source_pool": {"avg_latency_alpha": "0"}}, "latency alpha"),
            ({"source_pool": {"max_feedback_latency_ms": "0"}}, "latency bound"),
            ({"source_pool": {"premium_pool_size": "-1"}}, "premium pool"),
            ({"source_pool": {"premium_min_usage_count": "-1"}}, "premium evidence"),
            ({"source_pool": {"completed_allocation_retention_seconds": "0"}}, "completed retention"),
            ({"source_pool": {"completed_allocation_max": "0"}}, "completed count"),
            ({"source_pool": {"reliability_prior": "1.1"}}, "prior"),
            ({"source_pool": {"reliability_slow_alpha": "0"}}, "slow alpha"),
            ({"source_pool": {"reliability_slow_alpha": "0.5", "reliability_fast_alpha": "0.4"}}, "alpha ordering"),
            ({"source_pool": {"reliability_decay_half_life_hours": "0"}}, "half life"),
            ({"source_pool": {"reliability_recent_results_limit": "0"}}, "history limit"),
            ({"source_pool": {"reliability_history_prior_weight": "-1"}}, "history weight"),
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

    def test_restart_only_server_values_are_reported_but_not_applied_on_reload(self):
        old_values = (
            self.manager.server_port,
            self.manager.production_threads,
            self.manager.background_workers,
        )
        changed = {
            section: dict(values) for section, values in self.config_dict.items()
        }
        changed["server"].update(
            {"port": "7001", "production_threads": "12", "background_workers": "6"}
        )
        write_config_file(self.tmp_dir, changed, name="config.ini")

        result = self.manager.reload_sources()

        self.assertEqual(
            (
                self.manager.server_port,
                self.manager.production_threads,
                self.manager.background_workers,
            ),
            old_values,
        )
        self.assertIn(
            "[server] production_threads / background_workers",
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
            ),
            old_values,
        )


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
        self.manager.process_feedback("source1", proxy, 500, failure_kind="dead")
        self.assertEqual(self.manager.accepted_feedback_failure_total, 1)
        self.manager.source_stats.clear()

        response = self.client.get("/metrics")
        body = response.get_data(as_text=True)
        expected_families = [
            "smartproxy_feedback_accepted_total",
            "smartproxy_feedback_legacy_total",
            "smartproxy_feedback_rejected_total",
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
        self.assertIn("smartproxy_feedback_legacy_total 1", body)

    def test_get_routes_preserve_fields_and_add_source_and_allocation(self):
        allocation = {
            "proxy": "http://192.0.2.51:8080",
            "source": "source1",
            "allocation_id": "opaque-token",
        }
        with patch.object(self.manager, "allocate_proxy", return_value=allocation):
            normal = self.client.get("/get-proxy?source=source1")
        with patch.object(
            self.manager, "allocate_premium_proxy", return_value=allocation
        ):
            premium = self.client.get("/get-premium-proxy")

        for response in (normal, premium):
            payload = response.get_json()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(payload["http"], allocation["proxy"])
            self.assertEqual(payload["https"], allocation["proxy"])
            self.assertEqual(payload["source"], "source1")
            self.assertEqual(payload["allocation_id"], "opaque-token")

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
            patch.object(self.manager, "refresh_serving_plans"),
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
            1,
        )

    def test_production_entry_uses_single_process_waitress(self):
        import src.main as main_module

        fake_manager = MagicMock()
        fake_manager.debug_mode = False
        fake_manager.server_port = 7000
        fake_manager.production_threads = 9
        fake_app = MagicMock()
        with (
            patch.object(sys, "argv", ["smartproxy"]),
            patch.object(main_module, "configure_logging_from_file"),
            patch.object(main_module, "load_proxy_manager", return_value=fake_manager),
            patch.object(main_module, "create_app", return_value=fake_app),
            patch.object(main_module, "serve") as serve,
            patch.object(main_module.signal, "signal"),
        ):
            main_module.main()

        fake_manager.start_scheduler.assert_called_once()
        fake_manager.stop_scheduler.assert_called_once()
        serve.assert_called_once_with(
            fake_app, host="0.0.0.0", port=7000, threads=9
        )
        fake_app.run.assert_not_called()

    def test_importing_logger_does_not_initialize_a_persistent_sink(self):
        import src.utils.logger as logger_module

        with patch.object(logger_module.logger, "add") as add:
            importlib.reload(logger_module)
        add.assert_not_called()


if __name__ == "__main__":
    unittest.main()
