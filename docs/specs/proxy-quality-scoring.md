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
sliding-window scorer. It is normalized once on the way in - at the API
boundary, by `restore_stats()`, and by `_migrate_legacy_stat()` - and appended
in timestamp order, so the selection path treats it as sorted and binary
-searches the qualification cutoff instead of revalidating every stored entry
on every request. Counter-only database history is seeded from a lifetime
success rate shrunk toward `p0`, then aged from its last feedback timestamp.

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
- A proxy seeded from durable database counters has an empty result window and
  therefore an untouched budget. It re-enters as a discovery candidate holding
  its seeded score; pre-spending the budget on results earned in a previous
  life left it ineligible for exploitation *and* for every exploration group,
  which is unreachable rather than deprioritised.

Exploitation draws from the whole ranked pool. Tiering weights the `tiered`
strategy; it is not the eligibility set. Restricting eligibility to the top
tier caps in-flight concurrency at `top_tier_size` and returns "no proxy
available" while the rest of the ranked, qualified pool sits idle.

Every handout creates an in-flight lease. That proxy is ineligible until its
feedback arrives or `proxy_inflight_timeout_seconds` expires, preventing one
candidate from absorbing a burst before its result is known. Cooldown is an
additional optional constraint.

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
mutation pauses - including the trial budget, which is reputation: a handout
made while the source is paused produces no usable evidence and must not be
charged. A completed recovery window reaching
`outage_recovery_baseline_ratio` of the baseline, with enough distinct
proxies, resumes learning. Transitions are logged, and
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
