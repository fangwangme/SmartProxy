# -*- coding: utf-8 -*-
"""
Issue #27: serving must never depend on accumulated reputation.

The incident these tests exist for went unnoticed because nothing asserted the
one property that matters: while the `proxies` table holds anything at all, the
handout path answers. Every test below is written against that invariant rather
than against the mechanism that implements it.
"""
import random
import time
from concurrent.futures import Future
from unittest.mock import patch

from src.api.server import create_app
from tests.test_smart_proxy import ProxyManagerTestBase


def urls(count: int, first_octet: int = 100) -> list:
    """`count` distinct proxy URLs in a documentation-only address range."""
    return [
        f"http://192.0.2.{first_octet + index // 250}:{9000 + index % 250}"
        for index in range(count)
    ]


class CandidatePoolInvariantTests(ProxyManagerTestBase):
    """The invariant: a non-empty proxies table always yields a handout."""

    SOURCE = "source1"

    def seed_pool(self, active=(), reserve=()):
        """Publish a database state into the in-memory pools via the real sync."""
        self.mock_db_instance.get_active_proxies.return_value = set(active)
        self.mock_db_instance.get_reserve_proxies.return_value = list(reserve)
        self.manager._sync_and_select_top_proxies()

    def assert_always_serves(self, attempts: int = 200, source=None):
        """The handout path must answer every time, not merely usually."""
        source = self.SOURCE if source is None else source
        for attempt in range(attempts):
            handout = self.manager.allocate_proxy(source)
            self.assertIsNotNone(
                handout,
                f"allocate_proxy returned None on attempt {attempt + 1} while "
                f"the pool held proxies",
            )

    def test_cold_start_serves_with_nothing_validated(self):
        """
        The case the issue exists for: rows fetched, nothing validated yet.

        Tier 3 has to carry the whole pool here, which means never-validated
        proxies must reach memory at all.
        """
        self.seed_pool(active=(), reserve=urls(50))

        self.assert_always_serves()

    def test_exhausted_probation_budget_still_serves(self):
        """
        The observed production shape: three results, zero successes, scores
        below the prior. Under the gate model every one of these is locked out.
        """
        pool = urls(240, first_octet=110)
        now = time.time()
        for proxy_url in pool:
            stat = self.manager._get_new_proxy_stat(self.SOURCE)
            stat["recent_results"] = [[now - 60 + i, False, None] for i in range(3)]
            stat["failure_count"] = 3
            stat["score"] = 1.75
            self.manager.source_stats[self.SOURCE][proxy_url] = stat
        self.seed_pool(active=pool)

        self.assert_always_serves()

    def test_never_qualifying_pool_still_serves(self):
        """A ~1% success rate never clears a `score > prior` bar. It must serve."""
        pool = urls(120, first_octet=130)
        now = time.time()
        for index, proxy_url in enumerate(pool):
            results = [[now - 100 + i, i == 0 and index == 0, None] for i in range(20)]
            stat = self.manager._get_new_proxy_stat(self.SOURCE)
            stat["recent_results"] = results
            self.manager.source_stats[self.SOURCE][proxy_url] = stat
        self.seed_pool(active=pool)

        self.assert_always_serves()

    def test_get_proxy_route_never_answers_404_over_a_live_pool(self):
        """The invariant at the HTTP boundary, where the incident was measured."""
        self.seed_pool(active=urls(10, first_octet=150), reserve=urls(10, first_octet=160))
        app = create_app(self.manager)
        client = app.test_client()

        for attempt in range(100):
            response = client.get(f"/get-proxy?source={self.SOURCE}")
            self.assertEqual(
                response.status_code,
                200,
                f"/get-proxy answered {response.status_code} on attempt "
                f"{attempt + 1} while the pool held proxies",
            )

    def test_empty_database_is_the_only_way_to_get_nothing(self):
        """The invariant is bounded by the database, not by reputation."""
        self.seed_pool(active=(), reserve=())

        self.assertIsNone(self.manager.allocate_proxy(self.SOURCE))


