# Proxy Quality: Online Reliability and Candidate Pool Selection

This spec defines the scoring contract from issue #23 and the routing contract
that replaced its selection boundaries in issue #27. Implementation:
`src/core/proxy_manager.py`.

**The governing rule.** Learning decides *which* proxy is served, never
*whether* one is served. No threshold sits anywhere on the routing path, and no
routing parameter can empty the servable set at any value in its accepted
range. A parameter whose wrong value degrades performance is a tunable; a
parameter whose wrong value empties the servable set is a design defect.

## 1. Validation and feedback remain separate

Validation owns only the `is_active` liveness signal. Real client feedback owns
reliability and traffic allocation. Validator latency, anonymity, and pass/fail
observations must never enter the reliability score.

`is_active` is a *signal*, not an admission test. It is the ordering boundary
between tier 2 and tier 3 of the candidate pool (section 4); a proxy that has
never been validated is still servable.

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
in timestamp order. Counter-only history is seeded from a lifetime
success rate shrunk toward `p0`, then aged from its last feedback timestamp. A
record whose timestamp is missing or unusable is aged to the prior instead of
trusted as fresh: unknown age is unbounded age, and the score decides pool
membership and ordering. The raw counters survive either way, so the proxy
earns its score back on fresh evidence while remaining servable throughout.

## 4. Candidate pool selection

Ranking is the only mechanism. Each source keeps a fixed-size candidate pool,
rebuilt on a timer and filled best-first; a handout is a uniform random pick
from that pool.

```text
every pool_refresh_seconds, fill candidate_pool_size slots in priority order:

  tier 1   proxies with successful feedback on record   <- scored, ordered
  tier 2   proxies that passed validation (is_active)   <- tops up tier 1
  tier 3   anything else, including never-validated     <- tops up the rest
```

Tier 1 is defined by feedback, not by validation: `is_active` is not one of its
conditions. Its candidates are the proxies the router can see - the live set
and the reserve page (4.1) - so a proxy with a success on record that has since
failed validation is usually outside both until it passes again. That boundary
is provisional; see 4.5.

Score orders within a tier and decides who makes the cut. It never decides
whether a proxy may serve. The pool therefore always holds
`min(candidate_pool_size, everything available)`, which makes an empty answer
structurally impossible while the database holds any proxy at all. The
invariant is a property of the data structure, not a fallback branch someone
has to remember to write - and it is asserted by test, not by review note.

Ordering within tiers 1 and 2 is by descending score, then by URL, so two
rebuilds over the same state agree; `active_proxies` is a set and its iteration
order is not reproducible on its own. Tier 3 keeps the order the database
returned, because score there is the untouched prior for very nearly every row.

A cold pool is the same code path as a warm one. With nothing validated and
nothing scored, tier 3 fills every slot from whatever the fetchers have found;
validation and feedback then promote proxies into tiers 2 and 1 over the
following hours, and the served-quality curve rises while the served volume
never drops.

### 4.1 Tier 3 has its own query

`get_active_proxies()` answers `is_active = true` only, so the routing layer
cannot see a never-validated proxy through it - which on a cold start is the
entire population, and exactly the tier that has to carry it.
`get_reserve_proxies(limit)` supplies the rest of the table. Its result is held
in memory as `reserve_proxies`, so the pool refresh stays lock-only and never
waits on the database.

That placement is deliberate. Running the query on the refresh timer instead
would put a network round trip inside the routing control plane, and a brief
database outage would then empty tier 3 - the one tier that carries a cold
start. A failed reserve query keeps the previous list rather than emptying it.

Publication is event-driven, and must never be conditional on a validation
cycle *succeeding* - rows are servable the moment they are committed, whatever
the validator later makes of them. Three paths publish it:

| Path | Why |
| --- | --- |
| pool sync | the ordinary case, alongside the active query |
| a fetch that committed rows | "serves from the first fetch onward" means the fetch, not the validation cycle that may follow minutes later |
| a validation cycle whose target quorum is down | otherwise a target that stays down leaves the router blind to the whole table |

