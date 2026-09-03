# Proxy Quality: Online Reliability and Selection Boundaries

This spec defines the issue #23 scoring and routing contract. Implementation:
`src/core/proxy_manager.py`.

## 1. Validation and feedback remain separate

Validation owns only the `is_active` liveness gate. Real client feedback owns
reliability, qualification, and traffic allocation. Validator latency,
anonymity, and pass/fail observations must never enter the reliability score.

Feedback latency remains observable as `avg_latency_ms`. It is only a
deterministic secondary ordering key when reliability scores tie.

## 2. Two-speed online reliability

Each proxy has independent per-source `quality_slow` and `quality_fast`
estimators. Both start at a fixed configured prior `p0` (default `0.05`) and
update for every accepted client result:

```text
slow = (1 - slow_alpha) * slow + slow_alpha * outcome
fast = (1 - fast_alpha) * fast + fast_alpha * outcome
score = 100 * min(slow, fast)
```

`outcome` is 1 for success and 0 for failure. Defaults are
`slow_alpha = 0.12` and `fast_alpha = 0.30`. The slow estimator limits one-hit
promotion; the fast estimator makes deterioration visible immediately. Scores
remain bounded to 0-100 for API and dashboard compatibility.

The prior is fixed, never derived from the current population. With defaults:

| Evidence | Score |
| --- | ---: |
| untried | 5.00 |
| one failure | 3.50 |
| two failures | 2.45 |
| one success | 16.40 |

Changing any equal-age result from failure to success must increase the score.
Every immediate failure must lower it and every immediate success must raise it.

## 3. Time-based forgiveness

Before applying a new event and during pool sync, both estimators decay toward
`p0` using `reliability_decay_half_life_hours`. Aging cannot reverse the sign
of evidence: old good and bad state both converge on the initial score.

`recent_results` is retained as bounded raw replay data. It is not a separate
sliding-window scorer. Counter-only database history is seeded from a lifetime
success rate shrunk toward `p0`, then aged from its last feedback timestamp.

## 4. Qualification, exploration, and probation

Scoring and trial allocation are independent. A proxy is qualified when it is
live, has at least `qualification_min_results` valid results in the current
forgiveness epoch (default 3), and scores strictly above the fixed prior.
Qualified proxies receive score-driven exploitation traffic from the active
ranked pool.

There is one total exploration budget:

```text
progress = min(qualified_count / exploration_target_qualified, 1)
ratio = max_ratio - (max_ratio - min_ratio) * progress
```

Defaults are 30% at zero qualified proxies, 5% at 50 or more, and linear
interpolation between. Discovery, probation, retry, and unqualified proxies in
the ranked pool cannot create extra exploration outside that decision.

Within exploration, two thirds goes to never-tried discovery by default. The
remaining third serves immediate probation and delayed retry candidates. If no
qualified exploit candidate exists, the service may explicitly serve an
eligible trial candidate above the nominal ratio; this cold-start safety
fallback is logged and is not reported as ordinary budgeted exploration.

Each proxy receives three immediate probation handouts. Failing probation
removes it from normal exploitation. At most two later handouts are available,
each only after `retry_delay_seconds`. Once the last handout/feedback is older
than `probation_forgiveness_hours`, the trial epoch resets while historical
counters remain intact.

Every handout creates an in-flight lease. That proxy is ineligible until its
feedback arrives or `proxy_inflight_timeout_seconds` expires, preventing one
candidate from absorbing a burst before its result is known. Cooldown is an
additional optional constraint.

## 5. Source-wide outage guard

The outage guard observes source results separately from scoring. A uniformly
poor cold start cannot arm it. Activation requires:

1. a completed window that met the configured healthy success threshold;
2. a following completed window that met the broad failure threshold; and
3. the configured minimum number of distinct proxies in that failure window.

Tentative proxy mutations from the triggering broad-failure window are rolled
back. While active, aggregate per-minute feedback continues, in-flight leases
are released, and proxy reputation mutation pauses. A completed recovery window
with enough distinct proxies resumes learning. Transitions are logged, and
Prometheus metrics expose active state and paused-update totals per source.

## 6. Persistence and migration

JSON snapshots contain root-level `scoring_version = 2`. Matching-version
derived estimator state is validated and restored. A missing or mismatched
version never trusts stored derived scores: valid `recent_results` are replayed
in timestamp order. Raw feedback and database counters are preserved; migration
does not delete history.

Durable database counters are absolute. Writes use monotonic database updates,
are serialized in-process, and failed batches are re-queued so an idle proxy is
retried without waiting for another feedback event.

Runtime modes are non-destructive:

- normal: restore normal JSON state, hydrate database reputation, and persist
  both normal JSON and durable reputation;
- `--no-restore`: skip JSON restore, keep database hydration/persistence, and
  write JSON only to a `.no-restore` sibling path;
- `--fresh-scoring`: skip JSON and database reputation hydration, disable
  durable reputation writes, keep aggregate feedback, and write JSON only to a
  `.fresh-scoring` sibling path.

Neither experimental mode deletes, renames, or overwrites normal state.

## 7. Configuration ownership

All thresholds above live in `[source_pool]` and are documented in
`config/config.example.ini`. Missing deployment keys are reported by
`check_config_drift()`. `config.ini` remains optional and git-ignored.

## 8. Operational observation

Local deterministic replay proves ordering and learning direction, not the
production ceiling. A rollout should run `--fresh-scoring` or a shadow replay,
bucket requests by score before outcome, and compare the rolling success rate
with the observed stable wall. The target is 90-95% of that wall within roughly
one hour. Publish only aggregate bucket counts/rates; keep proxy addresses,
hostnames, internal domains, paths, and raw request logs private.
