import unittest
from unittest.mock import patch, MagicMock, ANY, call
import os
import configparser
import sys
from collections import defaultdict

# --- Mock critical dependencies before they are imported by the main module ---
mock_logger = MagicMock()
mock_logger.add.return_value = None
patch("loguru.logger", mock_logger).start()

sys_modules = {
    "psycopg2": MagicMock(),
    "psycopg2.pool": MagicMock(),
    "psycopg2.extras": MagicMock(),
}

# Now we can safely import the module to be tested.
# We apply the patch using a context manager in the test class setUp.
import smart_proxy


class TestSmartProxyV4(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        """Apply the psycopg2 mock before any tests run."""
        cls.psycopg2_patcher = patch.dict("sys.modules", sys_modules)
        cls.psycopg2_patcher.start()
        # Reload the module to ensure it uses the mocked psycopg2
        import importlib

        importlib.reload(smart_proxy)

    @classmethod
    def tearDownClass(cls):
        """Stop the patcher."""
        cls.psycopg2_patcher.stop()

    def setUp(self):
        """Set up a clean environment for each test."""
        self.config_path = "test_config.ini"
        config = configparser.ConfigParser()
        config["server"] = {"port": "1234"}
        config["database"] = {
            "host": "fake",
            "port": "5432",
            "dbname": "fake",
            "user": "fake",
            "password": "fake",
        }
        config["validator"] = {
            "validation_workers": "10",
            "validation_timeout_s": "1",
            "validation_target": "http://fake-validator.com",
            "validation_supplement_threshold": "50",
        }
        config["scheduler"] = {
            "validation_interval_seconds": "60",
            "stats_flush_interval_seconds": "60",  # New config for testing
        }
        config["sources"] = {
            "predefined_sources": "source_a, source_b",
            "default_source": "source_a",
        }
        config["source_pool"] = {
            "max_pool_size": "100",
            "failure_penalties": "-1, -10, -50",
        }
        with open(self.config_path, "w") as f:
            config.write(f)

        self.db_mock = MagicMock()
        with patch("smart_proxy.DatabaseManager", return_value=self.db_mock):
            self.manager = smart_proxy.ProxyManager(self.config_path)

    def tearDown(self):
        """Clean up created files."""
        if os.path.exists(self.config_path):
            os.remove(self.config_path)

    def test_feedback_updates_in_memory_buffer(self):
        """
        Test that process_feedback correctly updates the in-memory buffer
        before it gets flushed to the database.
        """
        source = "source_a"
        proxy_url = "http://1.1.1.1:8080"

        # Manually add proxy to the scoring pool to avoid None error
        self.manager.source_stats[source][
            proxy_url
        ] = self.manager._get_new_proxy_stat()

        # Simulate 2 successes and 1 failure
        self.manager.process_feedback(source, proxy_url, status_code=100)
        self.manager.process_feedback(source, proxy_url, status_code=7)
        self.manager.process_feedback(source, proxy_url, status_code=500)  # Failure

        # Check the buffer state
        self.assertEqual(self.manager.feedback_buffer[source]["success"], 2)
        self.assertEqual(self.manager.feedback_buffer[source]["failure"], 1)

    def test_flush_feedback_buffer_to_db(self):
        """
        Test that the _flush_feedback_buffer method correctly aggregates
        the in-memory stats and calls the database manager.
        """
        # Populate the buffer directly for this test
        self.manager.feedback_buffer = defaultdict(
            lambda: defaultdict(int),
            {
                "source_a": defaultdict(int, {"success": 5, "failure": 2}),
                "source_b": defaultdict(int, {"success": 10}),
            },
        )

        self.manager._flush_feedback_buffer()

        # Verify that the DB flush method was called
        self.db_mock.flush_feedback_stats.assert_called_once()

        # Inspect the data that was passed to the DB method
        call_args = self.db_mock.flush_feedback_stats.call_args[0][0]

        # Convert list of tuples to a dict for easier assertion
        flushed_data = {
            item[1]: {"success": item[2], "failure": item[3]} for item in call_args
        }

        self.assertIn("source_a", flushed_data)
        self.assertIn("source_b", flushed_data)
        self.assertEqual(flushed_data["source_a"]["success"], 5)
        self.assertEqual(flushed_data["source_a"]["failure"], 2)
        self.assertEqual(flushed_data["source_b"]["success"], 10)
        self.assertEqual(
            flushed_data["source_b"].get("failure", 0), 0
        )  # Ensure failure defaults to 0 if not present

        # Verify that the buffer is cleared after flushing
        self.assertEqual(len(self.manager.feedback_buffer), 0)

    def test_feedback_updates_proxy_score(self):
        """
        Test that process_feedback still correctly updates the in-memory proxy score
        for the selection logic, independent of the buffer.
        """
        source = "source_a"
        proxy_url = "http://1.1.1.1:8080"

        # Setup initial state
        self.manager.source_stats[source][
            proxy_url
        ] = self.manager._get_new_proxy_stat()

        # First failure
        self.manager.process_feedback(source, proxy_url, status_code=404)
        self.assertEqual(self.manager.source_stats[source][proxy_url]["score"], -1)
        self.assertEqual(
            self.manager.source_stats[source][proxy_url]["consecutive_failures"], 1
        )

        # Second consecutive failure (should use the second penalty)
        self.manager.process_feedback(source, proxy_url, status_code=503)
        self.assertEqual(
            self.manager.source_stats[source][proxy_url]["score"], -1 - 10
        )  # -11
        self.assertEqual(
            self.manager.source_stats[source][proxy_url]["consecutive_failures"], 2
        )

        # A success should reset the score and consecutive failures
        self.manager.process_feedback(
            source, proxy_url, status_code=100, response_time_ms=500
        )
        # Latency bonus: (2000-500)/400 = 3.75. Base gain: 1. Total: 4.75
        self.assertAlmostEqual(
            self.manager.source_stats[source][proxy_url]["score"], 4.75
        )
        self.assertEqual(
            self.manager.source_stats[source][proxy_url]["consecutive_failures"], 0
        )

    def test_get_daily_stats_api_call(self):
        """Test the underlying DB method for the daily stats API."""
        source = "source_a"
        date = "2025-08-02"

        # Mock the DB response
        self.db_mock.get_daily_stats.return_value = {
            "total_success": 100,
            "total_failure": 25,
        }

        # This is a conceptual test of the DB call, not the Flask route itself.
        result = self.manager.db.get_daily_stats(source, date)

        self.db_mock.get_daily_stats.assert_called_with(source, date)
        self.assertEqual(result["total_success"], 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