class CandidatePoolFillTests(ProxyManagerTestBase):
    """After any rebuild the pool holds min(N, everything available)."""

    SOURCE = "source1"

    def pool(self, source=None):
        return self.manager.candidate_pools[self.SOURCE if source is None else source]

    def seed_pool(self, active=(), reserve=()):
        self.mock_db_instance.get_active_proxies.return_value = set(active)
        self.mock_db_instance.get_reserve_proxies.return_value = list(reserve)
        self.manager._sync_and_select_top_proxies()

    def test_pool_is_full_in_every_tier_combination(self):
        """Nothing scored, nothing validated, and every mixture in between."""
        self.manager.candidate_pool_size = 20
        now = time.time()
        scored = urls(8, first_octet=170)
        validated = urls(8, first_octet=180)
        raw = urls(8, first_octet=190)
        for proxy_url in scored:
            stat = self.manager._get_new_proxy_stat(self.SOURCE)
            stat["recent_results"] = [[now, True, None]]
            stat["success_count"] = 1
            self.manager.source_stats[self.SOURCE][proxy_url] = stat

        combinations = [
            ((), ()),
            ((), raw),
            (validated, ()),
            (validated, raw),
            (scored, ()),
            (scored + validated, raw),
        ]
        for active, reserve in combinations:
            with self.subTest(active=len(active), reserve=len(reserve)):
                self.seed_pool(active=active, reserve=reserve)
                available = len(set(active) | set(reserve))
                self.assertEqual(
                    len(self.pool()),
                    min(self.manager.candidate_pool_size, available),
                )

    def test_pool_is_capped_at_the_configured_size(self):
        self.manager.candidate_pool_size = 25
        self.seed_pool(active=urls(200, first_octet=100))

        self.assertEqual(len(self.pool()), 25)

    def test_tier_one_wins_the_slots_it_can_fill(self):
        """Score orders the pool; it never decides whether a proxy may serve."""
        self.manager.candidate_pool_size = 10
        self.manager.exploration_slots = 2
        now = time.time()
        proven = urls(6, first_octet=170)
        for proxy_url in proven:
            stat = self.manager._get_new_proxy_stat(self.SOURCE)
            stat["recent_results"] = [[now, True, None]] * 4
            stat["success_count"] = 4
            stat["score"] = 90.0
            self.manager.source_stats[self.SOURCE][proxy_url] = stat
        blank = urls(40, first_octet=180)
        self.seed_pool(active=proven + blank)

        pool = set(self.pool())
        self.assertTrue(set(proven) <= pool)
        self.assertEqual(len(pool), 10)


class CandidatePoolServingTests(ProxyManagerTestBase):
    """Serving behaviour: uniform inside the pool, no gate anywhere on the path."""

    SOURCE = "source1"

    def seed_pool(self, active=(), reserve=()):
        self.mock_db_instance.get_active_proxies.return_value = set(active)
        self.mock_db_instance.get_reserve_proxies.return_value = list(reserve)
        self.manager._sync_and_select_top_proxies()

    def test_handout_is_uniform_over_the_pool(self):
        self.manager.candidate_pool_size = 8
        pool = urls(8, first_octet=100)
        self.seed_pool(active=pool)

        random.seed(1234)
        seen = {}
        for _ in range(4000):
            handout = self.manager.allocate_proxy(self.SOURCE)
            seen[handout["proxy"]] = seen.get(handout["proxy"], 0) + 1

        self.assertEqual(len(seen), 8)
        for count in seen.values():
            self.assertGreater(count, 4000 / 8 * 0.6)

    def test_concurrency_never_removes_a_proxy_from_the_pool(self):
        """A burst against a one-proxy pool keeps serving that proxy."""
        self.manager.candidate_pool_size = 200
        only = urls(1, first_octet=100)
        self.seed_pool(active=only)

        for _ in range(50):
            handout = self.manager.allocate_proxy(self.SOURCE)
            self.assertEqual(handout["proxy"], only[0])

    def test_repeated_failures_never_remove_a_proxy_from_the_pool(self):
        """Feedback re-ranks; it may not empty the servable set."""
        self.manager.candidate_pool_size = 200
        only = urls(1, first_octet=100)
        self.seed_pool(active=only)

        for _ in range(30):
            handout = self.manager.allocate_proxy(self.SOURCE)
            self.assertIsNotNone(handout)
            self.manager.process_feedback(self.SOURCE, handout["proxy"], 0)

        self.manager._rebuild_candidate_pool(self.SOURCE)
        self.assertIsNotNone(self.manager.allocate_proxy(self.SOURCE))


class ExplorationSlotTests(ProxyManagerTestBase):
    """The reserved slots are a floor for newcomers, never a gate."""

    SOURCE = "source1"

    def seed_pool(self, active=(), reserve=()):
        self.mock_db_instance.get_active_proxies.return_value = set(active)
        self.mock_db_instance.get_reserve_proxies.return_value = list(reserve)
        self.manager._sync_and_select_top_proxies()

    def test_newcomers_reach_a_pool_saturated_by_incumbents(self):
        self.manager.candidate_pool_size = 10
        self.manager.exploration_slots = 3
        now = time.time()
        incumbents = urls(30, first_octet=170)
        for proxy_url in incumbents:
            stat = self.manager._get_new_proxy_stat(self.SOURCE)
            stat["recent_results"] = [[now, True, None]] * 5
            stat["success_count"] = 5
            stat["score"] = 95.0
            self.manager.source_stats[self.SOURCE][proxy_url] = stat
        newcomers = urls(10, first_octet=180)
        self.seed_pool(active=incumbents + newcomers)

        pool = set(self.manager.candidate_pools[self.SOURCE])
        self.assertEqual(len(pool), 10)
        self.assertGreaterEqual(len(pool & set(newcomers)), 3)

    def test_zero_exploration_slots_still_fills_the_pool(self):
        self.manager.candidate_pool_size = 10
        self.manager.exploration_slots = 0
        self.seed_pool(active=urls(40, first_octet=100))

        self.assertEqual(len(self.manager.candidate_pools[self.SOURCE]), 10)

    def test_exploration_slots_above_the_pool_size_cannot_starve_it(self):
        self.manager.candidate_pool_size = 10
        self.manager.exploration_slots = 50
        self.seed_pool(active=urls(40, first_octet=100))

        self.assertEqual(len(self.manager.candidate_pools[self.SOURCE]), 10)


