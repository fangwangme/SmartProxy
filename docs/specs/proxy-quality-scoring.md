# Proxy Quality: Online Reliability and Selection Boundaries

This spec defines the issue #23 scoring and routing contract. Implementation:
`src/core/proxy_manager.py`.

## 1. Validation and feedback remain separate

Validation owns only the `is_active` liveness gate. Real client feedback owns
reliability, qualification, and traffic allocation. Validator latency,
anonymity, and pass/fail observations must never enter the reliability score.

Feedback latency remains observable as `avg_latency_ms`. It is recorded and
nothing else: it does not enter the score, and it does not order the pool
either. It was a secondary ordering key for tied scores until measurement
showed the tie it served does not occur - across 8000 stored stats only two
groups shared a score, and neither held distinct latencies - so a 1ms success
and a 30s success are now worth exactly the same to selection.

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
sliding-window scorer. It is normalized once on the way in - at the API
boundary, by `restore_stats()`, and by `_migrate_legacy_stat()` - and appended
in timestamp order, so the selection path treats it as sorted and binary
-searches the qualification cutoff instead of revalidating every stored entry
on every request. Counter-only history is seeded from a lifetime
success rate shrunk toward `p0`, then aged from its last feedback timestamp. A
record whose timestamp is missing or unusable is aged to the prior instead of
trusted as fresh: unknown age is unbounded age, and the score drives
exploitation weight. The raw counters survive either way, and the proxy
re-enters as a discovery candidate, so it earns its score back on fresh
evidence.

## 4. Qualification, exploration, and probation

Scoring and trial allocation are independent. A proxy is qualified when it is
live, has at least `qualification_min_results` valid results in the current
forgiveness epoch (default 3), and scores strictly above the fixed prior.
Qualified proxies receive score-driven exploitation traffic from the active
ranked pool.

There is one total exploration budget, driven by how much of the live pool has
been evaluated rather than by an absolute count of winners:

```text
target   = max(exploration_target_qualified,
               live_count * exploration_target_qualified_ratio)
progress = min(qualified_count / target, 1)
ratio    = max_ratio - (max_ratio - min_ratio) * progress
```

Defaults are 30% at zero qualified proxies and 5% once half the live pool
qualifies, with linear interpolation between and an absolute floor of 50 so a
small pool still converges. The absolute target alone would read a 1200-proxy
pool with 110 qualified members as finished and drop exploration to its minimum
while 91% of the pool had never been measured. Discovery, probation, retry, and unqualified proxies in
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

The trial budget is spent by trial handouts alone, and qualifying returns it.
Two rules follow, and both are load-bearing:

- A proxy that qualifies has its trial counter cleared, so a later dip below
  the prior costs it probation and the delayed retries, not the pool. Charging
  the budget for observed results instead meant any proxy with five recent
  results carried an exhausted budget, and its first dip - which a 20%-success
  proxy reaches on a routine losing streak - exiled it for a full forgiveness
  epoch with no retry at all.
- A proxy seeded from counter-only history has an empty result window and
  therefore an untouched budget. It re-enters as a discovery candidate holding
  its seeded score; pre-spending the budget on results earned in a previous
  life left it ineligible for exploitation *and* for every exploration group,
  which is unreachable rather than deprioritised.

Exploitation draws from every live, qualified proxy. Neither tiering nor
`max_pool_size` is an eligibility gate: the tier lists weight the `tiered`
strategy and are recomputed only by the pool sync, so gating on them stranded
any proxy that qualified between syncs - feedback had already removed it from
the trial pool for being qualified, and the exploit set would not take it for
being outside the ranked slice. The plan is ordered by score so a rebuild is
reproducible.

A trial candidate is claimed out of the serving plan when it is handed out and
returned when its feedback arrives, so one candidate cannot absorb a burst
before its result is known. Removal *is* the lease, which is why it costs
nothing on the request path; `proxy_inflight_timeout_seconds` bounds how long a
claim survives if feedback never comes.

Qualified proxies are not serialised. Their success rate is already known, so
holding each to one outstanding request would cap the service at (qualified
proxies / round-trip time) - single-digit requests per second for a pool of a
hundred against a slow target. `proxy_max_inflight` is available as a per-proxy
capacity guard and defaults to 0, meaning unlimited. When it is set, the plan
alone cannot enforce it - a burst inside one plan's lifetime would hand the
same proxy out without limit - so the drawn candidate is checked and redrawn,
which is O(1) on one proxy rather than a pass over the pool. With the cap off
the draw does no checking at all.

`proxy_cooldown_ms` spaces out *trial* handouts and nothing else. It cannot
gate exploitation: the plan is rebuilt on an interval longer than any sane
cooldown, so filtering the exploit set by it removes precisely the proxies that
are getting traffic - the highest-scoring ones - for the whole life of the next
plan. On a 40-proxy pool at a 500ms cooldown, a burst left every one of the top
ten out of the following plan and dropped the best servable score from 99.7 to
38.0.

