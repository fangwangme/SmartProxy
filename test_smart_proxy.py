# -*- coding: utf-8 -*-
import asyncio
import unittest
import configparser
import threading
from unittest.mock import MagicMock, patch, AsyncMock

from smart_proxy import ProxyManager, DatabaseManager, FAILED_STATUS_CODES


class TestProxyManager(unittest.TestCase):

    def setUp(self):
        """Set up a mock environment for each test."""
        self.mock_config = configparser.ConfigParser()
        self.mock_config.read_dict(
            {
                "database": {
                    "host": "localhost",
                    "port": "5432",
                    "dbname": "test",
                    "user": "user",
                    "password": "password",
                },
                "server": {"port": "6942"},
                "validator": {
                    "validation_workers": "10",
                    "validation_timeout_s": "5",
                    "validation_target": "http://mocktarget.com",
                    "validation_supplement_threshold": "1000",  # Match default
                    "validation_window_minutes": "30",
                    "max_validations_per_window": "5",
                },
                "scheduler": {
                    "validation_interval_seconds": "60",
                    "stats_flush_interval_seconds": "60",
                    "source_refresh_interval_seconds": "300",
                },
                "sources": {
                    "predefined_sources": "source1,source2",
                    "default_source": "source1",
                },
                "source_pool": {
                    "max_pool_size": "100",
                    "stats_pool_max_multiplier": "10",
                    "failure_penalties": "-1, -5",
                    "weighted_selection_enabled": "false",
                    "top_tier_size": "50",
                    "top_tier_load_percentage": "70",
                },
                "backup": {
                    "stats_backup_enabled": "false",
                },
                "proxy_source_A": {
                    "url": "http://source-a.com",
                    "update_interval_minutes": "10",
                },
            }
        )

        patcher_config = patch(
            "configparser.ConfigParser.read", return_value=self.mock_config
        )
        self.addCleanup(patcher_config.stop)
        patcher_config.start()

        patcher_db = patch("smart_proxy.DatabaseManager", spec=DatabaseManager)
        self.addCleanup(patcher_db.stop)
        self.MockDatabaseManager = patcher_db.start()

        self.manager = ProxyManager("dummy_path.ini")
        self.mock_db_instance = self.MockDatabaseManager.return_value

    # ========== Core Proxy Selection Tests ==========
    
    def test_get_proxy_returns_proxy_from_pool(self):
        """Test get_proxy returns a proxy when available."""
        self.manager.available_proxies["source1"] = {
            "top_tier": ["http://1.1.1.1:80", "http://2.2.2.2:80"],
            "bottom_tier": ["http://3.3.3.3:80"],
        }
        self.manager.predefined_sources.add("source1")
        
        proxy = self.manager.get_proxy("source1")
        
        self.assertIn(proxy, ["http://1.1.1.1:80", "http://2.2.2.2:80", "http://3.3.3.3:80"])

    def test_get_proxy_returns_none_when_empty(self):
        """Test get_proxy returns None when no proxies available."""
        self.manager.available_proxies["source1"] = {
            "top_tier": [],
            "bottom_tier": [],
        }
        
        proxy = self.manager.get_proxy("source1")
        
        self.assertIsNone(proxy)

    def test_get_proxy_uses_default_source_for_unknown(self):
        """Test get_proxy falls back to default source for unknown source."""
        self.manager.available_proxies["source1"] = {
            "top_tier": ["http://1.1.1.1:80"],
            "bottom_tier": [],
        }
        self.manager.predefined_sources.add("source1")
        self.manager.default_source = "source1"
        
        proxy = self.manager.get_proxy("unknown_source")
        
        self.assertEqual(proxy, "http://1.1.1.1:80")

    def test_get_premium_proxy_returns_proxy_when_available(self):
        """Test get_premium_proxy returns a proxy when premium pool is available."""
        self.manager.premium_proxies = ["http://premium1:80", "http://premium2:80"]
        
        proxy = self.manager.get_premium_proxy()
        
        self.assertIn(proxy, self.manager.premium_proxies)

    def test_get_premium_proxy_returns_none_when_empty(self):
        """Test get_premium_proxy returns None when premium pool is empty."""
        self.manager.premium_proxies = []
        
        proxy = self.manager.get_premium_proxy()
        
        self.assertIsNone(proxy)

    # ========== Feedback Processing Tests ==========
    
    def test_process_feedback_updates_score_on_success(self):
        """Test that process_feedback correctly updates proxy score on success."""
        proxy_url = "http://1.1.1.1:80"
        self.manager.predefined_sources.add("source1")
        self.manager.source_stats["source1"] = {
            proxy_url: {
                "score": 10.0,
                "success_count": 5,
                "failure_count": 1,
                "consecutive_failures": 0,
            }
        }
        
        self.manager.process_feedback("source1", proxy_url, 200, response_time_ms=500)
        
        stat = self.manager.source_stats["source1"][proxy_url]
        self.assertGreater(stat["score"], 10.0)
        self.assertEqual(stat["success_count"], 6)
        self.assertEqual(stat["consecutive_failures"], 0)

    def test_process_feedback_updates_score_on_failure(self):
        """Test that process_feedback correctly updates proxy score on failure."""
        proxy_url = "http://1.1.1.1:80"
        self.manager.predefined_sources.add("source1")
        self.manager.source_stats["source1"] = {
            proxy_url: {
                "score": 10.0,
                "success_count": 5,
                "failure_count": 1,
                "consecutive_failures": 0,
            }
        }
        
        # 0 is in FAILED_STATUS_CODES
        self.manager.process_feedback("source1", proxy_url, 0)
        
        stat = self.manager.source_stats["source1"][proxy_url]
        self.assertLess(stat["score"], 10.0)
        self.assertEqual(stat["failure_count"], 2)
        self.assertEqual(stat["consecutive_failures"], 1)

    def test_process_feedback_handles_unknown_proxy(self):
        """Test that process_feedback handles unknown proxy gracefully."""
        self.manager.predefined_sources.add("source1")
        self.manager.source_stats["source1"] = {}
        
        # Should not raise
        self.manager.process_feedback("source1", "http://unknown:80", 200)

    # ========== Lock Tests ==========
    
    def test_get_source_lock_creates_and_returns_lock(self):
        """Test that _get_source_lock creates a new lock for each source."""
        lock1 = self.manager._get_source_lock("source1")
        lock2 = self.manager._get_source_lock("source2")
        lock1_again = self.manager._get_source_lock("source1")
        
        self.assertIs(lock1, lock1_again)
        self.assertIsNot(lock1, lock2)
        self.assertIsInstance(lock1, threading.Lock)

    def test_rlock_is_reentrant(self):
        """Test that main lock is RLock and reentrant."""
        self.assertIsInstance(self.manager.lock, type(threading.RLock()))
        
        # Should not deadlock
        with self.manager.lock:
            with self.manager.lock:
                pass

    # ========== Source Management Tests ==========
    
    def test_get_source_or_default_returns_source_if_defined(self):
        """Test _get_source_or_default returns source if it's predefined."""
        self.manager.predefined_sources = {"source1", "source2"}
        
        result = self.manager._get_source_or_default("source1")
        
        self.assertEqual(result, "source1")

    def test_get_source_or_default_returns_default_if_unknown(self):
        """Test _get_source_or_default returns default for unknown source."""
        self.manager.predefined_sources = {"source1"}
        self.manager.default_source = "source1"
        
        result = self.manager._get_source_or_default("unknown")
        
        self.assertEqual(result, "source1")

    # ========== Validation Cycle Tests ==========
    
    def test_validation_cycle_skips_when_already_validating(self):
        """Test that validation cycle is skipped if already in progress."""
        self.manager.is_validating = True
        
        self.manager._run_validation_cycle()
        
        # Should not have called DB methods
        self.mock_db_instance.get_proxies_to_validate.assert_not_called()

    def test_validation_cycle_skips_when_no_proxies(self):
        """Test that validation cycle is skipped when no proxies to validate."""
        self.mock_db_instance.get_proxies_to_validate.return_value = []
        self.mock_db_instance.get_eligible_failed_proxies.return_value = []
        
        self.manager._run_validation_cycle()
        
        # Should not have updated counters
        self.mock_db_instance.update_validation_counters.assert_not_called()

    def test_validation_cycle_supplements_when_below_threshold(self):
        """Test validation cycle supplements with failed proxies when below threshold."""
        initial_proxies = [
            {"id": 1, "protocol": "http", "ip": "1.1.1.1", "port": 80},
        ]
        self.mock_db_instance.get_proxies_to_validate.return_value = initial_proxies
        self.mock_db_instance.get_eligible_failed_proxies.return_value = [
            {"id": 2, "protocol": "http", "ip": "2.2.2.2", "port": 80},
        ]
        
        async def mock_validate_batch(proxies):
            return [], [p["id"] for p in proxies]
        
        with patch.object(self.manager, "_validate_proxies_batch_async", side_effect=mock_validate_batch):
            self.manager._run_validation_cycle()
        
        # Should have called get_eligible_failed_proxies since we're below threshold
        self.mock_db_instance.get_eligible_failed_proxies.assert_called_once()

    # ========== Stats Proxy Helper Tests ==========
    
    def test_get_new_proxy_stat_returns_correct_structure(self):
        """Test _get_new_proxy_stat returns correct initial structure."""
        stat = self.manager._get_new_proxy_stat()
        
        self.assertEqual(stat["score"], 0)
        self.assertEqual(stat["success_count"], 0)
        self.assertEqual(stat["failure_count"], 0)
        self.assertEqual(stat["consecutive_failures"], 0)

    # ========== FAILED_STATUS_CODES Tests ==========
    
    def test_failed_status_codes_contains_expected_values(self):
        """Test FAILED_STATUS_CODES contains timeout and proxy error codes."""
        self.assertIn(0, FAILED_STATUS_CODES)  # Timeout
        self.assertIn(4, FAILED_STATUS_CODES)  # Proxy error


class TestDatabaseManager(unittest.TestCase):
    """Test DatabaseManager methods with mocked psycopg2."""

    @patch("smart_proxy.psycopg2.pool.ThreadedConnectionPool")
    def test_database_manager_uses_threaded_pool(self, mock_pool_class):
        """Test that DatabaseManager uses ThreadedConnectionPool."""
        mock_config = configparser.ConfigParser()
        mock_config.read_dict({
            "database": {
                "host": "localhost",
                "port": "5432", 
                "dbname": "test",
                "user": "user",
                "password": "password",
                "max_connections": "50",
            }
        })
        
        db = DatabaseManager(mock_config)
        
        mock_pool_class.assert_called_once()
        call_kwargs = mock_pool_class.call_args
        self.assertEqual(call_kwargs[1]["maxconn"], 50)


if __name__ == "__main__":
    unittest.main()