class LowSuccessRateTests(ProxyManagerTestBase):
    """
    A ~1% success rate is a serviceable pool, not a degraded one.

    This is the shape the incident had: 240 live proxies, three results each,
    no successes, every score below the prior. Under the gate model it produced
    2220 refusals in 3000 requests while every one of those proxies was alive.
    """

    SOURCE = "source1"

    def drive(self, requests: int, success_every: int, better: set = frozenset()):
        """Serve `requests` requests, reporting one success in `success_every`."""
        served, refused, served_better = 0, 0, 0
        for step in range(requests):
            handout = self.manager.allocate_proxy(self.SOURCE)
            if handout is None:
                refused += 1
                continue
            served += 1
            proxy_url = handout["proxy"]
            if proxy_url in better:
                served_better += 1
                is_success = step % max(1, success_every // 10) == 0
            else:
                is_success = step % success_every == 0
            self.manager.process_feedback(
                self.SOURCE, proxy_url, 200 if is_success else 0
            )
        return served, refused, served_better

    def test_one_percent_success_rate_is_bounded_by_demand_not_by_a_bar(self):
        pool = urls(240, first_octet=100)
        self.mock_db_instance.get_active_proxies.return_value = set(pool)
        self.mock_db_instance.get_reserve_proxies.return_value = []
        self.manager._sync_and_select_top_proxies()

        served, refused, _ = self.drive(requests=3000, success_every=100)

        self.assertEqual(refused, 0)
        self.assertEqual(served, 3000)

    def test_ranking_concentrates_traffic_on_the_better_proxies(self):
        """The pool is the ranking: proxies that succeed take over the slots."""
        self.manager.candidate_pool_size = 40
        self.manager.exploration_slots = 4
        pool = urls(240, first_octet=100)
        better = set(pool[:20])
        self.mock_db_instance.get_active_proxies.return_value = set(pool)
        self.mock_db_instance.get_reserve_proxies.return_value = []
        self.manager._sync_and_select_top_proxies()

        random.seed(2727)
        # Learn, rebuilding the pool as the timer would, then measure.
        for _ in range(10):
            self.drive(requests=400, success_every=100, better=better)
            self.manager._rebuild_candidate_pool(self.SOURCE)

        _, refused, served_better = self.drive(
            requests=400, success_every=100, better=better
        )

        self.assertEqual(refused, 0)
        # Uniform over 240 would put ~8% of traffic on the 20 better proxies.
        # Across 30 seeds this scenario averages 0.27 and never fell below
        # 0.20: a proxy that succeeds one time in ten drops below an untried
        # one after five or six straight failures, which is the score's design
        # (min of the two estimators, an optimistic prior) and leaves it to
        # exploration.
        self.assertGreater(served_better / 400, 2 * 20 / 240)


class ColdStartTests(ProxyManagerTestBase):
    """From an empty database forward, with no interval of refusals."""

    SOURCE = "source1"

    def test_service_serves_continuously_from_the_first_fetch_onward(self):
        fetched = urls(300, first_octet=100)
        validated = set(fetched[:12])
        timeline = [
            # (active, reserve) as the database looks at each step.
            (set(), []),                       # empty database
            (set(), fetched),                  # first fetch landed, nothing validated
            (validated, fetched[12:]),         # first validation cycle
            (validated, fetched[12:]),         # steady state
        ]

        refusals = 0
        for step, (active, reserve) in enumerate(timeline):
            self.mock_db_instance.get_active_proxies.return_value = set(active)
            self.mock_db_instance.get_reserve_proxies.return_value = list(reserve)
            self.manager._sync_and_select_top_proxies()
            for _ in range(200):
                handout = self.manager.allocate_proxy(self.SOURCE)
                if handout is None:
                    # Step 0 is the only legitimate refusal: no proxies exist.
                    refusals += 1
                    self.assertEqual(step, 0, "refused while the database held proxies")
                    continue
                self.manager.process_feedback(self.SOURCE, handout["proxy"], 0)

        self.assertEqual(refusals, 200)

    def test_validation_promotes_into_tier_two_without_a_gap_in_service(self):
        """Tier 3 carries the pool, then hands the slots to tier 2."""
        self.manager.candidate_pool_size = 10
        self.manager.exploration_slots = 0
        fetched = urls(100, first_octet=100)

        self.mock_db_instance.get_active_proxies.return_value = set()
        self.mock_db_instance.get_reserve_proxies.return_value = fetched
        self.manager._sync_and_select_top_proxies()
        cold_pool = list(self.manager.candidate_pools[self.SOURCE])
        self.assertEqual(len(cold_pool), 10)
        self.assertTrue(set(cold_pool) <= set(fetched))

        alive = set(fetched[50:70])
        self.mock_db_instance.get_active_proxies.return_value = alive
        self.mock_db_instance.get_reserve_proxies.return_value = [
            url for url in fetched if url not in alive
        ]
        self.manager._sync_and_select_top_proxies()

        warm_pool = self.manager.candidate_pools[self.SOURCE]
        self.assertEqual(len(warm_pool), 10)
        self.assertTrue(set(warm_pool) <= alive)


class ProductionShapeReplayTests(ProxyManagerTestBase):
    """
    Replay the distribution recorded during the cold window.

    For the affected source, 240 retained stats: 0 qualified, 128 holding
    exactly three results with no successes, only 15% having ever recorded a
    single success, score median 1.75 against a qualification bar of 5.0.
    """

    SOURCE = "source1"

    def build_pool(self):
        pool = urls(240, first_octet=100)
        now = time.time()
        for index, proxy_url in enumerate(pool):
            stat = self.manager._get_new_proxy_stat(self.SOURCE)
            if index < 128:
                # Three results, no successes: the signature of a spent budget.
                results = [[now - 300 + i, False, None] for i in range(3)]
                stat["failure_count"] = 3
            elif index < 204:
                # Ever recorded a success, but nowhere near the prior.
                results = [[now - 300 + i, i == 0, None] for i in range(8)]
                stat["success_count"] = 1
                stat["failure_count"] = 7
            else:
                results = []
            stat["recent_results"] = results
            stat["score"] = 1.75
            self.manager.source_stats[self.SOURCE][proxy_url] = stat
        self.mock_db_instance.get_active_proxies.return_value = set(pool)
        self.mock_db_instance.get_reserve_proxies.return_value = []
        self.manager._sync_and_select_top_proxies()
        # The better third of the population; the replay does not tell the
        # router which those are, it only reports their outcomes.
        return pool, set(pool[::3])

    ROUNDS = 15
    REQUESTS_PER_ROUND = 1000

    def test_served_quality_rises_while_served_volume_never_touches_zero(self):
        self.manager.candidate_pool_size = 60
        self.manager.exploration_slots = 6
        pool, better = self.build_pool()

        random.seed(27)
        volume_curve, quality_curve, concentration_curve = [], [], []
        for _ in range(self.ROUNDS):
            served = successes = on_better = 0
            for _ in range(self.REQUESTS_PER_ROUND):
                handout = self.manager.allocate_proxy(self.SOURCE)
                if handout is None:
                    continue
                served += 1
                is_better = handout["proxy"] in better
                on_better += int(is_better)
                # 12% for the better third, 1% for the rest.
                is_success = random.random() < (0.12 if is_better else 0.01)
                successes += int(is_success)
                self.manager.process_feedback(
                    self.SOURCE, handout["proxy"], 200 if is_success else 0
                )
            volume_curve.append(served)
            quality_curve.append(successes / served)
            concentration_curve.append(on_better / served)
            self.manager._rebuild_candidate_pool(self.SOURCE)

        print(
            f"\n[#27 production-shape replay] volume={volume_curve}\n"
            f"  served success rate={[round(r, 4) for r in quality_curve]}\n"
            f"  share on the better third={[round(r, 3) for r in concentration_curve]}"
        )

        # The served-volume curve never touches zero - it never even dips
        # below what the client asked for.
        self.assertTrue(
            all(served == self.REQUESTS_PER_ROUND for served in volume_curve),
            f"served volume fell below client demand: {volume_curve}",
        )

        # The served-quality curve rises. Per-round success counts are noisy at
        # a ~6% rate, so both ends are read over three rounds - 3000 requests
        # each - rather than off single points.
        early_quality = sum(quality_curve[:3]) / 3
        late_quality = sum(quality_curve[-3:]) / 3
        self.assertGreater(late_quality, early_quality * 1.15)

        # And the mechanism behind the rise: tier 1 concentrates traffic on the
        # better proxies. A uniform draw over the whole population would hold
        # this at one third for every round, which is where the first round
        # starts from - the pool has learned nothing yet.
        self.assertGreater(sum(concentration_curve[-3:]) / 3, 0.45)
        self.assertGreater(
            sum(concentration_curve[-3:]) / 3, concentration_curve[0] * 1.3
        )


class RoutingParameterAuditTests(ProxyManagerTestBase):
    """No routing parameter may empty the servable set at any accepted value."""

    SOURCE = "source1"

    def test_no_accepted_value_of_any_routing_parameter_empties_the_pool(self):
        pool = urls(60, first_octet=100)
        now = time.time()
        for index, proxy_url in enumerate(pool):
            stat = self.manager._get_new_proxy_stat(self.SOURCE)
            stat["recent_results"] = [[now - 10 + i, index % 7 == 0, None] for i in range(5)]
            stat["failure_count"] = 5
            self.manager.source_stats[self.SOURCE][proxy_url] = stat
        self.mock_db_instance.get_active_proxies.return_value = set(pool)
        self.mock_db_instance.get_reserve_proxies.return_value = urls(20, first_octet=170)

        extremes = {
            "candidate_pool_size": [1, 200, 100000],
            "pool_refresh_seconds": [0.001, 60.0, 86400.0],
            "exploration_slots": [0, 20, 100000],
            "max_pool_size": [1, 200],
            "top_tier_size": [0, 100],
            "premium_pool_size": [0, 20],
            "premium_min_usage_count": [0, 50],
            "proxy_inflight_timeout_s": [0.1, 120.0],
        }
        for name, values in extremes.items():
            original = getattr(self.manager, name)
            for value in values:
                with self.subTest(parameter=name, value=value):
                    setattr(self.manager, name, value)
                    self.manager._sync_and_select_top_proxies()
                    self.assertTrue(self.manager.candidate_pools[self.SOURCE])
                    for _ in range(20):
                        self.assertIsNotNone(self.manager.allocate_proxy(self.SOURCE))
            setattr(self.manager, name, original)

    def test_shipped_example_config_declares_the_pool_parameters(self):
        import configparser

        from src.core.proxy_manager import CONFIG_EXAMPLE_PATH

        example = configparser.ConfigParser()
        self.assertTrue(example.read(CONFIG_EXAMPLE_PATH, encoding="utf-8"))

        for key in ("candidate_pool_size", "pool_refresh_seconds", "exploration_slots"):
            self.assertTrue(example.has_option("source_pool", key), key)
        self.assertTrue(example.has_option("server", "connection_limit"))

        # The gate administration is gone from the shipped config as well as
        # from the code; leaving the keys would drift straight back in.
        for key in (
            "exploration_min_ratio",
            "exploration_max_ratio",
            "exploration_target_qualified",
            "exploration_target_qualified_ratio",
            "exploration_discovery_share",
            "qualification_min_results",
            "probation_attempts",
            "retry_attempts",
            "retry_delay_seconds",
            "probation_forgiveness_hours",
            "exploit_draw_attempts",
            "proxy_max_inflight",
            "proxy_cooldown_ms",
            "serving_plan_max_age_seconds",
            "avg_latency_alpha",
        ):
            self.assertFalse(example.has_option("source_pool", key), key)


class ReviewRegressionTests(ProxyManagerTestBase):
    """
    Three holes the PR #28 review found in the first implementation.

    Each is the same mistake in a different place: the invariant was asserted
    from the pool-sync boundary inward, so anything that could stop the sync
    from running, or stop a servable proxy from reaching a tier, went unseen.
    """

    SOURCE = "source1"

    def test_a_failed_validation_cycle_does_not_strand_fetched_rows(self):
        """
        The reserve must not depend on a validation cycle *succeeding*.

        Rows are servable the moment they are committed. Waiting for a healthy
        validation quorum leaves the router blind to the whole table on a cold
        pool, which is exactly when it has nothing else to serve.
        """
        rows = urls(50)
        self.mock_db_instance.get_active_proxies.return_value = set()
        self.mock_db_instance.get_reserve_proxies.return_value = rows
        self.mock_db_instance.get_new_proxies_to_validate.return_value = [
            {"id": index, "protocol": "http", "ip": "192.0.2.1", "port": 9000 + index}
            for index in range(5)
        ]
        self.mock_db_instance.get_active_proxies_to_revalidate.return_value = []
        self.mock_db_instance.get_eligible_failed_proxies.return_value = []

        async def quorum_down(_proxies):
            return [], [0, 1, 2, 3, 4], {"quorum_healthy": False, "healthy_targets": 0}

        with patch.object(
            self.manager, "_validate_proxies_batch_async", side_effect=quorum_down
        ):
            self.manager._run_validation_cycle()
        self.manager.refresh_candidate_pools()

        self.assertIsNotNone(
            self.manager.allocate_proxy(self.SOURCE),
            "refused while the database held 50 proxies",
        )

    def test_a_fetch_alone_makes_the_service_servable(self):
        """
        "Serves continuously from the first fetch onward" means the fetch, not
        the validation cycle that may follow it minutes later or fail.
        """
        rows = urls(30)
        self.mock_db_instance.get_active_proxies.return_value = set()
        self.mock_db_instance.get_reserve_proxies.return_value = rows
        self.mock_db_instance.insert_proxies.return_value = True
        fetched = Future()
        fetched.set_result([("http", "192.0.2.100", 9000)])

        self.manager._handle_fetch_results([fetched], False)

        self.assertIsNotNone(
            self.manager.allocate_proxy(self.SOURCE),
            "refused after a fetch committed rows to an empty pool",
        )

    def test_a_failed_reserve_query_keeps_the_previous_reserve(self):
        """The design rule the fix must not break: a blip cannot empty tier 3."""
        rows = urls(20)
        self.mock_db_instance.get_active_proxies.return_value = set()
        self.mock_db_instance.get_reserve_proxies.return_value = rows
        self.manager._sync_and_select_top_proxies()

        self.mock_db_instance.get_reserve_proxies.return_value = None
        self.assertFalse(self.manager._refresh_reserve_proxies())
        self.manager.refresh_candidate_pools()

        self.assertEqual(len(self.manager.reserve_proxies), 20)
        self.assertIsNotNone(self.manager.allocate_proxy(self.SOURCE))

    def test_successful_feedback_reaches_tier_one_without_is_active(self):
        """
        Tier 1 is "proxies with successful feedback on record" — no more.

        Requiring is_active would put validation back in front of the ranking
        as an admission test, and would rank a never-measured proxy above one
        that real traffic keeps succeeding on.
        """
        self.manager.candidate_pool_size = 1
        self.manager.exploration_slots = 0
        proven, unproven = "http://192.0.2.50:9000", "http://192.0.2.60:9000"
        now = time.time()
        stat = self.manager._get_new_proxy_stat(self.SOURCE)
        stat.update(
            {
                "success_count": 10,
                "recent_results": [[now, True, None]] * 10,
                "quality_slow": 0.74,
                "quality_fast": 0.74,
                "quality_updated_ts": now,
            }
        )
        self.manager.source_stats[self.SOURCE][proven] = stat
        self.manager._refresh_score(stat, self.SOURCE)
        self.mock_db_instance.get_active_proxies.return_value = {unproven}
        self.mock_db_instance.get_reserve_proxies.return_value = [proven]

        self.manager._sync_and_select_top_proxies()

        self.assertGreater(stat["score"], 50.0)
        self.assertEqual(self.manager.candidate_pools[self.SOURCE], [proven])

    def test_tier_one_is_not_duplicated_when_both_queries_report_a_proxy(self):
        """The two queries can race; a proxy in both must occupy one slot."""
        self.manager.candidate_pool_size = 10
        shared = "http://192.0.2.70:9000"
        now = time.time()
        stat = self.manager._get_new_proxy_stat(self.SOURCE)
        stat.update({"success_count": 3, "recent_results": [[now, True, None]] * 3})
        self.manager.source_stats[self.SOURCE][shared] = stat
        self.mock_db_instance.get_active_proxies.return_value = {shared}
        self.mock_db_instance.get_reserve_proxies.return_value = [shared]

        self.manager._sync_and_select_top_proxies()

        self.assertEqual(self.manager.candidate_pools[self.SOURCE], [shared])

    def test_expired_leases_are_reclaimed_without_any_feedback(self):
        """
        A client that takes proxies and never reports must not grow the stat.

        Deleting the trial epoch removed the sweep that used to prune these as
        a side effect, leaving feedback as the only thing that reclaimed a
        lease — and feedback is precisely what this client never sends.
        """
        only = urls(1)
        self.mock_db_instance.get_active_proxies.return_value = set(only)
        self.mock_db_instance.get_reserve_proxies.return_value = []
        self.manager._sync_and_select_top_proxies()
        stat = self.manager.source_stats[self.SOURCE][only[0]]

        start = time.time()
        for step in range(500):
            with patch(
                "src.core.proxy_manager.time.time",
                return_value=start + step * (self.manager.proxy_inflight_timeout_s + 1),
            ):
                self.manager.allocate_proxy(self.SOURCE)
                self.manager.refresh_candidate_pools()

        self.assertLessEqual(len(stat["inflight"]), 2)
        self.assertEqual(stat["handout_count"], 500)

    def test_an_idle_proxy_does_not_keep_its_leases_forever(self):
        """The sync sweep reaches proxies that stopped being handed out."""
        only = urls(1)
        self.mock_db_instance.get_active_proxies.return_value = set(only)
        self.mock_db_instance.get_reserve_proxies.return_value = []
        self.manager._sync_and_select_top_proxies()
        stat = self.manager.source_stats[self.SOURCE][only[0]]
        for _ in range(20):
            self.manager.allocate_proxy(self.SOURCE)
        self.assertEqual(len(stat["inflight"]), 20)

        future = time.time() + self.manager.proxy_inflight_timeout_s + 1
        with patch("src.core.proxy_manager.time.time", return_value=future):
            self.manager._sync_and_select_top_proxies()

        self.assertEqual(stat["inflight"], [])


class SecondReviewRegressionTests(ProxyManagerTestBase):
    """Findings from the second review of PR #28."""

    SOURCE = "source1"

    def test_truncation_keeps_the_record_of_a_servable_reserve_proxy(self):
        """
        Eviction must follow the servable set, not only the live one.

        The pool re-seeds a blank stat for any member that lacks one, so
        evicting a reserve proxy still in the pool erased its failure history
        while it went on being served.
        """
        self.manager.max_pool_size = 1
        self.manager.stats_pool_max_multiplier = 2
        active = urls(2, first_octet=10)
        reserve = urls(1, first_octet=80)[0]
        now = time.time()
        record = self.manager._get_new_proxy_stat(self.SOURCE)
        record.update(
            {
                "success_count": 1,
                "failure_count": 4,
                "recent_results": [[now, True, None]] + [[now, False, None]] * 4,
                "last_feedback_ts": now,
            }
        )
        self.manager.source_stats[self.SOURCE][reserve] = record
        self.mock_db_instance.get_active_proxies.return_value = set(active)
        self.mock_db_instance.get_reserve_proxies.return_value = [reserve]

        self.manager._sync_and_select_top_proxies()

        self.assertIn(reserve, self.manager.candidate_pools[self.SOURCE])
        kept = self.manager.source_stats[self.SOURCE][reserve]
        self.assertIs(kept, record)
        self.assertEqual((kept["success_count"], kept["failure_count"]), (1, 4))

    def test_truncation_still_evicts_history_nothing_serves(self):
        """Only the servable set is exempt; the cap still bounds the rest."""
        self.manager.max_pool_size = 1
        self.manager.stats_pool_max_multiplier = 2
        active = urls(2, first_octet=10)
        gone = urls(3, first_octet=90)
        for proxy_url in gone:
            self.manager.source_stats[self.SOURCE][proxy_url] = (
                self.manager._get_new_proxy_stat(self.SOURCE)
            )
        self.mock_db_instance.get_active_proxies.return_value = set(active)
        self.mock_db_instance.get_reserve_proxies.return_value = []

        self.manager._sync_and_select_top_proxies()

        self.assertFalse(set(gone) & set(self.manager.source_stats[self.SOURCE]))


class ScoreRankingTests(ProxyManagerTestBase):
    """
    The final review of PR #28: the pool must rank on the current score.

    Its first version filled tiers by identity - any success on record, then
    validated - and kept exploration for proxies no feedback had ever reached.
    Neither identity expired, so both outlived the evidence behind them.
    """

    SOURCE = "source1"

    def setUp(self):
        super().setUp()
        self.manager.candidate_pool_size = 200
        self.manager.exploration_slots = 20

    def seed_pool(self, active=(), reserve=()):
        self.mock_db_instance.get_active_proxies.return_value = set(active)
        self.mock_db_instance.get_reserve_proxies.return_value = list(reserve)
        self.manager._sync_and_select_top_proxies()

    def pool(self):
        return set(self.manager.candidate_pools[self.SOURCE])

    def report_failures(self, proxy_url, times=1):
        for _ in range(times):
            self.manager.process_feedback(self.SOURCE, proxy_url, 0)

    def test_a_success_long_ago_buys_no_slot_once_the_score_is_gone(self):
        old, fresh = urls(200, first_octet=10), urls(200, first_octet=60)
        self.seed_pool(active=old + fresh)
        for proxy_url in old:
            self.manager.process_feedback(self.SOURCE, proxy_url, 200)
            self.report_failures(proxy_url, 100)
        self.seed_pool(active=old + fresh)
        self.assertLess(self.manager.source_stats[self.SOURCE][old[0]]["score"], 0.01)

        # The tiered pool gave these 180 of 200 slots.
        self.assertEqual(self.pool(), set(fresh))

    def test_one_failure_does_not_hand_the_pool_back_to_dead_incumbents(self):
        old, fresh = urls(200, first_octet=10), urls(200, first_octet=60)
        self.seed_pool(active=old + fresh)
        for proxy_url in old:
            self.manager.process_feedback(self.SOURCE, proxy_url, 200)
            self.report_failures(proxy_url, 100)
        for proxy_url in fresh:
            self.report_failures(proxy_url)
        self.seed_pool(active=old + fresh)

        # The tiered pool gave the old ones all 200 slots. Now they get only
        # the rotation's retrials: they were tried longer ago than the fresh
        # ones, whose single failure still scores above theirs.
        pool = self.pool()
        self.assertEqual(len(pool & set(fresh)), 180)
        self.assertEqual(len(pool & set(old)), self.manager.exploration_slots)

    def test_a_proxy_that_failed_long_ago_comes_round_again(self):
        """Exploration is a rotation, not a reserve for the never-measured."""
        self.manager.candidate_pool_size = 10
        self.manager.exploration_slots = 2
        now = time.time()
        incumbents = urls(30, first_octet=170)
        for proxy_url in incumbents:
            stat = self.manager._get_new_proxy_stat(self.SOURCE)
            stat.update(
                {
                    "success_count": 5,
                    "recent_results": [[now - 60, True, None]] * 5,
                    "last_feedback_ts": now - 60,
                    "last_handed_out_ts": now - 60,
                }
            )
            self.manager.source_stats[self.SOURCE][proxy_url] = stat
        recovered = "http://192.0.2.99:9000"
        stat = self.manager._get_new_proxy_stat(self.SOURCE)
        stat.update(
            {
                "failure_count": 1,
                "recent_results": [[now - 6 * 3600, False, None]],
                "last_feedback_ts": now - 6 * 3600,
                "last_handed_out_ts": now - 6 * 3600,
            }
        )
        self.manager.source_stats[self.SOURCE][recovered] = stat

        # Validation found it alive again; nothing has cleared its record.
        self.seed_pool(active=incumbents + [recovered])

        self.assertIn(recovered, self.pool())
        self.assertEqual(stat["failure_count"], 1)

    def test_a_strong_record_off_the_reserve_page_keeps_its_ranking(self):
        """
        A failed check stamps a row newest and pushes it off the page; the
        record that real traffic built must still rank - and still be served.
        """
        self.manager.candidate_pool_size = 10
        self.manager.exploration_slots = 0
        star = "http://198.51.100.7:8080"
        page = urls(50, first_octet=80)
        self.seed_pool(reserve=[star] + page)
        for _ in range(40):
            self.manager.process_feedback(self.SOURCE, star, 200)
        self.mock_db_instance.get_existing_proxies.side_effect = lambda wanted: {
            proxy_url for proxy_url in wanted if proxy_url == star
        }

        self.seed_pool(reserve=page)

        self.assertIn(star, self.pool())
        self.mock_db_instance.get_existing_proxies.assert_called_with([star])
        handed_out = [self.manager.allocate_proxy(self.SOURCE)["proxy"] for _ in range(200)]
        self.assertIn(star, handed_out)

    def test_a_record_whose_row_is_gone_is_never_served(self):
        """Retained history needs its row: a rebuilt table drops the proxy."""
        self.manager.candidate_pool_size = 10
        self.manager.exploration_slots = 0
        ghost = "http://198.51.100.8:8080"
        page = urls(50, first_octet=80)
        self.seed_pool(reserve=[ghost] + page)
        for _ in range(40):
            self.manager.process_feedback(self.SOURCE, ghost, 200)
        self.mock_db_instance.get_existing_proxies.return_value = set()

        self.seed_pool(reserve=page)

        self.assertNotIn(ghost, self.pool())
        self.assertNotIn(ghost, self.manager.reserve_proxies)

    def test_a_failed_retained_lookup_keeps_the_previous_reserve(self):
        page = urls(20, first_octet=80)
        self.seed_pool(reserve=page)
        self.manager.process_feedback(self.SOURCE, page[0], 200)
        self.mock_db_instance.get_existing_proxies.return_value = None
        self.mock_db_instance.get_reserve_proxies.return_value = urls(5, first_octet=90)

        self.assertFalse(self.manager._refresh_reserve_proxies())
        self.assertEqual(self.manager.reserve_proxies, page)


class WinnerSwitchTests(ProxyManagerTestBase):
    """Old winners die, new ones start succeeding: the pool must follow."""

    SOURCE = "source1"

    def test_the_pool_moves_from_dead_winners_to_new_ones(self):
        self.manager.candidate_pool_size = 50
        self.manager.exploration_slots = 5
        proxies = urls(200, first_octet=100)
        old_winners, new_winners = set(proxies[:20]), set(proxies[20:40])
        self.mock_db_instance.get_active_proxies.return_value = set(proxies)
        self.mock_db_instance.get_reserve_proxies.return_value = []
        rng = random.Random(4242)
        random.seed(4242)

        def serve_round(winners):
            succeeded = 0
            for _ in range(400):
                proxy_url = self.manager.allocate_proxy(self.SOURCE)["proxy"]
                ok = rng.random() < (0.8 if proxy_url in winners else 0.02)
                succeeded += ok
                self.manager.process_feedback(self.SOURCE, proxy_url, 200 if ok else 0)
            return succeeded / 400

        for _ in range(8):
            self.manager._sync_and_select_top_proxies()
            serve_round(old_winners)
        rates = []
        for _ in range(12):
            self.manager._sync_and_select_top_proxies()
            rates.append(serve_round(new_winners))

        # All 20 new winners in a 50-slot pool is 0.332 under a uniform draw.
        # The tiered pool stayed near the 0.02 floor: it could not re-admit a
        # proxy it had measured once.
        self.assertGreater(sum(rates[-3:]) / 3, 0.25)
        self.assertGreater(
            len(set(self.manager.candidate_pools[self.SOURCE]) & new_winners), 15
        )


class WeightedDrawTests(ProxyManagerTestBase):
    """
    Ranking decides who is in the pool; weighting decides how often each is drawn.

    A real cold start drawing uniformly reached ~35% success after 18 minutes
    while the 40 proxies scoring 50+ had 93% on their own record: a uniform
    draw spreads traffic evenly over every slot, proven or not.
    """

    SOURCE = "source1"

    def setUp(self):
        super().setUp()
        self.manager.candidate_pool_size = 100
        self.manager.exploration_slots = 10
        # The shipped default; the shared test config draws uniformly.
        self.manager.selection_strategy = "softmax"
        self.manager.softmax_temperature = 14.0
        self.proven = urls(10, first_octet=100)
        self.untried = urls(190, first_octet=110)
        now = time.time()
        for proxy_url in self.proven:
            stat = self.manager._get_new_proxy_stat(self.SOURCE)
            for index in range(20):
                self.manager._update_reliability_state(stat, True, now - 20 + index)
            stat["success_count"] = 20
            self.manager.source_stats[self.SOURCE][proxy_url] = stat
        self.mock_db_instance.get_active_proxies.return_value = set(
            self.proven + self.untried
        )
        self.mock_db_instance.get_reserve_proxies.return_value = []
        self.manager._sync_and_select_top_proxies()
        random.seed(2710)

    def draw(self, count=4000):
        return [self.manager.allocate_proxy(self.SOURCE)["proxy"] for _ in range(count)]

    def test_traffic_concentrates_on_the_proven_proxies(self):
        handouts = self.draw()
        proven_share = sum(url in set(self.proven) for url in handouts) / len(handouts)

        # 10 of 100 slots: a uniform draw gives them 10%. Softmax at 14 over
        # scores near 100 against the prior of 5 gives them nearly all of the
        # ranked share, which is 90% of handouts.
        self.assertGreater(proven_share, 0.8)

    def test_exploration_slots_get_their_share_of_handouts(self):
        exploring = set(self.manager.candidate_draws[self.SOURCE][2])
        self.assertEqual(len(exploring), 10)

        handouts = self.draw()
        share = sum(url in exploring for url in handouts) / len(handouts)

        # 10 / 100 slots -> 10% of handouts, though every one of them scores
        # at the prior and would almost never win a softmax draw.
        self.assertAlmostEqual(share, 0.10, delta=0.02)

    def test_a_uniform_strategy_still_draws_uniformly(self):
        self.manager.selection_strategy = "uniform"
        self.manager.refresh_candidate_pools()

        handouts = self.draw()
        proven_share = sum(url in set(self.proven) for url in handouts) / len(handouts)

        self.assertAlmostEqual(proven_share, 10 / 100, delta=0.03)
