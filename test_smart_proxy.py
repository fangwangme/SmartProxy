import unittest
from unittest.mock import patch, MagicMock, ANY, call
import os
import configparser
import requests

# --- Mock critical dependencies before they are imported by the main module ---

# 1. Mock the logger to prevent it from writing to files or console during tests.
# By patching 'logger.add', we prevent any handlers from being configured.
mock_logger = MagicMock()
mock_logger.add.return_value = None
patch("loguru.logger", mock_logger).start()

# 2. Mock the entire psycopg2 library to prevent any actual database connections.
sys_modules = {
    "psycopg2": MagicMock(),
    "psycopg2.pool": MagicMock(),
    "psycopg2.extras": MagicMock(),
}
# We apply the patch to 'sys.modules' using a context manager in the test class setUp.
# This ensures the mock is active when 'smart_proxy' is imported.

# Now we can safely import the module to be tested.
import smart_proxy


class TestSmartProxyV3(unittest.TestCase):

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
        config["scheduler"] = {"validation_interval_seconds": "60"}
        config["sources"] = {
            "predefined_sources": "source_a, source_b",
            "default_source": "source_a",
        }
        config["source_pool"] = {
            "max_pool_size": "100",
            "failure_penalties": "-1, -10, -50",
        }
        config["proxy_source_test"] = {
            "url": "http://fake-proxies.com/proxies.txt",
            "update_interval_minutes": "5",
            "default_protocol": "http",
        }
        with open(self.config_path, "w") as f:
            config.write(f)

        # We patch DatabaseManager to control its behavior completely.
        self.db_mock = MagicMock()
        with patch("smart_proxy.DatabaseManager", return_value=self.db_mock):
            self.manager = smart_proxy.ProxyManager(self.config_path)

    def tearDown(self):
        """Clean up created files."""
        if os.path.exists(self.config_path):
            os.remove(self.config_path)

    @patch("requests.get")
    def test_fetch_and_parse_source(self, mock_get):
        """Test fetching proxies from a URL, parsing them, and inserting into the DB."""
        mock_response = MagicMock()
        # Test with mixed formats
        mock_response.text = (
            "1.1.1.1:8080\n"
            "socks5://2.2.2.2:9090\n"
            "  \n"  # Empty line should be ignored
            "invalid-line"
        )
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        # Get the test job loaded from our test_config.ini
        test_job = self.manager.fetcher_jobs[0]
        self.manager._fetch_and_parse_source(test_job)

        # Verify that the database insert method was called with correctly parsed data
        self.db_mock.insert_proxies.assert_called_once()
        call_args = self.db_mock.insert_proxies.call_args[0][0]
        self.assertIn(("http", "1.1.1.1", 8080), call_args)
        self.assertIn(("socks5", "2.2.2.2", 9090), call_args)
        self.assertEqual(len(call_args), 2)

    @patch("requests.get")
    def test_validation_cycle_success_and_failure(self, mock_get):
        """Test the validation logic for both successful and failed proxies."""
        # Mock DB to return two proxies that need validation
        proxies_to_validate = [
            (1, "http", "1.1.1.1", 8080),  # This one will succeed
            (2, "http", "2.2.2.2", 8080),  # This one will fail
        ]
        self.db_mock.get_proxies_to_validate.return_value = proxies_to_validate
        self.db_mock.get_recent_failed_proxies.return_value = []  # No supplement needed

        # Configure mock_get to respond differently based on the URL
        def side_effect(url, proxies, timeout):
            mock_resp = MagicMock()
            if proxies["http"] == "http://1.1.1.1:8080":
                mock_resp.json.return_value = {"headers": {}}  # Anonymous
                mock_resp.raise_for_status.return_value = None
                return mock_resp
            else:
                raise requests.RequestException("Connection failed")

        mock_get.side_effect = side_effect

        # Mock the sync method that gets called at the end
        with patch.object(self.manager, "_sync_and_select_top_proxies") as mock_sync:
            self.manager._run_validation_cycle()

            # Verify that the DB was updated correctly for both proxies
            expected_calls = [
                call(1, True, ANY, "elite"),  # Success
                call(2, False, None, None),  # Failure
            ]
            self.db_mock.update_proxy_validation_result.assert_has_calls(
                expected_calls, any_order=True
            )
            self.assertEqual(self.db_mock.update_proxy_validation_result.call_count, 2)

            # Verify that the final sync was triggered
            mock_sync.assert_called_once()

    def test_get_proxy_and_feedback_flow(self):
        """Test the in-memory get/feedback cycle, including scoring logic."""
        source = "source_a"
        proxy_url_1 = "http://1.1.1.1:8080"
        proxy_url_2 = "http://2.2.2.2:8080"

        # Manually set up the in-memory state for the test
        self.manager.active_proxies = {proxy_url_1, proxy_url_2}
        self.manager._sync_and_select_top_proxies()  # This will populate the pools

        # 1. Get a proxy
        retrieved_proxy = self.manager.get_proxy(source)
        self.assertIn(retrieved_proxy, [proxy_url_1, proxy_url_2])

        # 2. Give failure feedback (1st failure)
        self.manager.process_feedback(source, proxy_url_1, "failure")
        # Score becomes 0 + (-1) = -1
        self.assertEqual(self.manager.source_stats[source][proxy_url_1]["score"], -1)
        self.assertEqual(
            self.manager.source_stats[source][proxy_url_1]["consecutive_failures"], 1
        )

        # 3. Give failure feedback again (2nd consecutive failure)
        self.manager.process_feedback(source, proxy_url_1, "failure")
        # Score becomes -1 + (-10) = -11
        self.assertEqual(self.manager.source_stats[source][proxy_url_1]["score"], -11)
        self.assertEqual(
            self.manager.source_stats[source][proxy_url_1]["consecutive_failures"], 2
        )

        # 4. Give success feedback with latency
        self.manager.process_feedback(
            source, proxy_url_1, "success", response_time_ms=300
        )
        # Latency bonus: (2000 - 300) / 400 = 4.25. Base gain is 1. Total gain = 5.25
        # Since it was failing, score resets to the gain.
        self.assertAlmostEqual(
            self.manager.source_stats[source][proxy_url_1]["score"], 5.25
        )
        self.assertEqual(
            self.manager.source_stats[source][proxy_url_1]["consecutive_failures"], 0
        )

    def test_cold_start_logic(self):
        """Test the synchronous initial load on a cold start."""
        # Patch the methods that would be called during a cold start
        with patch.object(
            self.manager, "_run_validation_cycle"
        ) as mock_validate, patch.object(
            self.manager.fetch_executor, "submit"
        ) as mock_fetch:

            # Simulate a cold start: DB returns no active proxies
            self.db_mock.get_active_proxies.return_value = set()

            # Call the main loading function
            smart_proxy.load_proxy_manager(self.config_path)

            # Verify that fetch and validate were called synchronously
            mock_fetch.assert_called()
            mock_validate.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
