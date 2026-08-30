# -*- coding: utf-8 -*-
"""Stats endpoints: a time slot with no traffic reports an unknown success rate.

The dashboard charts a full day of slots. Encoding "no traffic" as
``success_rate: 0`` made every line dive to the floor for the remainder of the
day; the endpoints now emit ``None`` for those slots while keeping the counts
at 0.
"""
import unittest
from datetime import datetime
from unittest.mock import MagicMock

from src.core.proxy_manager import ProxyManager
from src.api.server import create_app

LOCAL = {"REMOTE_ADDR": "127.0.0.1"}
DATE = "2026-08-31"


def _slot(hour, minute, success, failure):
    return {
        "interval_start": datetime(2026, 8, 31, hour, minute),
        "success": success,
        "failure": failure,
    }


class StatsEndpointsTestCase(unittest.TestCase):
    def setUp(self):
        self.mock_proxy_manager = MagicMock(spec=ProxyManager)
        self.mock_proxy_manager.allowed_ips = []
        self.mock_proxy_manager.trust_proxy_headers = False
        self.mock_proxy_manager.trusted_proxy_ips = []
        self.mock_proxy_manager.lock = MagicMock()
        self.mock_proxy_manager.dashboard_sources = {"alpha"}
        self.mock_proxy_manager.db = MagicMock()

        self.app = create_app(self.mock_proxy_manager)
        self.client = self.app.test_client()

    def _timeseries(self, interval=60):
        response = self.client.get(
            f"/api/stats/timeseries?source=alpha&date={DATE}&interval={interval}",
            environ_overrides=LOCAL,
        )
        self.assertEqual(response.status_code, 200)
        return {row["time"]: row for row in response.get_json()}

    def _overview(self, interval=60):
        response = self.client.get(
            f"/api/stats/overview?date={DATE}&interval={interval}",
            environ_overrides=LOCAL,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()["sources"]
        return {item["source"]: item for item in payload}


class TestTimeseriesNullSemantics(StatsEndpointsTestCase):
    def test_slot_without_traffic_has_null_rate_and_zero_counts(self):
        """A slot the DB never reported is unknown, not 0%."""
        self.mock_proxy_manager.db.get_timeseries_stats.return_value = [
            _slot(1, 0, success=9, failure=1)
        ]

        rows = self._timeseries()

        empty = rows["05:00"]
        self.assertIsNone(empty["success_rate"])
        self.assertEqual(empty["total_requests"], 0)
        self.assertEqual(empty["success_count"], 0)

    def test_slot_with_traffic_keeps_its_numeric_rate(self):
        self.mock_proxy_manager.db.get_timeseries_stats.return_value = [
            _slot(1, 0, success=9, failure=1)
        ]

        rows = self._timeseries()

        busy = rows["01:00"]
        self.assertEqual(busy["success_rate"], 90.0)
        self.assertEqual(busy["total_requests"], 10)
        self.assertEqual(busy["success_count"], 9)

    def test_reported_slot_with_zero_total_is_also_null(self):
        """A row that exists but counted nothing is still 'no traffic'."""
        self.mock_proxy_manager.db.get_timeseries_stats.return_value = [
            _slot(2, 0, success=0, failure=0)
        ]

        rows = self._timeseries()

        self.assertIsNone(rows["02:00"]["success_rate"])
        self.assertEqual(rows["02:00"]["total_requests"], 0)

    def test_all_day_slots_are_still_emitted(self):
        """Zero-fill of the time axis stays; only the rate semantics changed."""
        self.mock_proxy_manager.db.get_timeseries_stats.return_value = []

        rows = self._timeseries()

        self.assertEqual(len(rows), 24)
        self.assertTrue(all(row["success_rate"] is None for row in rows.values()))

    def test_a_zero_percent_slot_is_not_confused_with_an_empty_one(self):
        """All-failure traffic must stay 0.0, distinct from None."""
        self.mock_proxy_manager.db.get_timeseries_stats.return_value = [
            _slot(3, 0, success=0, failure=4)
        ]

        rows = self._timeseries()

        self.assertEqual(rows["03:00"]["success_rate"], 0.0)
        self.assertEqual(rows["03:00"]["total_requests"], 4)


class TestOverviewNullSemantics(StatsEndpointsTestCase):
    def _set_overview(self, daily, timeseries):
        self.mock_proxy_manager.db.get_overview_stats.return_value = {
            "daily": daily,
            "timeseries": timeseries,
        }

    def test_slot_without_traffic_has_null_rate_and_zero_counts(self):
        self._set_overview(
            daily=[
                {"source_name": "alpha", "total_success": 9, "total_failure": 1}
            ],
            timeseries=[dict(_slot(1, 0, success=9, failure=1), source_name="alpha")],
        )

        points = {p["time"]: p for p in self._overview()["alpha"]["timeseries"]}

        empty = points["05:00"]
        self.assertIsNone(empty["success_rate"])
        self.assertEqual(empty["total_requests"], 0)
        self.assertEqual(empty["success_count"], 0)

    def test_slot_with_traffic_keeps_its_numeric_rate(self):
        self._set_overview(
            daily=[
                {"source_name": "alpha", "total_success": 9, "total_failure": 1}
            ],
            timeseries=[dict(_slot(1, 0, success=9, failure=1), source_name="alpha")],
        )

        points = {p["time"]: p for p in self._overview()["alpha"]["timeseries"]}

        busy = points["01:00"]
        self.assertEqual(busy["success_rate"], 90.0)
        self.assertEqual(busy["total_requests"], 10)
        self.assertEqual(busy["success_count"], 9)

    def test_reported_slot_with_zero_total_is_also_null(self):
        self._set_overview(
            daily=[
                {"source_name": "alpha", "total_success": 0, "total_failure": 0}
            ],
            timeseries=[dict(_slot(2, 0, success=0, failure=0), source_name="alpha")],
        )

        points = {p["time"]: p for p in self._overview()["alpha"]["timeseries"]}

        self.assertIsNone(points["02:00"]["success_rate"])
        self.assertEqual(points["02:00"]["total_requests"], 0)

    def test_source_without_any_traffic_has_a_fully_null_series(self):
        """Configured-but-idle sources fall back to the empty-slot shape."""
        self._set_overview(daily=[], timeseries=[])

        series = self._overview()["alpha"]["timeseries"]

        self.assertEqual(len(series), 24)
        self.assertTrue(all(point["success_rate"] is None for point in series))
        self.assertTrue(all(point["total_requests"] == 0 for point in series))

    def test_a_zero_percent_slot_is_not_confused_with_an_empty_one(self):
        self._set_overview(
            daily=[
                {"source_name": "alpha", "total_success": 0, "total_failure": 4}
            ],
            timeseries=[dict(_slot(3, 0, success=0, failure=4), source_name="alpha")],
        )

        points = {p["time"]: p for p in self._overview()["alpha"]["timeseries"]}

        self.assertEqual(points["03:00"]["success_rate"], 0.0)
        self.assertEqual(points["03:00"]["total_requests"], 4)


if __name__ == "__main__":
    unittest.main()
