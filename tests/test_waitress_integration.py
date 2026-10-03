# -*- coding: utf-8 -*-
"""
The production server path end to end: real waitress, production settings.

Every other test drives Flask's test client, which is not what runs in
production. Three defects lived in that gap - a select() loop that died on a
descriptor past 1024, forwarding headers stripped before the app could read
them, and a reverse proxy on the same host reaching internal endpoints - and
none of them was visible through the test client.
"""
import http.client
import logging
import resource
import threading
import unittest
from unittest.mock import patch

from src.api.server import create_app
from src.main import create_production_server, waitress_options
from tests.test_smart_proxy import ProxyManagerTestBase

# Never 6942: the real service lives there.
MOCK_PORT = 6999
OUTSIDE_CLIENT = "203.0.113.9"
OTHER_CLIENT = "198.51.100.7"


class WaitressProductionPathTests(ProxyManagerTestBase):
    def setUp(self):
        super().setUp()
        # A reverse proxy on this host, trusted to forward, in front of one
        # allowed outside client.
        self.manager.trust_proxy_headers = True
        self.manager.trusted_proxy_ips = ["127.0.0.1"]
        self.manager.allowed_ips = [OUTSIDE_CLIENT]
        self.server = create_production_server(
            create_app(self.manager), self.manager, host="127.0.0.1", port=MOCK_PORT
        )
        self.loop_errors = []

        def run():
            try:
                self.server.run()
            except Exception as error:  # the select() crash surfaces here
                self.loop_errors.append(error)

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.close()
        dispatcher = getattr(self.server, "task_dispatcher", None)
        if dispatcher is not None:
            dispatcher.shutdown()
        self.thread.join(timeout=5)

    def status(self, method, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", MOCK_PORT, timeout=5)
        try:
            connection.request(method, path, headers=headers or {})
            return connection.getresponse().status
        finally:
            connection.close()

    def test_a_client_closing_its_connection_is_not_an_event(self):
        """
        A closed connection is routine, not a "priority event".

        macOS poll() reports POLLPRI alongside POLLHUP when the peer closes,
        and waitress subscribed to POLLPRI, so every request ended with
        "unhandled incoming priority event" - one warning per request. HTTP
        never sends TCP urgent data, so the loop no longer asks for it.
        """
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        waitress_logger = logging.getLogger("waitress")
        waitress_logger.addHandler(handler)
        self.addCleanup(waitress_logger.removeHandler, handler)

        for _ in range(20):
            self.assertEqual(self.status("GET", "/api/sources"), 200)
        self.status("GET", "/api/sources")  # lets the loop see the last close

        self.assertEqual([record.getMessage() for record in records], [])

    def test_forwarding_headers_reach_the_allowlist(self):
        """
        A trusted proxy's forwarded client is the one the allowlist judges.

        With waitress stripping the header, every forwarded request looked like
        the proxy itself - loopback - and passed, whoever was behind it.
        """
        allowed = self.status("GET", "/api/sources", {"X-Forwarded-For": OUTSIDE_CLIENT})
        refused = self.status("GET", "/api/sources", {"X-Forwarded-For": OTHER_CLIENT})

        self.assertEqual((allowed, refused), (200, 403))

    def test_a_local_reverse_proxy_cannot_reach_internal_endpoints(self):
        for header in ("X-Forwarded-For", "Forwarded", "X-Real-IP"):
            with self.subTest(header=header):
                self.assertEqual(
                    self.status("GET", "/metrics", {header: OUTSIDE_CLIENT}), 403
                )
        with patch.object(self.manager, "reload_sources", return_value={}) as reload:
            refused = self.status(
                "POST", "/reload-sources", {"X-Forwarded-For": OUTSIDE_CLIENT}
            )
        self.assertEqual(refused, 403)
        reload.assert_not_called()

    def test_a_direct_local_caller_still_reaches_internal_endpoints(self):
        """The launcher and a local scraper connect directly, with no header."""
        self.assertEqual(self.status("GET", "/metrics"), 200)
        with patch.object(self.manager, "backup_stats", return_value={"status": "success"}):
            self.assertEqual(self.status("POST", "/backup-stats"), 200)

    def test_a_descriptor_past_1024_does_not_stop_the_server(self):
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if hard != resource.RLIM_INFINITY and hard < 2048:
            self.skipTest(f"open-file hard limit {hard} cannot reach descriptor 1024")
        resource.setrlimit(resource.RLIMIT_NOFILE, (4096, hard))
        filler = [open("/dev/null") for _ in range(1100)]
        try:
            self.assertGreaterEqual(filler[-1].fileno(), 1024)
            self.assertEqual(self.status("GET", "/live"), 200)
        finally:
            for handle in filler:
                handle.close()
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
        self.assertEqual(self.loop_errors, [])


if __name__ == "__main__":
    unittest.main()