The last two go through `_publish_reserve_pool()`, which republishes tier 3 and
rebuilds but never reads or rewrites `active_proxies`. That narrowness is the
point: it can run on a path where a validation batch has just failed, where
preserving last-known-good liveness is the standing contract.

Reserve proxies keep their records under the stats cap exactly as live ones do:
`_truncate_stats_pool()` exempts the whole servable set, live and reserve. The
pool re-seeds a blank stat for any member that lacks one, so evicting a reserve
proxy that is still being served would launder its failure history.

The row count is bounded by `candidate_pool_size`: one pool's worth is all
tier 3 can ever place, so the bound needs no tunable of its own. Rows are
ordered never-validated first - an unmeasured proxy is a better guess than one
that has already failed a check - then by the stalest last check.
`idx_proxies_reserve_pool` matches that ordering so the `LIMIT` stops at the
first page rather than sorting every dead row in the table.

### 4.2 Reserved exploration slots

`exploration_slots` of the N slots are held back for proxies that have never
been measured. This is the only remaining role of exploration, and it exists
for one reason: a pool filled entirely from tier 1 would never admit a new
proxy again - today's best would hold their slots indefinitely and tomorrow's
better proxy would never be discovered.

It is a floor, not a gate:

- reserved slots that no newcomer claims go back to the ranking;
- `0` disables the reservation and the pool still fills;
- a value at or above `candidate_pool_size` still fills the pool, it merely
  spends it all on newcomers - degraded quality, never degraded availability.

### 4.3 What is not on the routing path

Attempt history and per-proxy concurrency may be tie-breaks or weights when
filling the pool. Neither may remove a proxy from it.

- **Leases** (`inflight`, bounded by `proxy_inflight_timeout_seconds`) are
  bookkeeping. Their only consumer is `process_feedback`, which reads the count
  to tell a first report from a duplicate or a late one. Selection never looks
  at one, so a burst against a one-proxy pool keeps being served by that proxy.
  A lease is runtime state and is not persisted: it cannot outlive the process
  that granted it, so restoring one would only manufacture a phantom.
  Expired leases are reclaimed where the list grows - `_grant_lease()` drops
  the expired prefix before appending - and again in the sync's pass over every
  stat, which is what reaches proxies that stopped being handed out entirely.
  Feedback alone is not enough: a client that takes proxies and never reports
  is precisely the case that sends none, and the list is deep-copied and
  serialised by every backup.
- **Weighting inside the pool is dormant.** The draw is uniform;
  `selection_strategy`, `softmax_temperature`, `selection_weight_floor` and
  `top_tier_load_percentage` are parsed and validated but not consulted.
  Re-introducing weights on top of the pool is a later change and an
  optimisation, not a requirement.
- **The ranked tier lists** (`max_pool_size`, `top_tier_size`) are reporting
  and the input that later weighting change would use. They are not an
  eligibility gate and are not what `/get-proxy` reads.

### 4.4 Removed in issue #27

Four absolute thresholds sat in front of this ranking system, and each could
independently empty the servable set. A deliberate cold start on 2026-09-05
produced 1,178 refusals while 300+ proxies were validated alive; the score gate
was the load-bearing one and was self-defeating, since the threshold was by
definition the score of an unmeasured proxy.

| Removed gate | Rule it enforced |
| --- | --- |
| Validation | only `is_active` proxies were candidates |
| Score | `>= qualification_min_results` results **and** `score > prior` |
| Attempts | 3 probation handouts, then a locked retry delay, then exile |
| Concurrency | unqualified proxies hardcoded to 1 in-flight request |

With them went 18 functions and 13 routing tunables:
`exploration_min_ratio`, `exploration_max_ratio`,
`exploration_target_qualified`, `exploration_target_qualified_ratio`,
`exploration_discovery_share`, `qualification_min_results`,
`probation_attempts`, `retry_attempts`, `retry_delay_seconds`,
`probation_forgiveness_hours`, `exploit_draw_attempts`, `proxy_max_inflight`
and `proxy_cooldown_ms`. `serving_plan_max_age_seconds` became
`pool_refresh_seconds`, and `avg_latency_alpha` became a module constant since
nothing reads `avg_latency_ms` to decide anything.

