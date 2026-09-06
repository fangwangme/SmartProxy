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

    def test_tier_one_concentrates_traffic_on_the_better_proxies(self):
        """The pool is the ranking: proxies that succeed take over the slots."""
        self.manager.candidate_pool_size = 40
        self.manager.exploration_slots = 4
        pool = urls(240, first_octet=100)
        better = set(pool[:20])
        self.mock_db_instance.get_active_proxies.return_value = set(pool)
        self.mock_db_instance.get_reserve_proxies.return_value = []
        self.manager._sync_and_select_top_proxies()

        # Learn, rebuilding the pool as the timer would, then measure.
        for _ in range(10):
            self.drive(requests=400, success_every=100, better=better)
            self.manager._rebuild_candidate_pool(self.SOURCE)

        _, refused, served_better = self.drive(
            requests=400, success_every=100, better=better
        )

        self.assertEqual(refused, 0)
        # Uniform over 240 would put ~8% of traffic on the 20 better proxies.
        self.assertGreater(served_better / 400, 0.25)


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