## 5. Source-wide outage guard

The outage guard observes source results separately from scoring. Every
threshold is a multiple of the source's own success baseline - an EMA over
completed windows that outage windows never feed - because absolute ratios do
not survive contact with a pool whose normal success rate is 10%: an absolute
"healthy window is 50% successful" gate never opens there, and an absolute
"90% failure" trigger sits below that pool's normal state.

The window sizes itself to the baseline. A verdict needs enough observations
that an all-failure run of that length is less likely than
`outage_false_positive_budget` under the baseline: about 66 observations at a
10% baseline, three at 90%, bounded by `outage_window_size` and
`outage_window_max_size`.

A uniformly poor cold start cannot arm the guard: the first completed window
defines its own reference, and a reference of zero fails every gate.
Activation requires:

1. a completed window reaching `outage_healthy_baseline_ratio` of the baseline;
2. a following completed window at or below `outage_failure_baseline_ratio` of
   it; and
3. the configured minimum number of distinct proxies in that failure window.

Tentative proxy mutations from the triggering broad-failure window are rolled
back, field by field rather than by deep-copying each proxy's full result
history on every healthy feedback event. While active, aggregate per-minute
feedback continues, in-flight leases are released, and proxy reputation
mutation pauses for every source rather than only the reporting one - a
`dead` report from a source the guard has judged unreliable must not strip the
reputation those proxies earned elsewhere - including the trial budget, which is reputation: a handout
made while the source is paused produces no usable evidence and must not be
charged. A completed recovery window reaching
`outage_recovery_baseline_ratio` of the baseline, with enough distinct
proxies, resumes learning. Transitions are logged, and
Prometheus metrics expose active state and paused-update totals per source.

## 5a. Serving plan

Routing is split into a control plane and a data plane.

`_build_serving_plan()` decides everything: which proxies are live, qualified
and eligible, which are trial candidates and in which group, what the
exploration budget is, and what the selection weights are. It runs inside the
pool sync - reusing the pass that already refreshes every score and ranks the
pool, rather than sweeping it a second time - and on `serving_plan_max_age_seconds`
between syncs.

`get_proxy()` holds no pool logic. It reads the plan, spends one random draw on
the exploration budget, and takes either a trial candidate (O(1), removed from
the plan) or a weighted exploit pick (O(log n), against cumulative weights the
plan precomputed). Nothing in it scales with the size of the pool.

The plan is therefore allowed to be slightly stale, and that staleness is
bounded by its refresh interval. Two things must not wait for a rebuild, and
neither does:

- whether a trial candidate is currently out, maintained by claim and return;
- a proxy that has just qualified, which is promoted into the live exploit set
  the moment its feedback crosses the line. Without that it falls into a hole -
  feedback removes it from the trial pool because it is no longer a trial
  candidate, while the exploit set was frozen before it qualified. During a cold
  start that hole is most of the pool, and the service answers "no proxy
  available" while holding one that is healthy.

Staleness in the other direction is accepted: a proxy whose score has just
dipped below the prior keeps drawing exploitation traffic until the next
rebuild. That is bounded by `serving_plan_max_age_seconds` and is the cheaper
error - it costs a few requests, where the reverse costs availability.

## 6. Persistence

JSON snapshots contain root-level `scoring_version = 2`. Matching-version
derived estimator state is validated and restored. A missing or mismatched
version never trusts stored derived scores: valid `recent_results` are replayed
in timestamp order.

Proxy reputation is not mirrored into PostgreSQL. If a JSON snapshot is absent
or intentionally skipped, each proxy starts from the fixed prior and relearns
through normal feedback. This accepts a bounded warm-up period in exchange for
removing reputation migrations, hydration, and a second write path.

The optional cold-start mode is non-destructive:

- normal: restore and update the normal JSON state;
- `--no-restore`: skip JSON restore and write JSON only to a `.no-restore`
  sibling path.

`--no-restore` does not delete, rename, or overwrite normal state.

## 7. Configuration ownership

All thresholds above live in `[source_pool]` and are documented in
`config/config.example.ini`. Missing deployment keys are reported by
`check_config_drift()`. `config.ini` remains optional and git-ignored.

## 8. Operational observation

Local deterministic replay proves ordering and learning direction, not the
production ceiling. A rollout should run `--no-restore` or a shadow replay,
bucket requests by score before outcome, and compare the rolling success rate
with the observed stable wall. The target is 90-95% of that wall within roughly
one hour. Publish only aggregate bucket counts/rates; keep proxy addresses,
hostnames, internal domains, paths, and raw request logs private.