`proxy_cooldown_ms` went with them although it is not one of the four gates: a
cooldown filters pool members, so at a large enough value it holds every member
out at once, which the governing rule does not permit to survive.

### 4.5 Open decision: proven proxies outside the reserve page

A proxy that has succeeded for real clients and then failed validation is a
tier-1 proxy by definition, but the router usually cannot see it: its last
check is the newest in the table, so it sorts to the end of the reserve
ordering and falls outside the page. Until it passes validation again - and the
failed-proxy retry queue is oldest-first across the whole dead table, so that
can take hours - it is out of the ranking.

Pulling every such proxy into tier 1, existence-checked against the database,
is the obvious fix and was deliberately not taken, because of how tier 1 is
defined: *any* success on record, a lifetime counter that never resets, filled
as a block ahead of tier 2. Widened to all history, tier 1 fills with proxies
that succeeded once and have since died, ahead of proxies validated alive. A
simulation of 120 live proxies and 170 non-active proxies with a success on
record, 20 of which still work:

| True success rate of the 20 that still work | not widened | widened | widened: traffic to dead proxies |
| --- | ---: | ---: | ---: |
| 30% | 0.073 | 0.141 | 25% |
| 12% | 0.073 | 0.081 | 26% |
| 3% | 0.073 | 0.057 | 36% |
| 0% | 0.073 | 0.057 | 37% |

Widening pays only if proven-but-non-active proxies keep succeeding at about
the rate of the better live ones; otherwise it costs roughly a fifth of served
quality. The simulation does not model re-validation, which returns a working
proxy to the live set on its own, so it overstates the gain.

Bounding the widening by score - only proxies scoring above the prior - was
tested and rejected. It is the removed score gate in miniature: a non-active
proxy that dips below the prior on an ordinary losing streak is never served
again, so it can never produce the evidence that would lift it back (0.116 in
the 30% row, below both alternatives).

The decision waits on production data: how many proxies with a success on
record are non-active, how recent that success is, and how often they pass
re-validation.

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
reputation those proxies earned elsewhere. A paused source keeps serving from
its pool throughout; the guard protects reputation, never availability.
A completed recovery window reaching
`outage_recovery_baseline_ratio` of the baseline, with enough distinct
proxies, resumes learning. Transitions are logged, and
Prometheus metrics expose active state and paused-update totals per source.

## 5a. Pool refresh

Routing is split into a control plane and a data plane.

`_rebuild_candidate_pool()` decides everything: which proxies are in the pool
and in what order. It runs inside the pool sync - reusing the pass that already
refreshes every score - and on `pool_refresh_seconds` between syncs. It also
seeds a stat for every pool member, because anything that can be handed out
needs somewhere to record feedback, or a tier-3 proxy could never earn its way
up into tier 1.

`allocate_proxy()` holds no pool logic: one dict lookup and one
`random.choice`. Nothing in it scales with the size of the pool, and nothing in
it can refuse. Its only `None` is an empty pool, which means an empty database.

The pool is therefore allowed to be stale, and that staleness is bounded by
`pool_refresh_seconds`. Nothing is repaired on the request thread - a rebuild
there would put pool-sized work behind the manager lock on every handout, and a
request arriving while the pool is stale still has a pool. Staleness costs
ranking accuracy, which feedback corrects on the next rebuild; it never costs
availability, which is the trade the previous design got backwards.

The one case built on demand is a source whose pool does not exist yet, which
covers embedders that construct a manager without running the sync lifecycle.

## 6. Persistence

JSON snapshots contain root-level `scoring_version = 2`. Matching-version
derived estimator state is validated and restored. Keys an older build wrote
that the current one does not know - `trial_handout_count`, `retry_after_ts`
and `inflight` among them - are ignored on load, never rejected. There is no
migration script: `database_setup.sql` is the only schema authority. A missing or mismatched
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
